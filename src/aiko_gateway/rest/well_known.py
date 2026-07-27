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
    """Android Digital Asset Links for passkeys. The sha256 fingerprint is the
    Play App Signing cert (app task #20). Until it is configured we serve an EMPTY
    document rather than a target with no fingerprints — a fingerprint-less target
    can never verify, so publishing it is a negative/malformed association artifact
    that a client might cache (cage-match #38, Carnot). Once configured, the target
    appears.

    BOTH relations are required. `get_login_creds` alone passes Google's
    `assetlinks:check` API (so the association *looks* valid), but GMS's on-device
    passkey `ValidateRpIdOperation` ADDITIONALLY requires `handle_all_urls` — its
    absence fails registration with `[50152] RP ID cannot be validated` on every
    device, even a certified one, even though every other input is correct. This
    was the root cause of the Android passkey-create block (2026-07-28); Google's
    own "seamless credential sharing" codelab documents `handle_all_urls` as a
    "strict requirement" for cross-platform passkeys."""
    if not settings.passkey_android_cert_sha256:
        return []
    return [{
        "relation": [
            "delegate_permission/common.get_login_creds",
            "delegate_permission/common.handle_all_urls",
        ],
        "target": {
            "namespace": "android_app",
            "package_name": settings.passkey_android_package,
            "sha256_cert_fingerprints": settings.passkey_android_cert_sha256,
        },
    }]
