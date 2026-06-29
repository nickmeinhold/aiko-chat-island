"""Domain-association files for WebAuthn passkeys (#1471).

Served at the APEX /.well-known/ path (the router has NO prefix) so iOS and
Android can verify the app's right to use passkeys on this domain. Public, static,
unauthenticated; served ALWAYS (the app verifies association before passkey_enabled
flips advertisement on). Reverse proxy (Caddy) passes /.well-known/ through to the
app.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..config import settings

router = APIRouter(tags=["well-known"])


@router.get("/.well-known/apple-app-site-association")
def apple_app_site_association() -> dict:
    """iOS associated-domains (webcredentials) so the app may use passkeys scoped to
    this domain. application/json, NO redirect — iOS fetches it directly and rejects
    a redirect."""
    return {"webcredentials": {"apps": [settings.passkey_ios_app_id]}}


@router.get("/.well-known/assetlinks.json")
def android_asset_links() -> list[dict]:
    """Android Digital Asset Links (get_login_creds). The sha256 fingerprint is the
    Play App Signing cert (app task #20) — empty until registered, so Android App
    Link verification is pending while iOS works."""
    return [{
        "relation": ["delegate_permission/common.get_login_creds"],
        "target": {
            "namespace": "android_app",
            "package_name": settings.passkey_android_package,
            "sha256_cert_fingerprints": settings.passkey_android_cert_sha256,
        },
    }]
