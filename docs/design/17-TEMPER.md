# 17-TEMPER.md: FCM token state machine

**Overall verdict:** UN-TEMPERED (provisional). Both seated families say RECAST, but only ONE
adversary family struck, so this cannot stamp SOUND or certify the recast. A real ≥2-family
re-strike is owed before build.
**Struck:** dt-17-1791455736 (2026-10-08). Seated: Maxwell (Claude) + Tesla (Grok).
Dark: **Kelvin** (Gemini login lapsed; the CLI is waiting on a browser sign-in), **Carnot**
(`~/.codex/config.toml` names `gpt-6.1-sol`, which a ChatGPT-account login rejects; the only
model that is accepted, `gpt-5.5`, is at its usage limit). Wu disabled. **A dark seat is a
coverage gap, not a vote.**
**Bundle:** design 17, plus a measured addendum (per-island ring accounts exist; a fresh key 400s at
mint) and `fcm.py`'s `_access_token`, `_verdict` and `send`.

## Per-family verdicts

| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | The sweep certifies its own enum, and the enum already lacks send-side 403 and mint-400 causes |
| Tesla (Grok) | RECAST | The table is short by one state (`send_denied`), and by time (`tick`), and `get -> None` carries three meanings |
| Kelvin (Gemini) | dark | login lapsed |
| Carnot (GPT) | dark | model config / quota |

## Measured during the strike (control, 2026-10-08)

