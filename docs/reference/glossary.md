# Glossary

Terms that come up in this project, written for someone who wants to hold the
system rather than be routed around it.

**How to read the tier tag.** Every entry that describes our system carries one,
because "how it works" and "how we decided it should work" are different claims
and confusing them has bitten us:

- **[BUILT]** — in the code, deployed, running on both islands right now.
- **[DECIDED]** — a recorded decision. Not in the code yet.
- **[PROPOSED]** — on the table, nobody has ruled.
- *(no tag)* — a general industry term, not a fact about us.

---

## Cryptography

### Public / private key pair
Two mathematically linked keys. The **private** key stays with the holder; the
**public** key is handed out freely. Anything signed with the private key can be
verified by anyone holding the public key. A public key is *not a secret* — that
single fact is what makes [proof of possession](#proof-of-possession-pop) necessary.

### Proof of possession (PoP)
Making someone **prove they hold the private key** before you record the public
one as theirs. The island sends a random challenge, the client signs it with the
private key, the island verifies against the public key.

Without it, a key row means *"this account used this key"*. With it, the row means
*"this account holds this key"*. Those look identical in the database and are
completely different claims, because anyone can copy anyone's public key off a
message and present it as their own.

**[DECIDED, not built]** — #1816 T-series. Our `signing_keys` table today is an
observed-usage roster, not an ownership claim. This is exactly where the external
specialist's caveat landed ("assuming it binds users to public encryption keys").

### Passkey / WebAuthn
A login credential whose private key is generated *inside* the phone's secure hardware
(Secure Enclave, Android Keystore) and **cannot be extracted**. Login is a
challenge-response: the island sends a challenge, the phone signs it, the island
verifies. **[BUILT]** — `passkey_challenge_ttl_seconds`, 5 minutes.

This IS proof of possession, performed at registration and again at every login. Note
what it proves: *the holder of this credential is here*. It says nothing about any other
key the same user might hold.

### The two key pairs (and why the sign-up challenge doesn't cover both)
A user has **two independent key pairs that do not know about each other**:

1. **The passkey credential** — hardware-bound, challenged every login. **[BUILT]**
2. **The Ed25519 message-signing key** — app-generated in software, and the island first
   learns of it as a *side effect of a message arriving*. No ceremony, nothing proven.
   **[BUILT, unproven]**

So sign-up proves key 1 and says nothing about key 2. The obvious fix, and it is cheap:
present the signing public key **during the passkey ceremony** and have the client sign
the same challenge with both keys. One round trip already being paid for, both keys proven
at once, tied to the same account at the same moment. **[PROPOSED — Nick, 2026-09-20]**

### Nonce
A random value used exactly once, so a captured message cannot be replayed later.
"Number used once". The challenge in PoP is a nonce. **[BUILT]** — `nonce_service.py`
handles social sign-in nonces.

### Ed25519
The signature algorithm we use. Fast, small keys (32 bytes), modern. **[BUILT]** —
the island allowlists `alg: "EdDSA"` server-side and never trusts the algorithm
named in the envelope, which is a standard defence against an attacker downgrading
you to a weak algorithm by simply asking.

### X25519
The *key-exchange* algorithm from the same family as Ed25519. Used to agree a shared
secret for encryption, where Ed25519 signs. Their private keys are mathematically
close enough that one key could technically do both jobs — and the specialist's
verdict was **don't**: if the protocol has a flaw, a key usable for both lets an
attacker confuse a signature for an encryption operation. **[PROPOSED]** — app
design 20 §4a; the two-key answer is the recorded advice.

### Multikey
A W3C format for writing a public key as one self-describing string: a prefix says
which algorithm it is, then the raw key bytes, all base58-encoded. **[BUILT]** —
this is the format in our `signing_keys` table. It matters that the algorithm is
*inside* the key: a two-key split would not need us to invent a "what is this key
for" field, because the bytes already say.

### Signature stripping
Sending a message with the signature omitted. **Absent is legal on our wire**, which
is why the app silently sending unsigned messages to `chat.enspyr.co` for seven weeks
produced no error anywhere. **[BUILT]** — fixed in v0.14.0 by `GET /capabilities`, so
no client carries a hardcoded list of which hosts to sign for.

---

## Push notifications

### APNs
Apple Push Notification service. The only way to wake an iOS app that isn't running.

### Alert token vs VoIP token
Two *different* tokens from two *different* Apple registries, for two different jobs.
An **alert** token (from UIKit) shows a banner. A **VoIP** token (from PushKit) can
ring the phone like a telephone via CallKit. Neither can do the other's job, so one
handset legitimately holds **two rows** in our database. **[BUILT]** — `TokenKind`,
shipped v0.10.0.

### APNs environment
Apple runs two entirely separate push services, development and production. A token
minted by a dev build works *only* against the dev host. The same token string against
the wrong host is a `400 BadDeviceToken` and nothing else. **[BUILT]** — `apns_environment`,
v0.9.0.

### 410 Gone
APNs' way of saying "this device token is dead, stop using it". **This is the only
positive evidence of death we accept.** Age and silence are not evidence — a phone
that has been off for a month is still a valid destination. **[BUILT]** — the reaper.

### Token rotation
APNs and FCM reissue a device's token on reinstall, restore, or OS migration. Our rows
are keyed `UNIQUE(token)`, so a rotated token matches no row, **inserts** a new one, and
orphans the old — which is the entire reason a reaper has to exist. **[PROPOSED]** —
#4588 would key rows on the installation instead, so a rotation becomes an update and
the garbage is never created.

### install_id
A client-minted string identifying an app installation. **[BUILT but deliberately
unread]** — stored since v0.13.0, ignored by the router on purpose: the OS copies it
into iCloud / Android Auto Backup, so a restore puts two physical handsets under one
value. Blocked on app-side #3385 making it device-bound.

---

## Data and types

### Typed input / making illegal states unrepresentable
Designing types so a wrong value cannot be *constructed*, rather than writing a check
that catches it later. The payoff is measured in bugs that become unwritable.

Usually it doesn't add new safety — it moves existing safety **earlier**, from
commit-time or runtime to construction-time.

### Naive vs aware datetime
An **aware** datetime knows its timezone; a **naive** one doesn't. Every datetime this
codebase constructs is aware. SQLite stores no zone, so every datetime read back is
naive. Comparing them raises `TypeError`. **[BUILT]** — six sites patched; the class is
open as #4504.

### CHECK constraint
A database rule rejecting values outside a set. Real enforcement, but it fires at
**COMMIT** — the latest and least useful moment, after the work is done. **[BUILT]** —
this is how all fifteen of our closed sets are enforced today. #3400 is the open fork
on moving that to the Python type instead.

### StrEnum
A Python enum whose members *are* strings. Lets a closed set be a type without changing
how it's stored. **[BUILT]** — ~15 defined; the database columns are still typed as plain
strings, which is what #3400 is about.

### TOCTOU
"Time of check to time of use" — a bug where you check a condition, then act on it, and
something changed in between. The fix is to fold the check **into** the write so the
database evaluates both at once. **[BUILT]** — a standing house convention here.

### ULID
A sortable unique identifier. Like a UUID, but its leading bits are a timestamp, so
sorting by id sorts by creation time. **[BUILT]** — our ids throughout.

---

## Architecture

### Island
One gateway + broker + registrar + ChatServer. The unit of federation. Two are live:
`chat.imagineering.cc` and `chat.enspyr.co`. **[BUILT]**

### Gateway
The piece inside an island that puts a stable WSS + REST contract over the MQTT
backbone. This repo. **[BUILT]**

### Relayed vs P2P
A **relayed** call sends audio through a server (LiveKit) that sees the traffic. A
**P2P** call sends it directly between handsets. **[BUILT: 100% relayed today]**.
**[DECIDED: P2P by default for 1:1 calls]** — Nick, 2026-08-31. Both halves are true
at once, which is precisely why the tier tags exist.

### Cage match
Our adversarial review ritual: several different model families attack a change
independently. Mandatory by law for auth, moderation, wire-format and state-lifecycle
changes. **[BUILT]** — and its known structural limit is that every reviewer inherits
the same premises, so a panel interrogates *claims* and never the premise bundle.

### ADR
Architecture Decision Record. A numbered document capturing a decision and, more
importantly, the alternatives rejected and why. Lives in the home repo of whatever it
decides. `docs/adr/`. **[BUILT]**
