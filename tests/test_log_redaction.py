"""Credentials stay out of the logs, through uvicorn's OWN handlers too.

THE LEAK (2026-10-09, enspyr): every WebSocket handshake logged
`"WebSocket /v1/ws?token=<access JWT>" [accepted]`. The #3586 redaction could not
catch it, for two independent reasons, and each test below pins one:

  * it matched only hex, and a JWT is base64url;
  * it sat on the ROOT handlers, and uvicorn's CLI gives `uvicorn` and
    `uvicorn.access` their own handlers with `propagate=False`. The record never
    reached the filter.

So these tests run under UVICORN'S REAL `LOGGING_CONFIG`. A hand-built logger would
propagate to root and pass for the wrong reason, which is exactly the frame that
hid the leak in the first place.
"""
from __future__ import annotations

import copy
import io
import logging
import logging.config

import httpx
import pytest
from uvicorn.config import LOGGING_CONFIG

from aiko_gateway import log_redaction

_BASELINE_ROOT_FILTERS = {id(h): h.filters[:] for h in logging.getLogger().handlers}

JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMU0wMlkzSzJNVEhSNFlaQ0hQR0JaNkhLVCJ9"
       ".-LKoC-OT94xGfakeSignature_abc123")


@pytest.fixture
def uvicorn_logging():
    """uvicorn's own config, writing into a buffer; logging state restored after."""
    saved = {name: (lg.handlers[:], lg.propagate, lg.level, lg.filters[:])
             for name, lg in logging.Logger.manager.loggerDict.items()
             if isinstance(lg, logging.Logger)}
    root = logging.getLogger()
    # Each root handler's FILTERS too, not just the list (Carnot, PR#195 r2): the
    # root-only control and install() both add filters to these shared handler
    # objects, and restoring only the list leaked them into later tests.
    saved_root = (root.handlers[:], root.level,
                  {id(h): h.filters[:] for h in root.handlers})
    buf = io.StringIO()
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg["handlers"]["default"]["stream"] = buf
    cfg["handlers"]["access"]["stream"] = buf
    logging.config.dictConfig(cfg)
    yield buf
    for name, (handlers, propagate, level, filters) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.propagate, lg.level, lg.filters[:] = handlers, propagate, level, filters
    root.handlers[:], root.level = saved_root[0], saved_root[1]
    for h in root.handlers:
        h.filters[:] = saved_root[2].get(id(h), h.filters)


def _ws_handshake_line():
    # uvicorn's exact call: protocols/websockets logs via "uvicorn.error".
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "172.19.0.1:36974", f"/v1/ws?token={JWT}")


def test_the_websocket_token_is_redacted_on_uvicorns_own_handler(uvicorn_logging):
    log_redaction.install()
    _ws_handshake_line()
    out = uvicorn_logging.getvalue()
    assert JWT not in out and "eyJhbGci" not in out
    assert '"WebSocket /v1/ws?token=<redacted>" [accepted]' in out, out


def test_must_fail_without_install_the_token_leaks(uvicorn_logging):
    """THE CONTROL: the harness above can observe the leak it claims to stop."""
    _ws_handshake_line()
    assert JWT in uvicorn_logging.getvalue()


def test_must_fail_a_root_only_filter_still_leaks(uvicorn_logging):
    """The shape of the previous installation: root handlers only. Under uvicorn's
    config the record never reaches them, so it leaks however good the pattern."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_redaction.RedactCredentials())
    _ws_handshake_line()
    assert JWT in uvicorn_logging.getvalue()


def test_the_access_log_keeps_its_shape_and_loses_the_oauth_code(uvicorn_logging, capsys):
    """uvicorn's AccessFormatter UNPACKS record.args as a 5-tuple. Clearing args (the
    old filter's move) would turn every request line into a logging error."""
    log_redaction.install()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "172.19.0.1:1", "GET",
        "/v1/auth/oauth/github/callback?code=gho_SECRETcode123&state=st4te-SECRET",
        "1.1", 302)
    out = uvicorn_logging.getvalue()
    assert "Logging error" not in capsys.readouterr().err
    assert "gho_SECRETcode123" not in out and "st4te-SECRET" not in out
    assert "/v1/auth/oauth/github/callback?code=<redacted>&state=<redacted>" in out
    assert "302" in out


def test_non_secret_parameters_are_left_alone(uvicorn_logging):
    log_redaction.install()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "172.19.0.1:1", "GET",
        "/v1/channels/01M46EANYZ253Z04Y9XR8K4PAV/messages?after=01M4DHDM211XX34HBYQ10V2MR0&limit=50",
        "1.1", 200)
    assert "?after=01M4DHDM211XX34HBYQ10V2MR0&limit=50" in uvicorn_logging.getvalue()


@pytest.mark.parametrize("text", ["/x?codec=vp8", "/x?statement=1", "/x?tokens=3",
                                  "/x?mytoken=1"])
def test_a_parameter_that_merely_contains_a_secret_name_is_not_redacted(text):
    assert log_redaction.redact(text) == text


def test_the_hex_device_token_is_still_redacted_in_an_httpx_url_object():
    """claude-tasks#3586, now through the shape-preserving path: httpx logs a URL
    OBJECT, not a str, so the arg must be rewritten via its str()."""
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               'HTTP Request: %s %s "%s %d %s"',
                               ("POST", httpx.URL("https://api.push.apple.com/3/device/" + "ab" * 32),
                                "HTTP/2", 200, "OK"), None)
    log_redaction.RedactCredentials().filter(record)
    message = record.getMessage()
    assert "ab" * 32 not in message and "/3/device/abababababab..." in message
    assert record.args[3] == 200, "an int arg must stay an int"


def test_install_is_idempotent(uvicorn_logging):
    log_redaction.install()
    assert log_redaction.install() == 0
    for name in ("uvicorn", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            assert sum(isinstance(f, log_redaction.RedactCredentials)
                       for f in handler.filters) == 1


@pytest.mark.parametrize("name", ["Token", "CODE", "State", "Access_Token"])
def test_a_secret_name_in_any_case_is_redacted(name):
    assert log_redaction.redact(f"/x?{name}=s3cr3t&after=1") == f"/x?{name}=<redacted>&after=1"


def test_a_redaction_failure_withholds_the_record_rather_than_passing_it():
    """Fail closed: a record whose redaction raised is not proven clean."""
    class Boom:
        def __str__(self):
            raise RuntimeError("no")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s %s",
                               ("?token=SECRET", Boom()), None)   # 3 slots, 2 args
    log_redaction.RedactCredentials().filter(record)
    message = record.getMessage()
    assert "SECRET" not in message and "withheld" in message


def test_the_fixture_leaves_no_filter_behind_on_root():
    """Run after the root-only control in file order: root's handlers must be clean."""
    for handler in logging.getLogger().handlers:
        assert not any(isinstance(f, log_redaction.RedactCredentials)
                       and f not in _BASELINE_ROOT_FILTERS.get(id(handler), [])
                       for f in handler.filters)
