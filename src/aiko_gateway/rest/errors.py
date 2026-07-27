"""Structured HTTP error shapes shared across REST ingresses.

Some auth-terminal responses must carry a MACHINE-STABLE code, not just a
human-readable `detail`, so a client branches on the code and never on the prose
(which we are free to retune for humans). Today that's the ban 403: the app
detected a suspension by string-matching `detail` — a coupling to our exact
wording (`aiko_chat_app` handoff §2, tracked app-side as #30). This module makes
the code first-class and additive: `detail` is preserved verbatim, so the prose
match keeps working until the client cuts over to `error`.

Registration is a SINGLE DOOR (`register_error_handlers`) called by BOTH `main`
and any test app builder — the same anti-fragmentation discipline as the auth
ingresses (#1927): a handler installed in one place but not the other would make
the wire shape depend on how the app was assembled.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

ACCOUNT_SUSPENDED = "account_suspended"


class AccountSuspended(HTTPException):
    """403 for a banned account, carrying a machine-stable `error` code.

    Raised at EVERY ban ingress (get_current_user + the `_deny_if_banned` login/
    mint door). Subclasses HTTPException so an app WITHOUT the handler still
    renders a correct 403 (via the default handler, prose-only) — the handler
    only upgrades the body to include the top-level `error` code.
    """

    code = ACCOUNT_SUSPENDED

    def __init__(self) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "account suspended")


async def _account_suspended_handler(
    request: Request, exc: AccountSuspended
) -> JSONResponse:
    # Top-level `error` (what the client keys on) + `detail` verbatim (additive,
    # non-breaking for the existing prose match).
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.detail},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the structured-error handlers. Call from `main` AND every test app
    builder that asserts a structured body — the single door for the wire shape."""
    app.add_exception_handler(AccountSuspended, _account_suspended_handler)
