# SIGNING-SPEC — aikochat reaction signature v1

> **STATUS: GROUNDED (verified against the app's committed signer, 2026-08-07).**
> The layout below is not a blind proposal — it is a faithful mirror of the app's
> **shipped** message signer (`aiko_chat_app` →
> `lib/features/chat/domain/message_signing.dart:69-102`), whose field order
> (tag · pubkey · channel_id · client_msg_id · u64 signed_at_ms · content…) this
> spec reproduces exactly, swapping only the content fields + the domain tag.
>
> **The golden vector is authoritative by construction, not "pending the app's
> signer."** The *message* golden vector already proves the gateway's Python
> `lp()`/`struct.pack(">Q")` machinery and the app's Dart `lengthPrefixed`/
> `setUint64` machinery emit byte-identical output (green CI test today). Those
> primitives are field-content-agnostic — they don't branch on which field — so the
> proof transfers to any sequence of the same primitives, including this one. The
> reaction vector therefore inherits the message vector's *external* known-answer
> anchor; it is not self-referential.
>
> **App-side residual is mechanical, not a decision:** implement the reaction signer
> as the message signer with the tag + content fields swapped (the obvious mirror),
> and pin a golden-vector test to the same hex below (red on drift — the backstop).
> The only genuine ratifications left are naming-level (the `add`/`remove` action
> strings and the emoji canonicalization rule, both below), not the byte layout.
>
> **Why this exists / why signed from day one:** #2634 first shipped reactions
> *unsigned + anonymous* and was reverted. A reaction is a **signed lightweight
> endorsement** (raw material for the Carried Record, #2506) — and you cannot sign
> history retroactively, so the signature is captured from the first reaction or
> never. A reaction signed over the *wrong bytes* is just as permanently
> unverifiable as an unsigned one, which is why the byte layout must be co-agreed
> **before either side ships a signer.**

## Relationship to the message spec

This is the message signing spec with the **content fields swapped** and a
**distinct domain tag**. Fields 1–5 are byte-for-byte the same roles/encoding as a
signed message, so an app-side signer reuses its existing `signingBytes` spine and
only changes the tag + the trailing content fields. A verifier that already
reconstructs message bytes needs ~3 lines of delta.

## Algorithm

Identical to messages: **Ed25519** (RFC 8032), detached raw 64-byte `R‖S`,
deterministic, canonical-`S` non-malleable. See the message SIGNING-SPEC for the
full rationale — nothing changes here.

## The signed bytes — `reactionSigningBytes(payload)`

Hand-built, **length-prefixed, domain-separated**. Every variable-length field is
preceded by a **fixed-width big-endian u32** byte-length. Concatenation order is
fixed:

| # | Field | Encoding |
|---|-------|----------|
| 1 | domain tag `aikochat:react:v1:EdDSA` | u32-len ‖ UTF-8 bytes |
| 2 | sender public key | u32-len ‖ **raw 32 bytes** (NOT Multikey — raw here) |
| 3 | `channel_id` | u32-len ‖ UTF-8 |
| 4 | `client_msg_id` (the reaction's OWN id) | u32-len ‖ UTF-8 |
| 5 | `signed_at_ms` | **u64 big-endian** (no length prefix; fixed width) |
| 6 | `target_msg_id` (the message being reacted to) | u32-len ‖ UTF-8 |
| 7 | `emoji` | u32-len ‖ UTF-8 |
| 8 | `action` (`add` \| `remove`) | u32-len ‖ UTF-8 |

**Why each field:**
- **domain tag** binds app + algorithm AND the *event class*. It is
  `aikochat:react:v1:EdDSA`, deliberately **distinct** from the message tag
  `aikochat:msg:v1:EdDSA`. Domain separation is security-critical: without it a
  message signature could be lifted and re-presented as a reaction endorsement (or
  vice versa) — cross-event signature replay. Different tag → different signed
  bytes → the two structures are cryptographically non-interchangeable.
- **raw pubkey** inside the bytes defends key-substitution (identical to messages).
- **channel_id** stops cross-channel replay of the same reaction.
- **client_msg_id** is the reaction's own stable, verifier-reconstructable id
  (distinct from `target_msg_id`). It is what the gateway binds `origin.client_msg_id`
  against and what the reaction is stored under (idempotency key).
- **signed_at_ms** is the compose-time clock, persisted independently of the
  server-side timestamp (identical role to messages).
- **target_msg_id** binds *which message* was endorsed — the reaction's content.
- **emoji** is the endorsement itself.
- **action** is signed too, so a `remove` (un-vouch) is its **own non-repudiable
  event**, not an unsigned retraction of a signed one. A signed add + a signed
  remove are two independently-attestable facts.

**Length-prefixing is load-bearing** (same argument as messages): without it,
`emoji="👍", action="add"` and `emoji="👍a", action="dd"` would sign identical
bytes.

### Field-ordering note (resolves a drift in the v2-social handoff prose)

The `HANDOFF-to-app-tab-v2-social-wire.md` prose sketched the reaction bytes as
`DOMAIN_TAG ‖ channel_id ‖ target_msg_id ‖ emoji ‖ action ‖ pubkey ‖ client_msg_id
‖ signed_at_ms`. **This spec deliberately does NOT follow that ordering** — it puts
the pubkey near the end and the timestamp last, which diverges from the message
spec it claims to mirror. This spec instead preserves the message spec's exact
spine (tag, pubkey, channel_id, client_msg_id, signed_at_ms first, in that order)
and appends the reaction content, so the app signer reuses its message-signing code
path unchanged. **App tab: confirm this ordering or propose the alternative — this
is the one open decision, and it must be pinned before either signer ships.**

## Golden vector (authoritative by construction — app pins the same hex as a backstop)

Fixture (mirrors the message vector's style):
- `sender_pubkey` = raw 32 bytes `00 01 02 … 1f`
- `channel_id` = `chan-1`
- `client_msg_id` = `rxn-abc`
- `signed_at_ms` = `1720000000000`
- `target_msg_id` = `msg-xyz`
- `emoji` = `👍` (U+1F44D, UTF-8 `f0 9f 91 8d`)
- `action` = `add`

`reactionSigningBytes` (hex), field-by-field:
```
0000001761696b6f636861743a72656163743a76313a4564445341    #1 domain tag
00000020 000102…1f (raw 32)                                #2 pubkey
000000066368616e2d31                                       #3 channel_id 'chan-1'
0000000772786e2d616263                                     #4 client_msg_id 'rxn-abc'
0000019077fd3000                                           #5 signed_at_ms u64
000000076d73672d78797a                                     #6 target_msg_id 'msg-xyz'
00000004f09f918d                                           #7 emoji U+1F44D
00000003616464                                             #8 action 'add'
```

Concatenated:
```
0000001761696b6f636861743a72656163743a76313a45644453410000002000010203
0405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f000000066368616e
2d310000000772786e2d6162630000019077fd3000000000076d73672d78797a00000004
f09f918d00000003616464
```
(A signer/verifier that produces different bytes for this fixture is
non-conformant. **The gateway's `reaction_signing_bytes()` pins exactly these
bytes; the app confirms its signer reproduces them.**)

## Wire envelope (reuse the message `origin` shape verbatim)

A reaction carries the SAME `origin` envelope as a signed message — same keys, same
shapes, same gateway carriage rules:
```json
"origin": {
  "v": 1,
  "alg": "EdDSA",
  "key_version": 1,
  "sender_pubkey": "z6Mk…",       // Multikey multibase on the wire; raw 32 recovered per the message spec's extraction rule
  "client_msg_id": "rxn-…",        // the REACTION's own id, echoed for reconstruction
  "signed_at_ms": 1720000000000,
  "sig": "base64url-unpadded-raw-64"
}
```

**Gateway carries, does not verify** (identical posture to messages, `signing.py`):
validate the envelope shape at the trust boundary, bind `origin.client_msg_id ==
the reaction frame's client_msg_id`, persist, echo verbatim. Absent/garbage origin
= "unverified", never "invalid" — a reaction may be sent unsigned (legacy/degraded
client) and is still carried; it simply isn't reputation-grade. The gateway never
reconstructs `reactionSigningBytes` on the carry path; the reconstruction exists
only as the golden-vector drift-guard test.

**Reputation caveat** (same as messages): a shape-valid signature attests "*some*
key signed these bytes", NOT "*this account's* key". The pubkey→account binding is
#1816 PR B (`signing_keys`). Signing now captures the bytes so they become
reputation-grade retroactively when that trust root lands.

## Residual for the app tab (mechanical + two naming ratifications)

Field ordering + the golden vector are **settled** (grounded against
`message_signing.dart`, authoritative by construction — see STATUS). What's left:

1. **[mechanical] Implement the reaction signer** as the message signer with the
   tag swapped to `aikochat:react:v1:EdDSA` and `body`/`reply_to` → `target_msg_id`/
   `emoji`/`action`, and **pin a Dart golden-vector test to the hex above** (red on
   drift — the cross-language backstop, mirroring `message_signing_test.dart`).
2. **[ratify] `action` vocabulary** — gateway sets `add` / `remove` (signed bytes,
   so spelling is load-bearing: `add` ≠ `Add` ≠ `added`). Object only if you need
   different strings.
3. **[ratify — genuine interop question] emoji canonicalization.** An emoji can have
   multiple valid UTF-8 encodings (variation selectors, skin-tone modifiers, ZWJ
   sequences), so two clients could sign "the same" emoji as *different bytes* →
   different signatures + a broken idempotency key. **Gateway's proposed rule:** the
   signed `emoji` bytes are the exact UTF-8 the client sends, **no normalization**;
   the idempotency key is `(user, target_msg_id, exact-emoji-bytes)`. This keeps the
   gateway a pure carrier (it never re-encodes signed content) and pushes any
   picker-level normalization app-side, before signing. Ratify or propose NFC.
