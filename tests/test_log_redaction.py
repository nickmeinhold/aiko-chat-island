"""A device token is a credential and it was going into the log in full.

APNs puts the token in the URL PATH, so httpx's own request logging wrote all 64
hex characters at INFO on every send (claude-tasks#3586). These tests pin the
redaction AND the thing the redaction must not break: the log line is the only
direct evidence of which Apple host a row was sent to, so a filter that ate the
line would remove the observability along with the leak.
"""
from __future__ import annotations

import logging

from aiko_gateway.domain import apns  # noqa: F401  (import installs the filter)

# A real 64-hex APNs device token (synthetic — not a production value).
TOKEN = "d309f150ddd0b42453a756f697febf5660c11d1b9a0ae94cfa485af65679effe"
HTTPX_MSG = 'HTTP Request: POST %s "%s"'


def _emit(caplog, url: str, status: str = "HTTP/2 200 OK") -> str:
    """Log through the REAL "httpx" logger, the way httpx does — with %-args
    rather than a pre-formatted string, because that difference is the bug the
    filter has to survive."""
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(HTTPX_MSG, url, status)
    return caplog.records[-1].getMessage()


def test_a_device_token_is_trimmed_to_a_twelve_char_prefix(caplog) -> None:
    """The leak itself. 12 hex = 48 bits: useless for reconstructing a 256-bit
    token, sufficient to correlate a line with a row (`substr(token,1,12)`
    disambiguated instantly against the live table while debugging #3386)."""
    out = _emit(caplog, f"https://api.push.apple.com/3/device/{TOKEN}")
    assert TOKEN not in out, f"the full token survived redaction:\n{out}"
    assert "d309f150ddd0..." in out, out


def test_redaction_preserves_the_host_and_the_status(caplog) -> None:
    """THE CONTROL THAT MATTERS. A filter that suppressed or mangled the line
    would delete the only production evidence that `_host()` routes per row — the
    thing that witnessed #3386's central claim on 2026-08-29. Redacting must cost
    the token and nothing else."""
    out = _emit(caplog, f"https://api.sandbox.push.apple.com/3/device/{TOKEN}")
    assert "api.sandbox.push.apple.com" in out, out
    assert "HTTP/2 200 OK" in out, out
    assert "/3/device/" in out, out


def test_the_token_does_not_survive_in_record_args(caplog) -> None:
    """httpx logs with %-args, so redacting `record.msg` alone would leave the
    full token sitting in `record.args` for any OTHER handler to format back out —
    a redaction that only works for one handler is not a redaction."""
    _emit(caplog, f"https://api.push.apple.com/3/device/{TOKEN}")
    record = caplog.records[-1]
    assert TOKEN not in str(record.args), f"token survived in args: {record.args!r}"
    assert TOKEN not in str(record.msg), f"token survived in msg: {record.msg!r}"


def test_a_short_hex_run_is_left_alone(caplog) -> None:
    """NULL ARM — the filter must not fire on everything with hex in it. A check
    that redacts unconditionally would pass the tests above while quietly
    shredding unrelated log lines."""
    out = _emit(caplog, "https://api.push.apple.com/3/device/deadbeefcafe")
    assert "deadbeefcafe" in out, out
    assert "..." not in out, out


def test_a_ulid_row_id_is_not_redacted(caplog) -> None:
    """The replacement identifier must survive. push_service now logs the ROW ID
    instead of the token; a ULID is 26 chars of Crockford base32, so it must not
    trip a rule aimed at 32+ char hex runs."""
    out = _emit(caplog, "https://example.test/x/01M16A4T4QSTC7YB6G675VG3VF")
    assert "01M16A4T4QSTC7YB6G675VG3VF" in out, out
