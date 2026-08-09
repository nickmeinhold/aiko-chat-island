"""@-mention span carriage (#2632) — the gateway's carrier role for mentions.

The client sends key-bound mention spans on the WS ``send`` frame; the gateway
SHAPE-validates + caps them at this trust boundary, then persists + echoes them
VERBATIM (messages.mentions, message_view). It is a CARRIER, not a resolver: it
never looks a target up, never re-derives the client's offset basis, never
rewrites a span. This mirrors ``signing.validate_origin`` — same fail-closed
discipline (exact types, reject bool-as-int, no extra keys, size caps, a FRESH
projection so a later mutation of the inbound frame can't reach stored JSON) — but
there is no signature here, only structure.

Grounded against the app tab's ADR-0004 (aiko_chat_app/docs/adr): a mention
TARGET keys off the opaque identity, never a home-qualified string, so a rename
never orphans a mention (the client re-resolves key->current-handle at render).
And there is **no central directory** — autocomplete sources from the channel
member roster (the enriched ``GET /channels/{id}/members``), so nothing here
resolves or searches users.

Why ``target_type`` is NOT value-enumerated: the /v1 wire is append-only because
the gateway and app deploy independently (no shared cadence). A strict
``{user,channel,everyone}`` enum on the gateway would force a gateway deploy the
day the app adds a new target kind (e.g. ``role``). So we validate the
DISCRIMINATOR's shape (a short lowercase token) and carry its value opaquely — the
app owns the semantics. This is the same posture as carrying ``origin`` without
verifying the signature.

Why offsets are NOT bounds-checked against ``body``: the offset basis is UTF-16
code units (the app's declared, Dart-native basis); Python indexes code points, so
a gateway bounds-check would have to re-derive the app's basis — exactly the
coupling the "opaque round-trip" contract avoids. An out-of-range span is a client
bug the client's own renderer catches; the request-size cap (#28) already bounds
how large an offset can be. We validate that offset/length are sane integers, not
that they land inside this particular body.
"""
from __future__ import annotations

import re
from typing import Any, TypedDict

# Caps — a message carrying thousands of spans is a storage/DoS vector, not a real
# mention. Generous vs any real composer, tight vs abuse.
_MAX_MENTIONS = 64
_MAX_TARGET_ID = 128          # an Ed25519 multibase key is ~48 chars; headroom for opaque ids
_TARGET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")  # shape only; value is opaque
# offset/length are bounded well above any real UTF-16 index in a size-capped body,
# purely to reject absurd integers (a 10**18 offset is not a real span).
_MAX_INDEX = 1 << 20

_SPAN_KEYS = frozenset({"target_type", "target_id", "offset", "length"})


class MentionSpan(TypedDict):
    """The fixed four-key mention span carried on a message (#2632). The key SET is
    frozen forever (append-only wire extends via new target_type VALUES, never new
    span keys), so it is a named type, not a bare ``dict`` — the value of
    ``target_type`` stays opaque (the app owns its semantics), but the shape does
    not. What ``validate_mentions`` returns and ``messages.mentions`` stores."""
    target_type: str
    target_id: str
    offset: int
    length: int


class MentionError(ValueError):
    """A malformed / oversized mentions array. The send handler maps it to a
    ``bad_mentions`` wire error and NEVER persists — fail-closed, like OriginError."""


def _is_int(v: Any) -> bool:
    # bool is a subtle int subclass (True == 1); a span index/flag must be a real
    # int, never a bool sneaking through — same guard signing.validate_origin uses.
    return isinstance(v, int) and not isinstance(v, bool)


