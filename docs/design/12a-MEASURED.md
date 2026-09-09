# 12a-MEASURED — what the island actually does, measured 2026-09-09

Written because three design documents (island 12, island 12a, app 16 v2) reason in detail
about PushKit and CallKit, and nobody had checked what the island sends. Every claim here is
a grep or a read against `src/` at `c36cfbb`, with the instrument positive-controlled.

## M1. The island has NO VoIP send path. None.

```
$ grep -rni "voip" --include="*.py" src/ | wc -l
2
```

Both hits are **inside one comment** in `domain/apns.py` explaining why we do *not* use VoIP.
There is no VoIP token kind, no `.voip` topic, no PushKit anything.

**Positive control** (because a zero-count grep is a fact about the instrument until proven
otherwise): `grep -c "device" src/aiko_gateway/domain/devices_service.py` → **14**. The
instrument sees the files. The zero is real.

`apns.py:399` sends a hardcoded string literal:

```python
"apns-push-type": "alert",
```

Not a variable, not a parameter, not a branch. **One push type exists.**

## M2. Decision 2's "two token kinds" is designed, not built.

The devices table's enum is `Platform = {APNS, FCM}` — that is **iOS-versus-Android**, not
alert-versus-VoIP. A device row records which *service* to talk to, not which *kind of token*
it holds.

## M3. Consequence — the temper's flaw 5 is PREMATURE, not false.

Flaw 5 (Tesla, seconded by Carnot) says *"a user who declines notifications has a VoIP token
and will never have an alert token, so alert-borne ends cannot reach exactly the phones that
most need the ceiling."*

**That population does not exist today, because no VoIP token exists today.** The hazard is
real and becomes live the moment Decision 2/3 ship. So the finding is correctly aimed and
early — and its disposition (*do not harden Decision 4 yet*) is **strengthened**, not weakened:
you cannot correct a predicate that forks a transport the system has never sent.

**TEMPER.md flaw 5 amended accordingly.**

## M4. Flaw 4's clock collapse contradicts a written rationale in live source. Both are right.

`apns.py:140-145`, beside `_EXPIRATION_SECONDS = 60`:

> 60s is **deliberately** longer than the app's 10s ring-freshness gate: the two clocks answer
> different questions (that one decides whether to RING, this one decides whether the wake is
> still worth delivering at all), and a wake arriving at 30s still usefully says *"you just
> missed something in here."*

Design 12 says the opposite — *"a VoIP push should expire exactly when the ring stops; the two
collapse into one number"* — and the temper adopted that as flaw 4's disposition.

**Neither is wrong. They belong to different architectures.** In the alert world a late wake is
still useful, because the user taps it to enter a room whose door is open. In the VoIP world a
late wake reports a call to CallKit, and a stop that has already fired cannot retract a ring
that has not yet started. **The collapse is correct only under CallKit, and the system is not
running CallKit.**

**TEMPER.md flaw 4 amended:** the collapse is conditional on the CallKit transition, and the
alert-world rationale must be explicitly retired at that point rather than silently
contradicted.

## M5. The live comment still argues the superseded position.

`apns.py:392-398` reasons, in production source:

> Taking PushKit means taking mandatory CallKit with it. Apple's own documented alternative is
> exactly this — a UserNotifications alert — and **it fits "a call is a gathering" better than a
> ring does: a gathering has a door that stays open and needs no 30-second synchronous window.**

That is #3267's decision (2026-08-19), and it was **explicitly superseded** by #3609
(2026-08-29) — *"The alert-push decision's premise no longer holds"*, from Nick: *"I want to get
the app actually ringing, like phone calls ring."* The ordering is clear and there is no
conflict to surface; **but the code still tells the next reader we deliberately chose against
VoIP**, which is the argument they will inherit if they read the source instead of the tracker.

**Owed:** amend the comment to record that the reasoning was correct and is superseded, naming
#3609. Do not delete it — it is the record of a real decision, and the gathering argument may
return if CallKit is abandoned.

## M6. The framing fact none of the three documents states

**Not one VoIP push has ever been sent by this system.** The app tab's `.voipspike` build will
be the first. Every claim in designs 12, 12a and 16 v2 about must-report, the momentary ring,
report-and-end ratios and flaw 9's fleet-wide reputation burn is reasoning about a transport
with **zero operational history here** — Apple's documentation, correctly read, and nothing else.

That is not an argument against the design. It is an argument for why the one-handset
`reportCall(endedAt:)` experiment is not a detail: it is **first contact**, and it is the only
thing standing between three documents and a transport none of them has touched.

Today's working ring — proven on real handsets 2026-08-23 and 2026-08-29 — is an **alert** push
that the user taps. CallKit is not in the picture at all yet.

## What this does not change

- The two contradictions found inside design 12 (Decision 4 vs 1c; the call/not-call predicate
  misnaming) stand — they are about the design's internal consistency, not its transport.
