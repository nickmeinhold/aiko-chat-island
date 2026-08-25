"""Device-token registration endpoints (#16, increment 1).

The app POSTs its APNs/FCM push token here after login so the gateway knows where
to deliver notifications (the sending itself is increment 2). I1 (auth): both
routes take ``CurrentUser`` — an unauthenticated caller is rejected before any row
is touched, and the token is always bound to the AUTHENTICATED user, never a
user_id from the client body (the same server-derives-identity discipline as
messages.sender_user_id / invariant I5).
"""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from ..domain import devices_service as svc
from ..domain.models import Platform, PushEnvironment
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
    push_environment: PushEnvironment | None = None


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
        push_environment=req.push_environment)
    # Echo the RESOLVED environment, not the requested one: a client that sent
    # nothing learns what the island picked for it, which is the only way it can
    # notice a mismatch with the build it actually is.
    return {"id": row.id, "platform": row.platform,
            "push_environment": row.push_environment}


@router.delete("/devices", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    req: UnregisterDeviceReq, user: CurrentUser, session: DbSession
) -> None:
    """Unregister a device token (app logout). 204 whether or not the token was
    present — unregistering an unknown token is not an error (idempotent), and a
    404 would leak whether a given token is registered."""
    await svc.unregister_device(session, user_id=user.id, token=req.token)