def validate_mentions(raw: Any) -> list[MentionSpan] | None:
    """Validate an inbound ``mentions`` value to a fresh list of clean span dicts,
    or raise :class:`MentionError`. ``None``/absent is legal (a message with no
    mentions) and returns ``None`` so message_view omits the key. An empty list is
    structurally valid and returns ``[]``; the persistence layer normalizes empty →
    ``None`` so storage, view, and wire carry ONE representation of "no mentions".

    The returned spans are FRESH dicts with exactly the four span keys, in a stable
    field order — so what we persist/echo cannot be reached by a later mutation of
    the caller's frame, and never carries an unexpected key the client tucked in.
    Fully-identical spans are de-duplicated (first occurrence wins, order preserved):
    a mention set is a set, so 64 copies of one span is abuse the cap shouldn't be
    the only wall against — two spans with the same target but DIFFERENT offsets stay
    distinct (mentioning one person twice in a message is legitimate).
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise MentionError("mentions must be a list")
    if len(raw) > _MAX_MENTIONS:
        raise MentionError(f"mentions exceeds the {_MAX_MENTIONS}-span cap")

    out: list[MentionSpan] = []
    seen: set[tuple[str, str, int, int]] = set()
    for span in raw:
        if not isinstance(span, dict):
            raise MentionError("each mention span must be an object")
        if set(span.keys()) != _SPAN_KEYS:
            raise MentionError(
                "each mention span must have exactly "
                "{target_type, target_id, offset, length}")

        ttype = span["target_type"]
        if not isinstance(ttype, str) or not _TARGET_TYPE_RE.match(ttype):
            raise MentionError(
                "target_type must be a short lowercase token (^[a-z][a-z0-9_]{0,31}$)")

        tid = span["target_id"]
        if not isinstance(tid, str) or not (0 < len(tid) <= _MAX_TARGET_ID):
            raise MentionError(
                f"target_id must be a non-empty string within {_MAX_TARGET_ID} chars")
        # Opaque, but not a smuggling channel: reject control/non-printable chars so a
        # carried id can't inject a newline/escape into a log line or a client UI. A
        # real key/ULID/opaque-id is printable; this mirrors origin's charset discipline
        # without dictating the id's content (still resolver-free).
        if not tid.isprintable():
            raise MentionError("target_id must not contain control/non-printable characters")

        offset = span["offset"]
        if not _is_int(offset) or not (0 <= offset <= _MAX_INDEX):
            raise MentionError(f"offset must be an int in [0, {_MAX_INDEX}]")

        length = span["length"]
        if not _is_int(length) or not (1 <= length <= _MAX_INDEX):
            raise MentionError(f"length must be an int in [1, {_MAX_INDEX}]")

        key = (ttype, tid, offset, length)
        if key in seen:
            continue  # drop an exact-duplicate span (dedup; first occurrence kept)
        seen.add(key)
        out.append(MentionSpan(
            target_type=ttype, target_id=tid, offset=offset, length=length))
    return out


def strip_blocked_user_mentions(
    spans: list[dict] | None, blocked_user_ids: set[str]
) -> list[dict] | None:
    """Drop ``user``-typed spans whose ``target_id`` is in a block relationship with
    the sender — the NO-INTERACTION-across-a-block rule extended to mentions (#7).

    A mention span NAMES a target (unlike the sender's own ``origin`` envelope), so
    the moderation door the ``reply_to`` gate names for "future interaction surfaces
    (DMs, mentions)" opens here. STRIP, don't reject: the message still posts, the
    blocked user is simply never a live mention target (the span degrades to inert
    ``@text``, the existing "unresolved mention stays inert" contract). This is a
    relationship check on two ids — NOT identity resolution — so it keeps the
    carrier-not-resolver posture: the composer picks ``user`` targets from the member
    roster whose ``user_id`` IS this ``User.id``, so ``target_id in blocked_user_ids``
    is exact. Non-``user`` targets (channel/everyone) have no user to block and pass.

    Returns ``None`` unchanged; returns ``None`` if every span was stripped (so the
    persistence layer's empty→NULL normalization sees nothing to store)."""
    if not spans:
        return spans
    kept = [s for s in spans
            if not (s.get("target_type") == "user" and s.get("target_id") in blocked_user_ids)]
    return kept or None