- Nick's 2026-09-09 ruling that the island owns the ring ceiling stands.
- Flaws 1, 2, 3, 6, 7 of TEMPER.md are untouched by these measurements.

---

# FIRST CONTACT — the VoIP spike, 2026-09-09 (app tab)

Run by the app tab on `spike/voip-must-report`. **iPhone 14 Pro (iPhone15,2), iOS 26.6.1 build
23G83**, development-signed, sandbox APNs, topic `cc.imagineering.aikoChatApp.voipspike.voip`,
pushes sent from a local script holding the `.p8` — no island in the loop.

## M7. PROVEN — CallKit rings from a VoIP push with no Dart alive

```
[VOIP-SPIKE] push #1 report OK 849CF575-D357-4A23-8A74-D8A4F71A1122
```

Corroborated two ways: the log, and Nick watching the handset ring. The debug build logged
*"Cannot create a FlutterEngine instance in debug mode"* and **rang anyway**, because the whole
path is Swift in `didFinishLaunching` and needs no isolate.

Full chain demonstrated end to end for the first time in this system's history: PushKit
registration → VoIP token → sandbox APNs → `reportNewIncomingCall` → visible ring on a real
handset. **This is the cold-start property design 16 v2 §1 rests on, demonstrated rather than
argued.** M6's "not one VoIP push has ever been sent" was true until 2026-09-09; this was it.

**Cite this one. It is solid.**

## M8. VOID — the must-report question has NO ANSWER, and the reason is the finding

Not "inconclusive". **Void.** The negative control never fired:

```
[VOIP-SPIKE] push #1..#4 mode=silent   →  "reporting NOTHING"     Runner[1234:129433]
```

Four consecutive VoIP pushes where the handler reported **nothing whatsoever** — the flagrant,
undisputed violation — and iOS did not terminate, did not warn, did not degrade delivery. **One
PID across all four** (a termination would restart the counter in a new process, since VoIP push
relaunches).

**So the enforcement mechanism was not engaged in that configuration**, which makes both arms
uninformative: the earlier `endonly` arm read "unpunished" for the same reason the must-fail arm
did. A clean-looking log would have supported "YES, ends can ride VoIP" and it would have been
worthless.

Most likely cause, flagged by the app tab *before* the run: launching via
`devicectl process launch --console` holds a **usage assertion** and leaves the app
`running-active-Visible`. **Must-report governs waking a SUSPENDED app; a foregrounded app was
never in the state the rule polices.** Unseparated secondary candidates: development signing, and
a violation count below any threshold.

**A valid run needs the app suspended, no console attach, no usage assertion, logs read after the
fact via `log collect`.** Harness redesign, not another push.

**Consequence for TEMPER.md flaw 5: unchanged and still gating.** The predicate fix stays
un-hardened. The tree's top node is still open.

## M9. Tesla's momentary-ring claim has STILL never met a handset

The `endonly` arm reports `endedAt` for a UUID iOS has never seen. It never reports a call and
then retracts it. The race Tesla describes — retract-before-completion racing "unknown UUID"
against a full-screen flash after it — needs `reportNewIncomingCall` followed immediately by an
end **on the same UUID**, and that arm does not exist. **Temper flaw 1's central factual claim
remains unmeasured.** It is a separate arm and it is worth building.

## M10. A new instrument, and it makes flaw 9 observable

`CSDVoIPApplicationKillCounts` in `com.apple.TelephonyUtilities` is the **per-app VoIP kill
ledger**. `callservicesd` consulted it once per push and logged *"found no value for key"* each
time — absent meaning zero recorded kills.

This matters beyond tonight: **flaw 9's mechanism (a report-and-end ratio costing VoIP delivery
fleet-wide) stops being inferred and becomes directly readable.** Any future design that spends
momentary rings can be *measured* rather than argued about.

## M11. Containment held — verified independently, not accepted

- **Both live islands still read `APNS_TOPIC=cc.imagineering.aikoChatApp`** (checked over ssh
  from this repo, both boxes, after the run). No island was touched; every push came from a
  local script.
- `git show main:...project.pbxproj` → 3× `cc.imagineering.aikoChatApp` + 3× `.RunnerTests`.
  The `.voipspike` change is confined to uncommitted working-tree edits on
  `spike/voip-must-report`.

**Owed, and this class has bitten this project before:** the harness that produced M7 exists
**only as uncommitted working-tree state on one machine**, on a branch with no remote. `#3198`
records the same failure once already — *"its signer previously existed only in a scratchpad and
had evaporated."* The first VoIP push this system ever sent deserves a committed instrument.

## Method note worth keeping

Three times in one evening a **control** caught something neither reasoning nor adversarial
review would have: verifying the `_forget` claim instead of conceding it; the 4/4 strike on the
trilemma; and a negative control voiding a result that would otherwise have been reported as an
answer. The instrument also nearly won twice — a fabricated `%@`-redaction mechanism was refuted
by the device printing `[VOIP-SPIKE] {public}@` back, because `NSLog` is printf-style and does
not take os_log annotations.
