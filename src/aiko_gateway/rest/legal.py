"""Public legal documents — Privacy Policy + Terms of Use (#13).

Served as static HTML straight from the gateway so ``chat.imagineering.cc/privacy``
and ``/terms`` are stable public URLs for the App Store / Google Play listings and
for the in-app EULA's "Privacy Policy" link.

PUBLIC by design — no auth dependency. A store reviewer's bot, and any user
before they sign in, must be able to read these. This is the deliberate exception
to the I1 "every route is auth-gated" invariant (test_main_routes.py guards the
*data* routes; these carry no user data, only static documents).

Repo-authoritative: the documents live in-tree under ``legal/`` and deploy with
the code whose behaviour they describe, rather than as host-side Caddy config
that can drift. Read once at import (the files ship in the image via
``COPY src ./src``); the editable install resolves them through ``__file__``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

_LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"
_PRIVACY_HTML = (_LEGAL_DIR / "privacy.html").read_text(encoding="utf-8")
_TERMS_HTML = (_LEGAL_DIR / "terms.html").read_text(encoding="utf-8")


@router.get("/privacy", response_class=HTMLResponse)
def privacy() -> str:
    """The Privacy Policy (public)."""
    return _PRIVACY_HTML


@router.get("/terms", response_class=HTMLResponse)
def terms() -> str:
    """The Terms of Use & Community Guidelines (public)."""
    return _TERMS_HTML
