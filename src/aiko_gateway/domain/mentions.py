"""@-mention span carriage (#2632) — the gateway's carrier role for mentions.

The client sends key-bound mention spans on the WS ``send`` frame; the gateway
SHAPE-validates + caps them at this trust boundary, then persists + echoes them
VERBATIM (messages.mentions, message_view). It is a pure CARRIER, not a resolver
and not a filter: it never looks a target up, never re-derives the client's offset
basis, never rewrites or drops a span. This mirrors ``signing.validate_origin`` —
same fail-closed discipline (exact types, reject bool-as-int, no extra keys, size
caps, a charset guard, a FRESH projection so a later mutation of the inbound frame
can't reach stored JSON) — but there is no signature here, only structure.

WHAT ``target_id`` IS (pinned — an earlier draft wrongly called it "the Ed25519
key"): the gateway's OPAQUE, STABLE user identifier — exactly the ``user_id`` the
member roster (``GET /channels/{id}/members``) hands the composer. The client
re-resolves it to the *current* handle at render time, so a rename never orphans a
mention (ADR-0004: mention targets key off the opaque identity, never a
home-qualified string). It is NOT the raw signing pubkey and NOT a handle string.
This is why the gateway can stay resolver-free — it never has to map the id to
anything; it just carries it and the client resolves it against the roster/user
cache it already holds. A federated cross-island identity key is a future layer
(ADR-0004's IdentityDoc, not built); today the target is this island's user id.

WHY NO BLOCK / MODERATION GATE HERE: a mention span is inert carrier metadata.
Block enforcement lives at the INTERACTION layer (the "you were @'d" notification,
#2526), not at carriage — the message body carrying the span is already
block-excluded from every delivery path (live fanout + ``get_history``), so a
stored span reaches no blocked user through any read path; only a notification
could, and that layer must block-filter. Enforcing here would force the gateway to
resolve ``target_id``'s identity and recognize a reserved ``target_type`` value,
breaking the value-opaque, resolver-free carrier contract. See the send handler.

Why ``target_type`` is NOT value-enumerated: the /v1 wire is append-only because
the gateway and app deploy independently (no shared cadence). A strict
``{user,channel,everyone}`` enum on the gateway would force a gateway deploy the
day the app adds a new target kind. So we validate the DISCRIMINATOR's shape and
carry its value opaquely — the app owns the semantics.

Why offsets are NOT bounds-checked against ``body``: the offset basis is UTF-16
code units (the app's declared, Dart-native basis); Python indexes code points, so
a gateway bounds-check would have to re-derive the app's basis — exactly the
coupling the "opaque round-trip" contract avoids. We validate that offset/length
are sane, jointly-bounded integers, not that they land inside this particular body.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, TypedDict

# Caps — a message carrying thousands of spans is a storage/DoS vector, not a real
# mention. Generous vs any real composer, tight vs abuse.
_MAX_MENTIONS = 64
# Headroom for the opaque user id: a ULID is 26 chars; a future federated identity
# id could be longer, so 128 leaves room without inviting a blob.
_MAX_TARGET_ID = 128
# target_type shape only — value stays opaque. Lowercase snake_token: the app's known
# kinds (user/channel/everyone) satisfy it; a NEW kind must stay within this charset,
# a deliberate shape contract (not a value enum). A camelCase kind is a wire break the
# app and gateway must agree on, so shape (not value) is the right place to pin it.
_TARGET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# offset/length are bounded well above any real UTF-16 index in a size-capped body
# (#28), purely to reject absurd integers; their SUM is also bounded so a span can't
# claim an end at ~2Mi via two in-range axes (shape hygiene, not basis re-derivation).
_MAX_INDEX = 1 << 20

_SPAN_KEYS = frozenset({"target_type", "target_id", "offset", "length"})


class MentionSpan(TypedDict):
    """The fixed four-key mention span carried on a message (#2632). The key SET is
    frozen forever (the append-only wire extends via new target_type VALUES, never
    new span keys), so it is a named type, not a bare ``dict``. What
    ``validate_mentions`` returns and ``messages.mentions`` stores."""
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


# The "not real text" Unicode categories an opaque MACHINE id never legitimately
# contains: Control, Format (bidi/zero-width — the spoof vector isprintable() MISSES),
# Surrogate, Private-Use, and Unassigned (a future Unicode version could reclassify a
# Cn codepoint under a stored id's feet). Rejecting the whole set closes the
# smuggling/garbage channel without dictating the id's script or format — charset
# shape hygiene (like origin validating sig is base64url), not content resolution.
_FORBIDDEN_ID_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _is_clean_id(s: str) -> bool:
    return all(unicodedata.category(ch) not in _FORBIDDEN_ID_CATEGORIES for ch in s)


def validate_mentions(raw: Any) -> list[MentionSpan] | None:
    """Validate an inbound ``mentions`` value to a fresh list of clean span dicts,
    or raise :class:`MentionError`. ``None``/absent is legal (a message with no
    mentions) and returns ``None`` so message_view omits the key. An empty list is
    structurally valid and returns ``[]``; the persistence layer normalizes empty →
    ``None`` so storage, view, and wire carry ONE representation of "no mentions".

    Spans are carried VERBATIM — including exact duplicates (a pure carrier does not
    rewrite the client's array; the ``_MAX_MENTIONS`` cap is the wall against bloat,
    not a dedup pass, which would make ``mentions`` unlike the ``origin`` it mirrors).
    The returned spans are FRESH dicts with exactly the four span keys in a stable
    field order — so what we persist/echo cannot be reached by a later mutation of
    the caller's frame, and never carries an unexpected key the client tucked in.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise MentionError("mentions must be a list")
    if len(raw) > _MAX_MENTIONS:
        raise MentionError(f"mentions exceeds the {_MAX_MENTIONS}-span cap")

    out: list[MentionSpan] = []
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
        if not _is_clean_id(tid):
            raise MentionError("target_id must not contain control/format characters")

        offset = span["offset"]
        if not _is_int(offset) or not (0 <= offset <= _MAX_INDEX):
            raise MentionError(f"offset must be an int in [0, {_MAX_INDEX}]")

        length = span["length"]
        if not _is_int(length) or not (1 <= length <= _MAX_INDEX):
            raise MentionError(f"length must be an int in [1, {_MAX_INDEX}]")
        if offset + length > _MAX_INDEX:
            raise MentionError(f"offset + length must not exceed {_MAX_INDEX}")

        out.append(MentionSpan(
            target_type=ttype, target_id=tid, offset=offset, length=length))
    return out
