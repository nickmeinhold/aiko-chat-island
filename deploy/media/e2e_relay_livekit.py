#!/usr/bin/env python3
"""Prove media relays THROUGH the LiveKit embedded TURN, not just that TURN is up.

Both publisher and subscriber connect with ice_transport_type=TRANSPORT_RELAY, which
forbids host/srflx ICE candidates — so each client's only path to the SFU is its TURN
allocation. If the subscriber receives the publisher's video track under that constraint,
media traversed the TURN relay (twice: pub->TURN->SFU->TURN->sub). We then read get_rtc_stats() and confirm EVERY local ICE candidate gathered is
candidate_type=RELAY (a TURN allocation) — with relay-only policy there is no host/srflx
path — and surface the distinct relay transport/URL (udp/3478 vs tls/5349) so the report
states honestly which relay path carried the media.

Tokens are minted directly with the box's LiveKit API key/secret (isolates the media/relay
path from the gateway mint path, which v0.6.0 already proved). ice_servers is left empty:
LiveKit's SFU hands the client its session-bound embedded-TURN credentials in the join
response — that is the mechanism under test.

PROVEN 2026-08-11 against BOTH live islands on livekit-server v1.13.5:
RESULT=RELAY_MEDIA_OK, all_relay=true (1/1 local candidates were RELAY), transport
turn:<box>:3478?transport=udp (TRANSPORT_UDP). Run from an off-box external vantage.

This is the livekit-rtc engine for gate A of e2e_media_relay.py — the turnutils_uclient
approach there was wrong-premised (LiveKit's TURN is session-bound; there is no standalone
TURN credential to hand turnutils). e2e_media_relay.py invokes this harness and reads its
exit code + the structured RELAY_ASSERT line below; the harness OWNS the dual-leg relay-only
invariant (both pub and sub must gather candidates that are all RELAY, else it exits non-zero).

Env: LK_URL, LK_API_KEY, LK_API_SECRET, LK_ROOM (optional).
"""
import asyncio, os, sys, json, time
import numpy as np
from livekit import rtc, api

URL    = os.environ["LK_URL"]
KEY    = os.environ["LK_API_KEY"]
SECRET = os.environ["LK_API_SECRET"]
ROOM   = os.environ.get("LK_ROOM", f"relay-proof-{int(time.time())}")
W, H = 320, 240


def mint(identity: str, can_pub: bool, can_sub: bool) -> str:
    grants = api.VideoGrants(
        room_join=True, room=ROOM,
        can_publish=can_pub, can_subscribe=can_sub, can_publish_data=True,
    )
    return (
        api.AccessToken(KEY, SECRET)
        .with_identity(identity).with_name(identity)
        .with_grants(grants).to_jwt()
    )


def relay_opts():
    # ice_servers empty: the SFU supplies its session-bound embedded-TURN creds.
    return rtc.RtcConfiguration(ice_transport_type=rtc.IceTransportType.TRANSPORT_RELAY)


def _walk(stats_list):
    """Yield (kind, rtc_id, payload_message) for each stat in a publisher/subscriber list.

    livekit-rtc get_rtc_stats() returns RtcStats(publisher_stats=[...], subscriber_stats=[...])
    where each element is an RtcStats protobuf holding one set field (certificate,
    candidate_pair, local_candidate, remote_candidate, ...). Candidate details live under a
    nested `.candidate` submessage; the pair references candidates by their `rtc.id`.
    """
    for s in stats_list:
        rtc_id, kind, payload = None, None, None
        for fd, m in s.ListFields():
            if fd.name == "rtc":
                rtc_id = getattr(m, "id", None)
            else:
                kind, payload = fd.name, m
        yield kind, rtc_id, payload


def _enum_name(msg, field):
    """Decode a protobuf enum int field to its symbolic name (text-format shows names,
    but attribute access returns the int)."""
    val = getattr(msg, field, None)
    try:
        return msg.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number[val].name
    except Exception:
        return str(val)


def summarize_relay(stats_list):
    """Report the local ICE candidates gathered. Under relay-only policy every one must be
    candidate_type=RELAY (a TURN allocation) — that, plus a media round-trip, proves the
    media traversed the TURN server. We also surface the distinct relay transports/URLs so
    the report says honestly whether the path was UDP/3478 or TLS/5349."""
    locals_seen, relays = 0, []
    for kind, rtc_id, m in stats_list:
        if kind == "local_candidate":
            c = getattr(m, "candidate", m)
            locals_seen += 1
            ctype = _enum_name(c, "candidate_type")
            entry = {
                "type": ctype,
                "url": getattr(c, "url", ""),
                "relay_protocol": _enum_name(c, "relay_protocol"),
            }
            if "RELAY" in ctype.upper():
                relays.append(entry)
    distinct = sorted({(r["url"], r["relay_protocol"]) for r in relays})
    return {
        "local_candidates": locals_seen,
        "relay_candidates": len(relays),
        "all_relay": locals_seen > 0 and len(relays) == locals_seen,
        "relay_transports": [{"url": u, "relay_protocol": rp} for (u, rp) in distinct],
    }