A throwaway service account with **no role**: the token **minted fine**, then send returned **403,
gRPC status `PERMISSION_DENIED`, no FcmError `errorCode`**. The two real ring accounts
(role `islandRinger` = `cloudmessaging.messages.create` only) passed the same probe with a 400 for
the fake device token. So:
- IAM is checked at SEND, never at mint. The current mint-refusal log advice ("check the
  service account's role") points at the wrong layer.
- The island-wide signal is **403 + `PERMISSION_DENIED` + no FcmError code**.
  `SENDER_ID_MISMATCH` is the 403 that DOES carry an FcmError code, and it is per-device. This
  corrects Tesla's "never on a bare 403": the role-missing 403 IS bare.
- An earlier control reading ("OAuth 400 on a fresh key") is real but separate: it was key
  propagation lag on a seconds-old key, and it self-healed within 30s on the re-run.

## Fatal flaws (deduped, most severe first)

1. **No state for "this account may not send" (`send_denied`).** Raised by both. Revocation is now
   an intended operator action (one ring account per island), and it surfaces as the bare
   PERMISSION_DENIED 403 above. Today every device on every ring POSTs and ERROR-logs REJECTED.
   Clearing the token on it (Maxwell's first proposal, **retracted**) only re-mints a token that
   403s again. DISPOSITION: fold. A new state `send_denied(until)`. `minted` does not exit it; only
   time does (or a config reload). `get` returns Silent from it.
2. **The sweep closes the enum, not the response space.** Maxwell. A `product()` over events
   someone wrote down repeats round 3's from-memory list one level up. Flaw 1 is the proof.
   DISPOSITION: fold. A TOTAL classifier over (endpoint × status × error code) with a loud
   `unclassified` arm, tested non-silent. The sweep runs over the classifier's codomain.
3. **`get -> None` means three things; the mint handshake is unstated.** Tesla. Backoff (stay
   silent), empty (please mint) and just-failed share one value. Combined with Maxwell's measured
   fact that the fanout is a concurrent `asyncio.gather` (`push_service.py` ~1054), a cold cache
   means N concurrent mints, not "one wasted exchange". DISPOSITION: fold. `get` returns
   `Have(token) | Mint | Silent`. **Single-flight** the mint (one in-flight future awaited by every
   caller). That dissolves most of design question 2's burst.
4. **Time is missing from the grid.** Tesla. Cache expiry and backoff lapse are transitions.
   DISPOSITION: fold. `tick(now)` is an event; sweep the boundaries (zero lifetime after skew,
   backoff just before / just after, a 401 for a bearer `tick` already dropped).
5. **`mint_failed.kind` collapses causes with different remedies.** Both. An OAuth 400
   `invalid_grant` covers fresh-key propagation (self-heals), a deleted or disabled key
   (permanent, intended) and clock skew (permanent until NTP). DISPOSITION: fold. Classify on the
   OAuth `error` field (a fixed vocabulary, safe to log; the body stays off the log). Rewrite the
   operator messages per layer. Q2 answered: blinked takes a SHORT jittered backoff, if any is
   still needed after single-flight.
6. **Revocation latency is unpriced.** Maxwell. Whether Google invalidates already-issued access
   tokens when a key is deleted or an account is disabled is UNVERIFIED. DISPOSITION: fold as a
   measurement, not a claim: time from disabling `ring-<island>` to the first refused send. It
   doubles as the runbook for ejecting an island's ringer.
7. **`_TokenCell` is a fossil name; a mutable object hides the pure function.** Tesla. The unit is
   `transition(state, event) -> state`, the `plan_deliveries` shape. Naming it after the bearer
   cache leaves send-authorization homeless, which is how flaw 1 hid. DISPOSITION: fold. Retire
   the name (→ FCM island-auth transition).
8. **APNs (Q3).** Both agree it is not this table. Maxwell: same event algebra (Apple's
   `ExpiredProviderToken`/`InvalidProviderToken` is the `refused(bearer)` analogue). Tesla: a
   separate one-writer grid, or none. DISPOSITION: named. APNs is out of this design, and gets its
   own grid only if a finding asks for one.

## What holds

- The diagnosis: the defect class is implied (state, event) pairs, and the fix is data plus a
  full sweep.
- `refused(bearer)` as compare-and-clear by construction (round 3's race, as a transition).
- Blinks (429/5xx/transport) never enter the long negative cache.
- Scope: the wire `{c, k, m}`, routing and call/2 are out, and stay out.

## Disposition

RECAST, fold all seven, then **re-strike with ≥2 adversary families before any build**. That
needs Kelvin re-authed (`gemini` interactive sign-in) and/or Carnot's quota back.
This is recast round 1 of ≤3.

---

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The cell is the right container, but the design moves the from-memory list from the tests into an enum, and the enum is already missing the two events that matter most now that revocation is an operator action.

**Fatal flaws:**
- **The sweep certifies its own enum, not the response space (UNSTATED ASSUMPTION, the design's central claim).** "A new event or state then fails the sweep instead of waiting for a reviewer" holds only for an event someone ADDS to the enum. An event nobody wrote down is invisible to `product(states, events)`. That is the round-3 lesson, "the sweep was a list written down from memory", recurring one level up. Proof that it recurs: the table has no event for a send-side `403 PERMISSION_DENIED`.
- **Missing event: send-side `forbidden` (MISSING FAILURE MODE, raised by the measured addendum).** Token minting does not consult IAM, so "role removed / Cloud Messaging API disabled / island's ringer revoked by role" shows up only as a 403 on SEND. Today that is a per-device REJECTED with no circuit, so every device on every ring POSTs and gets refused. That is exactly the "one misconfiguration becomes one POST per device per ring" shape the mint backoff exists to stop, one layer down. The fix has to split on the error code. A bare `PERMISSION_DENIED` is island-wide, so it backs off. `SENDER_ID_MISMATCH`, also a 403, is per-device and must stay per-device.
- **`refused` conflates three causes with different remedies (WRONG OPTION-FRAME for `mint_failed.kind`).** An OAuth 400 `invalid_grant` covers: a freshly created key not yet propagated (measured, self-heals in seconds to minutes), a deleted or disabled key (permanent, the operator's intent), and the box clock out of skew ("token must be short-lived / reasonable timeframe", permanent until NTP). All three get the same 60s backoff, which is fine, plus the same log advice, "check the role", which is wrong for all three. `kind` should carry the OAuth `error` enum field. That is a fixed vocabulary, not echoed request material, so it is safe to log.
- **"No lock: a double-mint costs one wasted exchange" is false under the real fanout (UNSTATED ASSUMPTION, `_access_token` docstring, inherited by the proposal).** `push_service` fans out with `asyncio.gather` (line ~1054). A ring to N Android devices on a cold or just-expired cache fires N concurrent mints. Q2's "concurrent-mint burst can hit 429 repeatedly" is that herd, self-inflicted. **Single-flight the mint** (one in-flight future that all callers await). That dissolves Q2: no jittered backoff is needed for a burst we stop creating. It also shrinks the round-3 race class: with one mint in flight, the "wave B mints while wave A's late 401 arrives" window needs two mints *in sequence*, not a stampede.
- **Revocation latency is unpriced (UNDER-COUNTED BLAST RADIUS).** Per-island service accounts make "revoke this island's ringing" an intended operator move. A cached access token outlives that move until it expires (~55 min), or until a 401/403 arrives. Whether Google invalidates already-issued tokens when a key is deleted or a service account is disabled is UNVERIFIED here. The design must state what revocation does to a running island, and measure it rather than assume.

**What holds:**
- Owning the fields behind events instead of field writes is right. `refused(bearer)` as compare-and-clear by construction is the right primitive.
- The `plan_deliveries` analogy is right about *mechanism*: make the table data and test the data.
- Keeping `blinked` (429/5xx/transport) out of the negative cache is correct and should stay. A blink says nothing about the credential.

**If RECAST, what to fold back:**
- Make classification TOTAL over the response space rather than enumerating events. Classify (endpoint ∈ {mint, send}) × (status) × (error code) with an explicit `unclassified` arm that logs loudly and is tested to be non-silent. The sweep then runs over the classifier's codomain, and a response nobody anticipated lands in `unclassified`, not in REJECTED by default.
- Add the `forbidden(bearer)` event (send-side bare 403) → island-wide backoff + clear iff bearer matches. Keep `SENDER_ID_MISMATCH` per-device.
- Replace `refused` with the OAuth `error` code. Fix the operator log line: mint failures point at key/clock/propagation, send 403s point at role/API.
- Single-flight the mint. Delete Q2, or answer it as "dissolved by single-flight".
- Add a revocation section: measured latency from disabling `ring-<island>` to the first refused send. It doubles as the runbook for ejecting an island's ringer.
- Q3 (APNs): it shares the event algebra (Apple's `ExpiredProviderToken` / `InvalidProviderToken` 403 is the `refused(bearer)` analogue), but not the mint states. One classifier pattern, two tables.


## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** Three fields, three writers, three states — a triad tuned to yesterday's 401 — and the operator event this island was just rebuilt to receive (role-kill at send, 403) is a fourth harmonic that shatters the crystal at 3am.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- **Rank-deficient machine (addendum 1–2 + Proposal table + `send()` 401-only clearer).** Per-island service accounts make "revoke this island's right to ring" an intended, operator-initiated event. That event arrives as send-side `403 PERMISSION_DENIED`. The live access token is still a valid OAuth string; IAM is checked at send, never at mint. The table's only send-side event is `refused(b)` (the 401 compare-and-clear). A 403-as-401 (clear and remint) yields a fresh token that 403s forever. A 403 ignored (today's code) lets every device on every ring POST and ERROR-log `REJECTED` while `/health` stays green. A 403-as-any-403 backoff lets one `SENDER_ID_MISMATCH` device mute the island. The cell as specified has no legal transition for the frequency the addendum just turned on.
- **`get(now) -> str | None` is three voltages on one wire (Proposal API).** `None` means backoff (stay silent), empty (please mint), and blinked-or-failed-mint (already tried). `_access_token()` today is get-or-mint; the cell splits `get` / `minted` / `mint_failed` and then leaves the handshake with HTTP unstated. Two callers reading `None` as "mint" are the concurrent stampede the "no lock, one wasted exchange" comment already blessed. The product sweep never sees this: `get` is absent from the state×event table.
- **Time is load-bearing and missing from the table (Proposal table + cache lifetime ~55 min + 60s backoff).** `cached(t)` is `cached(t, expires_at)`; `backoff` is `backoff(until)`. Expiry is a silent transition into `empty`. `product(states, events)` over `{empty, cached, backoff} × {minted, mint_failed, refused}` leaves every skew-boundary and backoff-lapse implied — the same shape that produced the round-3 late-401. The `plan_deliveries` lesson was a full grid; this grid has no `tick(now)`.
- **Four `kind`s, one column (Proposal table + addendum 2 + `_access_token` non-200 path).** `credential` (parse/sign, will not self-heal), `refused` (mix of dead key and Google's measured HTTP 400 on a key minted seconds ago), `unreadable` (200 with a bad body), and `blinked` (429/5xx) exist in the event enum and then collapse: three kinds share "backoff", `blinked` is `—` in every state. Question 2 is already a hole in the table, written as a question so the suite cannot fail it. The 400-on-fresh-key is the future input: an operator provisions `ring-enspyr`, the first mint 400s, 60s of Android silence, log line blames the IAM role the token endpoint never consults.
- **Mutable `_TokenCell` is the wrong reification of a true lesson (Why this exists + Proposal).** `plan_deliveries` became a pure function over a data table. Methods that mutate three fields collocate writers; they still hide the function `product()` wants to sweep. The name `_TokenCell` will re-inject "this is about the bearer cache" and leave send-auth homeless — the fossil that keeps the 403 path in comments.
- **APNs against this table (Question 3).** Local signing has one writer and no mint-network kinds. Importing `blinked` / `refused` / compare-and-clear onto APNs grows dead events; sharing the table couples two auth systems the code already calls divergences. A one-column local-sign grid is a different crystal.

**What holds:**
- The diagnosis: the finding class is implied `(state, event)` pairs, and a literal table plus `product()` sweep is the closure that comments-and-guards keep missing.
- `refused(bearer)` as compare-and-clear by construction — the round-3 race, named as a transition.
- HTTP stays outside the transition (once `get` is a read with a tri-state, and `minted` / `mint_failed` are the only writes from the wire).
- Transport failure as a non-credential event: a one-second blip must not become a minute of silence.
- Scope cut: wire `{c,k,m}`, routing, call/2 — already tempered.

**If RECAST, what to fold back:**
- **Rename the object.** This is island-auth, not a token cell. The unit under test is a pure `transition(state, event) -> state` (the `plan_deliveries` shape). A tiny holder around one state blob is optional sugar; `_TokenCell` as the title of the design is the fossil to retire.
- **Enrich state until the operator event fits.** At least: `empty | cached(token, exp) | mint_backoff(kind, until) | send_denied(until)` with `now` threaded through every read. `send_denied` enters only on FCM detail `PERMISSION_DENIED` (the role/API-disabled signal), never on `SENDER_ID_MISMATCH`, never on a bare 403. From `send_denied`, `get` yields silence (no per-device POST, no remint). `minted` leaves `send_denied` in place — a new bearer still 403s. Exit is `tick` after a jittered window, or an explicit config-reload event if one exists.
- **Make `get` tri-state:** `Have(token) | Mint | Silent`. Orchestration: only `Mint` may POST the token endpoint. That single distinction kills the three-None wire and makes the no-lock double-mint a testable pair (`Mint`×`Mint` → last `minted` wins, both tokens valid) rather than a comment.
- **Put `tick(now)` in the grid.** Cache lapse and backoff lapse are events. The suite sweeps expiry edges (lifetime 0 after skew, backoff just-before / just-after, 401 of a bearer that `tick` already dropped).
- **Split the `mint_failed` column until kinds have distinct rows:** `credential` / `unreadable` → long backoff; `blinked` (429/5xx and transport) → short jittered backoff (Question 2 is a transition, answer it: yes); mint HTTP 400 on a newly created key → the short family, with a log that names key-propagation, not IAM. Parse the token-endpoint `error` field if it can separate `invalid_grant` from propagation; keep the body off the log. Role-missing remains a send-side `PERMISSION_DENIED`, never a mint-side sermon.
- **Caller contract of `refused`:** a send 401 is our bearer, same family as mint `None` — `Verdict.TRANSIENT` for the device, then compare-and-clear, then the next `get` may `Mint`. `REJECTED` stays for message-shape and project-shape faults.
- **Question 1:** a table bolted onto the current functions still tests an implied machine; extract `transition` first. Question 3: APNs gets its own one-writer grid, or none. This table is FCM's.
- **Invariant inside `minted`:** only a non-empty `str` bearer with a non-negative remaining lifetime enters `cached`; any other payload is `mint_failed(unreadable)` in the transition itself, so the 200-with-`access_token: null` frequency cannot bypass the orchestrator again.
