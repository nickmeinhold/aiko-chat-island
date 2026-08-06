# SIGNING-SPEC — aikochat reaction signature v1

> **STATUS: PROPOSED (gateway-authored, awaiting app-tab co-authoring).** This is
> the gateway's proposal for the reaction signed-bytes interop contract, mirroring
> the frozen message spec (`aiko_chat_app` →
> `docs/crucible/sovereign-message-signing/SIGNING-SPEC.md`). It is NOT frozen. The
> **app tab is the signer and owns this contract** — the authoritative golden
> vector must be produced by the app's real Ed25519 signer, not by the gateway's
> reconstruction. The gateway ships a `reaction_signing_bytes()` reconstruction +
> golden-vector test that locks to whatever the app confirms here; that test is the
> drift-guard, exactly as `signing_bytes()` is for messages.
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

## Golden vector (PROPOSED — app tab must confirm from the real signer)

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

## Open decisions for the app tab (co-authoring checklist)

1. **Field ordering** — confirm the message-spine mirror above, or propose the
   handoff-prose ordering. (Gateway recommends the mirror.)
2. **`action` vocabulary** — `add` / `remove` as the two signed action strings.
   Confirm exact spelling (they are signed bytes; `add` ≠ `Add` ≠ `added`).
3. **Golden vector** — produce the authoritative vector from the app's real signer
   and confirm it byte-matches the proposed hex above (or amend). The gateway's
   test then locks to the confirmed vector.