async def dump_stats(room: rtc.Room, label: str):
    try:
        raw = await room.get_rtc_stats()
        walked = list(_walk(getattr(raw, "publisher_stats", []))) \
            + list(_walk(getattr(raw, "subscriber_stats", [])))
        info = summarize_relay(walked)
        print(f"[{label}] selected-pair: {json.dumps(info, default=str)}", flush=True)
        return info
    except Exception as e:
        print(f"[{label}] stats unavailable: {e!r}", flush=True)
        return None


async def main():
    pub_room, sub_room = rtc.Room(), rtc.Room()
    got = asyncio.Event()
    result = {}

    @sub_room.on("track_subscribed")
    def _on_sub(track, publication, participant):
        result["track"] = (str(track.kind), publication.sid, participant.identity)
        print(f"[sub] track_subscribed kind={track.kind} from={participant.identity}", flush=True)
        got.set()

    await sub_room.connect(URL, mint("relay-sub", False, True),
                           options=rtc.RoomOptions(auto_subscribe=True, rtc_config=relay_opts()))
    print(f"[sub] connected id={sub_room.local_participant.identity} room={sub_room.name} (RELAY-only)", flush=True)

    await pub_room.connect(URL, mint("relay-pub", True, False),
                           options=rtc.RoomOptions(auto_subscribe=False, rtc_config=relay_opts()))
    print(f"[pub] connected id={pub_room.local_participant.identity} room={pub_room.name} (RELAY-only)", flush=True)

    source = rtc.VideoSource(W, H)
    track = rtc.LocalVideoTrack.create_video_track("relay-cam", source)
    pubn = await pub_room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA))
    print(f"[pub] published sid={pubn.sid}", flush=True)

    argb = np.zeros((H, W, 4), dtype=np.uint8)
    argb[:, :, 0] = 255; argb[:, :, 2] = 255; argb[:, :, 3] = 255
    buf = argb.tobytes()

    async def pump():
        while not got.is_set():
            source.capture_frame(rtc.VideoFrame(W, H, rtc.VideoBufferType.RGBA, buf))
            await asyncio.sleep(1/15)
    pump_task = asyncio.create_task(pump())

    rc = 2
    try:
        await asyncio.wait_for(got.wait(), timeout=30)
        # let media settle so stats have gathered candidates
        await asyncio.sleep(2)
        print("RESULT=RELAY_MEDIA_OK", result.get("track"), flush=True)
        # HARNESS OWNS THE RELAY-ONLY INVARIANT (cage-match #128, Carnot/Tesla):
        # media receipt alone is NOT sufficient — BOTH legs must have gathered
        # candidates and every one must be RELAY. Stats unavailable on EITHER leg is
        # FATAL (can't confirm relay-only) → fail closed, never rc=0. The parent gate
        # reads the structured RELAY_ASSERT line + this exit code, not a stdout grep.
        pub_info = await dump_stats(pub_room, "pub")
        sub_info = await dump_stats(sub_room, "sub")
        def _leg_ok(info):
            return bool(info) and info.get("local_candidates", 0) > 0 and info.get("all_relay") is True
        pub_ok, sub_ok = _leg_ok(pub_info), _leg_ok(sub_info)
        assertion = {"result": "OK" if (pub_ok and sub_ok) else "FAIL",
                     "pub_all_relay": pub_ok, "sub_all_relay": sub_ok}
        print("RELAY_ASSERT=" + json.dumps(assertion), flush=True)
        if pub_ok and sub_ok:
            rc = 0
        else:
            print("RESULT=RELAY_ASSERT_FAILED both legs must be relay-only with gathered candidates", flush=True)
            rc = 3
    except asyncio.TimeoutError:
        print("RESULT=TIMEOUT_NO_TRACK_UNDER_RELAY_ONLY", flush=True)
        await dump_stats(pub_room, "pub")
        await dump_stats(sub_room, "sub")
    finally:
        pump_task.cancel()
        await pub_room.disconnect()
        await sub_room.disconnect()
    return rc

sys.exit(asyncio.run(main()))
