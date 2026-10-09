"""Keep credentials out of the logs, whichever logger and handler carries them.

TWO CLASSES OF CREDENTIAL ARE REDACTED, and both reached a log line in production:

  1. A long hex run (claude-tasks#3586). httpx logged the APNs device token, which is
     part of the request URL path `/3/device/<token>`.
  2. A credential-named QUERY PARAMETER (found 2026-10-09). uvicorn logs the full
     path of every request and WebSocket handshake, so `GET /v1/ws?token=<access JWT>`
     wrote a live login token into the container log on every connect. The OAuth
     callback (`?code=…&state=…`) did the same with the provider's authorization
     code. A JWT is base64url, not hex, so class 1 never matched it.

WHY THIS IS NOT ONLY A ROOT-HANDLER FILTER, and the reason #2 got past the #3586 fix:
uvicorn's CLI configures logging BEFORE it imports the app, and its `uvicorn` and
`uvicorn.access` loggers carry their OWN handlers with `propagate=False`. Their records
never reach the root logger. A filter on the root handlers, which was the whole of the
previous installation, could not see them whatever pattern it matched. `install` walks
EVERY handler that exists, not just root's.

REDACTED IN PLACE, NEVER BY CLEARING `args`. uvicorn's `AccessFormatter` unpacks
`record.args` as a 5-tuple (client, method, path, version, status). The previous filter
rewrote the formatted message and set `args = ()`, which on an access record would turn
every request line into a formatting error. So this rewrites each argument and keeps
the tuple's shape. A non-string argument (httpx's `URL` object, the original #3586
carrier) is replaced by its redacted string only when it actually holds a credential,
so an `int` status code stays an `int`.

A REDACTION, NOT A SILENCE: the request line, the path and the non-secret parameters
stay legible. Those are the operator's evidence of what happened.
"""
from __future__ import annotations

import logging
import re

# A >=32-char hex run, trimmed to its first 12 chars. 48 bits cannot reconstruct a
# 256-bit device token and are plenty to correlate a log line with a DB row.
LONG_HEX = re.compile(r"\b([0-9a-fA-F]{12})[0-9a-fA-F]{20,}\b")

# Query parameters whose VALUE is a credential. Named rather than shape-matched,
# because a JWT, an OAuth code and an opaque state share no shape. `token` is the
# WebSocket auth (realtime/ws.py); `code` and `state` are the OAuth broker callback
# (rest/auth.py). The `*_token` names have no reader today and are listed so that a
# new endpoint is covered by default. The value runs to the next `&`, whitespace, a
# quote, or the end, which are the delimiters uvicorn and httpx log a URL inside.
SECRET_PARAMS = frozenset({"token", "access_token", "refresh_token", "id_token",
                           "code", "state"})
SECRET_QUERY = re.compile(
    r"([?&](?:" + "|".join(sorted(map(re.escape, SECRET_PARAMS))) + r")=)[^&\s\"']+")


def redact(text: str) -> str:
    """Both redactions, applied to one string."""
    return SECRET_QUERY.sub(r"\1<redacted>", LONG_HEX.sub(r"\1...", text))


def _redact_arg(arg: object) -> object:
    if isinstance(arg, str):
        return redact(arg)
    if isinstance(arg, (int, float, bool)) or arg is None:
        return arg
    try:
        as_text = str(arg)
    except Exception:  # pragma: no cover - a broken __str__ must not kill a log call
        return arg
    cleaned = redact(as_text)
    return cleaned if cleaned != as_text else arg


class RedactCredentials(logging.Filter):
    """Rewrite credentials out of a record's message AND args, keeping their shape."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            # BACKSTOP for a credential split across a format string and its args,
            # which neither piece shows alone. Only then collapse to a plain
            # message, because collapsing breaks formatters that read `args`.
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg, record.args = cleaned, ()
        except Exception:  # pragma: no cover - never let redaction drop a record
            pass
        return True  # this filter only rewrites; it never drops


def _filtered(target: logging.Filterer) -> bool:
    return any(isinstance(f, RedactCredentials) for f in target.filters)


def install() -> int:
    """Attach the filter to every handler that exists now. Returns how many were new.

    Root's handlers, plus the handlers of every configured logger. That second set is
    what reaches uvicorn's non-propagating loggers. Idempotent, so it is safe to call
    at import AND again at startup, after a server that configures logging late.
    """
    # SNAPSHOT FIRST. `list(dict.values())` copies in C without releasing the GIL;
    # a Python-level comprehension over the live dict can raise "dictionary
    # changed size during iteration" if a thread creates a logger mid-walk, and
    # the lifespan call runs while startup work is under way (Maxwell, PR#195).
    registered = list(logging.Logger.manager.loggerDict.values())
    loggers = [logging.getLogger()] + [lg for lg in registered
                                       if isinstance(lg, logging.Logger)]
    added = 0
    for lg in loggers:
        for handler in lg.handlers:
            if not _filtered(handler):
                handler.addFilter(RedactCredentials())
                added += 1
    return added
