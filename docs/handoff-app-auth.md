# Handoff → aiko_chat_app: auth work the gateway is waiting on

**From:** aiko-chat-island gateway session, 2026-07-05
**Audience:** an app-repo (`aiko_chat_app`) coding session
**One-line:** the gateway is *ahead* of the app on every open auth item — this is
a list of app-side changes to consume contracts that are already shipped and live
on both islands. Nothing here is blocked on the gateway.

Gateway endpoints referenced are verified against
`../aiko-chat-island/src/aiko_gateway/rest/auth.py` at this date. App insertion
points are `aiko_chat_app/lib/features/chat/data/gateway_rest_api.dart` unless
noted.

---

## Priority order

1. **Wire `passkey/add/finish`** — converts already-deployed dead code into the
   fix for the passkey-401 collision. Highest leverage. (app #1727 / gw #1728)
2. **`associated-domains: webcredentials:enspyr.co`** — hard prerequisite;
   passkeys *cannot* work on island 2 without it. (app #1722 / #1550)
3. **Fetch `/nonce` before `/social`** — turns on replay defense that is inert
   today. (app-side of gw #13 / #1449)
4. **Discover from the current gateway, not a hardcoded URL** — removes a
   discovery SPOF. (app #1672)

---

## 1. Passkey add-to-existing-account — `POST /v1/auth/passkey/add/finish`

### Why
First-passkey-creates-account collides when the user *already* has an account
(typically a prior social sign-in). The old path forced them through
`register → claim`, where a handle conflict with their **own** account rejected
the claim and orphaned the device credential permanently — `passkey_credentials`
stayed empty and the next passkey sign-in 401'd. The gateway now has a direct
link path.

### Contract
```
POST /v1/auth/passkey/add/finish
Authorization: Bearer <access_token>          ← the ONLY wire difference from register/finish
Body:  { "state": "<from register/start>", "credential": { <WebAuthn JSON> } }

200 → a bare USER object  (NOT an auth outcome — no tokens, no provisioning_token)
400 → invalid/expired challenge
401 → attestation verification failed
409 → passkey already registered
```

- **Challenge:** reuse the existing `POST /v1/auth/passkey/register/start` — it is
  identity-agnostic. There is **no** `add/start`. The `Authorization` bearer on
  `add/finish` is what distinguishes an add from a first-passkey register.
- **Flow:** authed user taps "Add a passkey" → `register/start` → device creates
  credential → `add/finish` **with the bearer** → gateway persists the credential
  directly against `user_id` and returns their user view.

### ⚠️ The trap that will bite you
`add/finish` returns a **bare user object** (`_user_view(user)`), *not* a
`SocialOutcome`. The existing `_passkeyFinish` helper (line ~167) routes every
response through `_resolveOutcome`, which **throws**
`"neither access_token nor provisioning_token"` on a bare user. You **cannot**
reuse `_passkeyFinish` for this call.

### App changes
- Add a new method (do **not** extend `_passkeyFinish`):
  ```dart
  @override
  Future<User> addPasskey(String state, String credentialJson) async {
    final credential = jsonDecode(credentialJson);      // same decode as _passkeyFinish
    final r = await _authed.post('/v1/auth/passkey/add/finish',
        data: {'state': state, 'credential': credential});
    return User.fromJson(_map(r.data));                 // parse the `user` shape directly
  }
  ```
  - Use **`_authed`** (line 76), not `_bare` — the `AuthInterceptor` must attach
    the bearer or the gateway can't identify the user.
  - Parse with the same model the app already uses for the `user` sub-object of
    `AuthSession` (authenticate/finish returns `{...tokens, "user": <that shape>}`;
    `add/finish` returns just that shape at top level).
  - Map `409` → a "passkey already on this account" UX, not a generic auth error.
- Start reuses the existing `startPasskeyRegistration()` (already wired, line 129).
- Surface it in the authed/settings UI ("Add a passkey"), distinct from the
  logged-out "Create a passkey" register path.

---

## 2. Associated-domains for island 2 (`enspyr.co`)

Add `webcredentials:enspyr.co` to the iOS + macOS associated-domains entitlement
(alongside the existing `imagineering.cc`). Without it the platform authenticator
refuses to create/use a passkey scoped to island 2 — this is a build-config
prerequisite, independent of any Dart. (app #1722 / #1550)

The gateway already serves the matching `/.well-known/apple-app-site-association`
and sets `PASSKEY_RP_ID` per island.

---

## 3. Turn on nonce replay defense — `POST /v1/auth/nonce`

### Why
The native social flow (`/social`) accepts an app-supplied `nonce`, but the app
never fetches a gateway-issued one, so a captured `/social` request is currently
replayable. The gateway can issue a single-use nonce it redeems exactly once.

### Contract
```
POST /v1/auth/nonce            (pre-auth, no body)
200 → { "nonce": "<opaque>" }
403 → social sign-in disabled
```

### App changes
1. Call `/v1/auth/nonce` **first**, before invoking the platform sign-in.
2. Feed the returned nonce into the Sign-in-with-Apple / Google request
   (Apple wants the **SHA-256 hash** of it; Google takes it raw).
3. Echo the **raw** nonce to `/v1/auth/social` in the existing `nonce` field.

Once the app ships this, the gateway flips `social_nonce_required=true` (one
config flag) to make a missing nonce a hard reject. Until then the gateway
tolerates missing nonces, so shipping the app change is safe and non-breaking.

---

## 4. Resilient gateway discovery (app #1672)

Discover peers from the **current** gateway's `GET /v1/gateways` (live on both
islands) plus a plural persisted seed list — not a single hardcoded
`GATEWAY_DIRECTORY_URL`, which is itself a discovery SPOF. Every gateway is a
complete directory; there is no special host. Persist discovered peers so a
cold start has more than one bootstrap contact.

---

## Already shipped & live on the gateway (no action needed)

- `add/finish`, `/nonce`, `/v1/gateways`, passkey advertisement via
  `GET /v1/auth/providers`, `register/*`, `authenticate/*`, `/social`,
  `/social/claim`, the OAuth broker (`/oauth/*`) — all deployed on
  `chat.imagineering.cc` and `chat.enspyr.co`.

## Needs upstream (Andy), not the app

- Real USER lists require ChatServer to publish a `user_list` EC share
  (`aiko_services`, gw #1304). The app can't work around this and neither can we.
