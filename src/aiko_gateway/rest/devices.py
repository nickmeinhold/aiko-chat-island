"""Device-token registration endpoints (#16, increment 1).

The app POSTs its APNs/FCM push token here after login so the gateway knows where
to deliver notifications, and says what each token is FOR. I1 (auth): both
routes take ``CurrentUser`` — an unauthenticated caller is rejected before any row
is touched, and the token is always bound to the AUTHENTICATED user, never a
user_id from the client body (the same server-derives-identity discipline as
messages.sender_user_id / invariant I5).
"""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from ..domain import devices_service as svc
from ..domain.models import Platform, ApnsEnvironment, TokenKind
from .deps import CurrentUser, DbSession

router = APIRouter(prefix="/v1", tags=["devices"])


class RegisterDeviceReq(BaseModel):
    # Platform typed as the enum so an out-of-set value is a 422 at the boundary,
    # not a silent store that the DB CHECK would later reject with a 500.
    platform: Platform
    token: str = Field(min_length=1, max_length=512)
    # Which APNs environment minted this token (#3386). OPTIONAL, and None means
    # "use this island's setting" — so an app built before this field existed
    # keeps working unchanged and the island half ships without waiting for the
    # app half. Typed as the enum so an out-of-set value is a 422 at the boundary
    # rather than a 500 from the DB CHECK, same as `platform`. Inert for 'fcm'.
    apns_environment: ApnsEnvironment | None = None
    # What this token is FOR (design 12 Decision 2) — `alert` for a UIKit
    # remote-notification token, `voip` for a PushKit one. OPTIONAL, and None means
    # "the client did not say", resolved in the SERVICE (one door) rather than
    # here. Absent means alert, so an app built before this field existed keeps
    # working unchanged and every existing row already means what it says.
    #
    # A DIFFERENT AXIS FROM `platform`, not a refinement of it: `platform` is which
    # transport family, this is which delivery semantics. An `apns_voip` platform
    # value would be unrepresentable for Android.
    #
    # Typed as the enum so an out-of-set value is a 422 at the boundary rather than
    # a 500 from the DB CHECK, same as `platform`. Inert for 'fcm', and there is
    # deliberately NO cross-field rejection of platform=fcm + token_kind=voip: it
    # prevents nothing (the FCM branch never reads the kind), it contradicts the
    # inert-for-FCM precedent `apns_environment` already set, and a rejected
    # registration is the most complete silence available — no row, no wake-time
    # log, no reachability entry.
    token_kind: TokenKind | None = None


class UnregisterDeviceReq(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/devices", status_code=status.HTTP_201_CREATED)
async def register_device(
    req: RegisterDeviceReq, user: CurrentUser, session: DbSession
) -> dict:
    """Register (or re-register) this device's push token for the current user.
    Idempotent: re-registering the same token is a no-op reassign, still 201."""
    row = await svc.register_device(
        session, user_id=user.id, platform=req.platform.value, token=req.token,
        apns_environment=req.apns_environment, token_kind=req.token_kind)
    # Echo the RESOLVED environment and kind, not the requested ones: a client that
    # sent nothing learns what the island picked for it, which is the only way it
    # can notice a mismatch with the build it actually is.
    #
    # `token_kind` IS ALSO THE DESYNC DETECTOR, and it is the only one available.
    # Measured before this shipped: this model SILENTLY DROPPED an unknown
    # `token_kind` (pydantic's default extra="ignore"), so an app shipping the
    # field against an island that had not deployed it got a 201, the field
    # vanished, the row stored 'alert', and every VoIP push then went to a token
    # that is not a VoIP token — a 400, never reaped, no error, no ring. The echo
    # is how the app learns it sent `voip` and got back `alert`.
    return {"id": row.id, "platform": row.platform,
            "apns_environment": row.apns_environment,
            "token_kind": row.token_kind}


@router.delete("/devices", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    req: UnregisterDeviceReq, user: CurrentUser, session: DbSession
) -> None:
    """Unregister a device token (app logout). 204 whether or not the token was
    present — unregistering an unknown token is not an error (idempotent), and a
    404 would leak whether a given token is registered."""
    await svc.unregister_device(session, user_id=user.id, token=req.token)
