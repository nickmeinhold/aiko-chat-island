# TEMPER.md: Dart port of the island gateway

> **STATUS after 4 rounds (2026-10-25): RECAST 4/4 in every round; never DISSOLVE. Still PROVISIONAL, not tempered.**
>
> - **Round 1** (15 flaws): structural.
> - **Round 2** (12): subtractive.
> - **Round 3** (10): narrow, and mostly deletions. But it includes one SEVERE defect that round 3's own fold introduced: the ready gate turns a bus stall into a total island outage. The severity trend is therefore *not* monotonic.
>
> - **Round 4** (bonus, Nick's choice; post-r3 folds only): 12 findings, none a total outage. Three were introduced by the folds: the unread `removed_upstream` mark, the configure×image-exclusion crash-loop, and the fold reaching only 1 of N sites. Two of the panel's OWN proposed fixes would have added outage paths, and were rejected. Dispositions are in the round-4 synthesis; they are NOT yet applied to `DESIGN.md`.
>
> Post-round-3 folds are in `DESIGN.md`, still marked UNSTRUCK pending Nick's call on round 4: (a) apply the r4 folds and go to Blade, or (b) stop at PROVISIONAL.

# Round 1

**Overall verdict: RECAST (4 of 4).** No family voted DISSOLVE. The frame holds; the oracle, the WebAuthn policy, the switch and the schema window do not, yet.
**Struck:** dt-1792894225 · families seated: Maxwell + Noether, Carnot, Tesla (Wu/Kimi disabled) · bundle: CRUCIBLE, RULINGS, RESEARCH, SPARK, DESIGN (94 KB)
**Arrival:** Maxwell (composed in-process), then Noether, Carnot and Tesla, collected together after one wait (Noether reported 101s). The per-family sections below are verbatim, in that order.

## Per-family verdicts

| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | Three behaviour changes smuggled into a port, `/openapi.json` unpriced, no kill criterion for steps 5 to 7. |
| Noether (DeepSeek) | RECAST | The ledger conserves final state, but not atomicity, ordering or the credential accept-set, which are the invariants the port exists to protect. |
| Carnot (GPT) | RECAST | The sole-owner switch holds; matching synthetic ledgers cannot establish security, concurrency or release-state correctness. |
| Tesla (Grok) | RECAST | Normalisation erases the bytes rollback must read, step 4 re-strikes D1 on the wrong share, and the freeze window traps the test island. |

## Fatal flaws (deduped, most severe first)

1. **The WebAuthn verifier changes policy inside a port.** Raised by all four.
   - §2.8 makes Dart stricter than py_webauthn 3.0.0 (`crossOrigin`, `topOrigin`, ID length, formats), which contradicts §2.3's differential agreement.
   - The `aaguid` census cannot see `alg` or `clientDataJSON` shapes.
   - Storing *original* COSE bytes diverges from the py_webauthn re-encoded rows, so rollback meets bytes Python never wrote.
   - Our only authenticator is software P-256 `none`.
   - **Disposition: fold** (§2.8 rewritten as parity-first; real-device matrix; `alg` census).
2. **The oracle is blind to atomicity, ordering, intermediate states, forbidden effects and races.** Raised by Noether, Carnot and Tesla. One canonicalised final diff cannot tell one 16-table commit from 16 commits, nor see a transient unauthorised fan-out followed by cleanup, nor localise which decision variant fired.
   - **Disposition: fold** (§2.2: sequenced commits and effects, a harness-only decision trace, fault injection, adversarial schedules with independent expected results).
3. **The normaliser can erase exactly the bytes rollback must read.** Raised by Tesla and Noether. `validateDatabaseSchema` compares by affinity, so DATETIME, BOOLEAN and JSON all pass.
   - **Disposition: fold** (two equalities; raw stored bytes for DATETIME, BOOLEAN, JSON and `public_key`, in both directions).
4. **No size estimate and no stop condition for the bulk of the port.** Raised by all four.
   - **Disposition: fold** (a coverage map at step 2; gate G before step 5 with Nick's go; falling back to R5 is a defined outcome).
5. **The switch has no release-state model.** Raised by all four. It lacks:
   - a drain protocol;
   - the two-pin compose transition that `update.sh` must know;
   - rollback triggers, a soak period and an owner;
   - an MQTT session policy;
   - consumed challenges and refresh-token rotation across rollback;
   - the point at which Python rollback stops being supported.

   **Disposition: fold** (§2.1 switch protocol; packaging and two-pin proof moved to step 3).
6. **The post-switch freeze window is incoherent across two islands.** Raised by Noether and Tesla; Carnot adds that the authority change ends rollback.
   - **Disposition: fold** (one named maximum soak; no alembic head bump from the first switch to the authority change; a schema-shaped bug on syd is a rollback; Python rollback ends explicitly at the authority change).
7. **Step 4 still consumes `channel_list` after step 0(c) found it broken.** Raised by Tesla. The Category replacement path is unproven for add, remove, reconnect and snapshot/incremental agreement (Carnot).
   - **Disposition: fold.**
8. **A `Decision` built before authorisation can go stale.** Raised by Carnot. Membership, bans and session generation are mutable state.
   - **Disposition: fold** (a Decision is an authenticated command with provenance; authorisation is rechecked inside the applier's owning transaction, per the one-door law).
9. **`/openapi.json` disappears at the switch.** Raised by Maxwell. It is FastAPI-generated, live at 76 KB, and named in CLAUDE.md as the cross-tab wire contract.
   - **Disposition: fold** (frozen artifact, served by Dart, a route-parity CI check, the app tab told).
10. **"Byte-for-byte" over-constrains, and the parsers are not bound to the silent-desync rule.** Raised by Maxwell and Tesla.
    - **Disposition: fold** (app-observable equality against the frozen schema; an unknown-field episode).
11. **Settings is an unlisted parser.** Raised by Maxwell.
    - **Disposition: fold** (an env → effective-settings vector file run by both).
12. **Drift's guarantee is overstated, and stamping `user_version` writes before validating.** Raised by Carnot.
    - **Disposition: fold** (validate the whole schema, then stamp; describe it as checked migration discipline, not compile-time proof; correct CRUCIBLE's claim in place).
13. **Unmeasured premise: "defects live in decisions" in `src/`.** Raised by Maxwell.
    - **Disposition: fold** as C10, measured before it orders the build.
14. **Andy is asked last.** Raised by Maxwell.
    - **Disposition: fold** (the ask moves to step 0, in Nick's hands).
15. **Clocks.** Raised by Tesla.
    - **Disposition: fold** (C1 includes episodes that cross the challenge TTL, JWT expiry and the 300s lease).

**Checked against source rather than accepted:** Tesla's "a redelivery after the switch inserts a duplicate row". At the prod pin, `mqtt.py:112` builds `mqtt.Client(transport=…)` with paho defaults: clean session, random client id, QoS 0 subscriptions (`:230,235`). So the broker keeps no session and never redelivers to a new process. The harmonic does not fire **under that policy**. It is folded as a stated policy the Dart client must match, not as a #42 prerequisite.

## What holds (all four, independently)

- One writer, one boot; process memory moves together; #46 and #42 leave the critical path.
- The observer, not an Actor, with two explicit dialect filters; the Feb pin as the target; the pinned `bus-e2e` job.
- No production tapes.
- Alembic stays the authority while building; FK stays off; cascades stay code; the `IslandDateTime` write format.
- The separable step-0 fixes, especially 0(c) D1 and 0(e) pyright.

## Disposition

**RECAST: fold all 15 into `DESIGN.md` (round 2 of at most 3), then re-strike.**

---

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The frame survives (one switch, a recorded oracle, a sealed Decision inside Dart), but the design smuggles three behaviour changes into a port, never priced the wire-contract artifact that FastAPI generates for free, and gives the expensive middle (steps 5 to 7) no kill criterion, which is how rewrites die.

`John McClane: "Come out to the coast, we'll get together, have a few laughs."` That's what every rewrite plan says at step 0.

**Fatal flaws:**
- **[under-counted blast radius] `/openapi.json` disappears at the switch.** CLAUDE.md names the published `/openapi.json` as part of the cross-tab wire contract that the app tab reads before any wire work. FastAPI generates it from the route signatures. `shelf` generates nothing. §2.6 says "the `/v1/*` wire does not change; the app tab has nothing to build", yet the artifact that DEFINES the wire for the other tab silently stops existing, or becomes hand-maintained and starts drifting the day it is written. Nowhere in DESIGN.md is this priced.
- **[illegal move: port plus behaviour change in one step] The WebAuthn verifier is specified to be STRICTER than the one it replaces** (§2.8: reject `crossOrigin: true` and any `topOrigin`, credential ID ≤1023, derived `apk-key-hash` origins). At the switch, any real passkey Python accepts and Dart rejects is a lockout, and the episode oracle cannot see it, because the episodes use a software authenticator and synthetic users. A port must accept exactly what the predecessor accepted. Tightenings ship to Python first (step 0), or after the switch, and never inside it. The same applies to deriving `android:apk-key-hash:` origins: it changes what an operator's `PASSKEY_EXTRA_ORIGINS` means.
- **[missing parse half] Settings is a parser the design never lists.** §2.3 lists the parse half as HTTP, WS, bus, CBOR and ID tokens. It omits the 62-field `Settings`, which is pydantic env coercion with known, cage-matched quirks: whitespace stripping, None defaults, `ge=1` bounds that fail loud (the `RATE_LIMIT_ENABLED="   "` history, `config.py:102,337,433`). An operator's existing `.env` parsed differently by Dart is a silent behaviour change at the switch, exactly the class `island configure` was built to make loud. It needs a differential vector file (env → effective settings), the `test_seed_peer_vectors.py` pattern, run by both.
- **[wrong option-frame for the oracle] "Byte-for-byte" over-constrains the wrong thing and under-constrains the right one.** Byte equality of HTTP bodies forces Dart to reproduce pydantic's 422 `detail` arrays, JSON key order and FastAPI's error prose: enormous work nobody consumes. Meanwhile the property that matters at the switch (does a REAL Apple or Android device still sign in) is not in the oracle at all, because there is no production tape. The contract should be *app-observable behaviour*, checked against the frozen `openapi.json` schema plus the app's actual parsing. And there should be a **pre-switch real-device matrix**: a staging RP, with iOS, Android and macOS browsers registering and signing in against the Dart verifier.
- **[no kill criterion] Steps 5 to 7 are the rewrite, and nothing can stop them.** Open variable 10 admits the bulk is unestimated (13k lines of service tests). C9 admits the cost compounds. The only stated response is "a feature freeze or R5". No step in §3 is a go/no-go. Fold: an explicit decision gate after step 4, with a size estimate and Nick's go, before the applier work starts. Otherwise C7 ("not a big bang wearing steps") is asserted, not designed.
- **[unstated assumption the enthusiasm smuggled in] "The island's measured defects have lived in decisions."** That was measured on `deploy/` tooling (design 15's corpus), not on `src/`. The only `src/` evidence offered is one docstring in `apns.py`. The design's priority order ("port the applier first, because that is where the defects lived") rests on an extrapolation from a different codebase. It may be true. It is unmeasured.
- **[sequencing] The Andy question is asked last and costs least first.** §7.1 gates step 8 on Andy's answer about ADR-0007 and the licence, so steps 1 to 7 are invested before it is known. Asking costs one message, and the answer could reshape §2.4 (for example, Andy wants a different Dart aiko, or a different licence). Move the ask to step 0 (Nick's hands).
- **[missing failure mode] "Then watch" has no definition.** Step 8 switches syd and "watches". There are no rollback triggers (auth failure count, 5xx rate, push accepted/failed ratio, bus relay liveness), no soak duration before melb, and nothing names who watches. Rollback is designed; deciding to roll back is not.

**What holds:**
- The seam is a moment, not a route. That is forced by measurement (the 16-table deletion transaction, in-memory invariants, the worker lock), not by taste. It removes #46 and #42 from the port's critical path.
- The observer-not-Actor bus shape, both dialects with explicit filters, the Feb pin as target. It also deletes a real latent RCE-class surface.
- Hand-faithful drift with CI `validateDatabaseSchema` against an alembic-built seed, the `IslandDateTime` write format for rollback, and the post-switch freeze (F1).
- R9 (pyright in CI) adopted as step 0(e) rather than used as an excuse.
- F7 (known-bug episodes are not specifications), proven by D1 the same day.

**If RECAST, what to fold back:**
- §2.6 and a new step 1(b): freeze today's `/openapi.json` as a checked-in artifact. Episodes validate responses against it. The Dart gateway serves it, and a CI check fails if a route exists in one but not the other. Tell the app tab it becomes a maintained file at the switch.
- §2.8: **parity first.** The Dart verifier accepts exactly what py_webauthn 3.0.0 plus our handlers accept. Each tightening is a separate PR, applied to Python in step 0 or after the switch.
- §2.3: add Settings to the parse half, with an env → effective-settings vector file run by both implementations.
- §2.2: replace "byte-for-byte" with *schema- and app-observable equality*, with an explicit list of what is compared exactly (status codes, JSON fields the app reads, WS frame types) and what is not (error prose, key order).
- §3: add a real-device sign-in matrix on a staging RP before step 8.
- §3: add a **decision gate after step 4** (size estimate, Nick's go). Move the Andy ask to step 0.
- §3 step 8: define the rollback triggers, the soak before melb, and who watches.
- §5: add C10, "defects live in decisions in `src/` too". Measure it from the gateway's own fix history (`git log --grep=fix -- src/`, classified) before it orders the build.


*Maxwell check after striking: `/openapi.json` is live on chat.imagineering.cc (HTTP 200, 76,186 bytes), FastAPI default at `main.py:317`.*


---

## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The design makes the *effect ledger* the conserved quantity of the port, but the invariants the port exists to protect — all-or-none atomicity, effect ordering, and "Dart accepts every credential Python accepts" — are symmetries the ledger is structurally blind to, so the oracle cannot witness the very defects it was built to catch.

**Fatal flaws:**

- **The oracle cannot see the "all or none" class — the port's own motivating defect.** DESIGN §2.2 defines the effect ledger as "the database diff, as rows added, changed and removed, **canonicalised**," and §2.3 + §3 step 5 claim "the applier is proven by the episode ledgers." A 16-table single-commit account deletion (RESEARCH §A2, `accounts_service.py:182-263`) and sixteen sequential commits produce the *same* final diff. So the ledger conserves final state but not the transaction boundary. CRUCIBLE's own motivation is that "every recurring defect was a *decision* mistaken for a value: choice versus default, which source wins, **all or none**." The oracle is blind to *all-or-none* and to partial failure. Class: **wrong oracle-frame / missing failure mode.** Episodes are happy-path scripts over synthetic users; there is no crash, no kill-between-write-and-publish, no concurrent arrival. A "sequence of swaps" (or even one boot) whose proof apparatus cannot observe the atomicity it claims is a proof of the wrong theorem.

- **"Differential agreement with Python" contradicts "Dart checks more than py_webauthn."** §2.3 says WebAuthn is proven by *differential agreement* — "the same input specimen must produce the same decision in Python and Dart." §2.8 says the Dart verifier "**checks more** than py_webauthn 3.0.0 does": rejects `crossOrigin:true`, any `topOrigin`, credential IDs >1023, and (initially) every attestation format but `none`/`packed`. These cannot both hold: for any input Python accepts and Dart rejects, differential agreement **fails by construction**, and the ledger — recorded from Python — records an *accept* Dart must not reproduce. The failure mode is precisely the design's stated riskiest surface (§0): a valid passkey locked out, silently, with no episode red. §7 open-variable 5 (the `aaguid` census) is the intended mitigation, but until it runs, Dart's strictness is an unmeasured, unoracled accept-set shrinkage. Class: **wrong option-frame** (agreement vs. correctness) plus **missing failure mode** (silent lockout).

- **Per-island switch vs. global drift baseline — the freeze window is undefined.** §2.5: "between an island's switch and the drift ADR taking authority, **that island's** schema is FROZEN," and "after the switch on **both** islands… freeze 0028+ as drift's baseline." Between syd's switch and melb's switch there is a window where syd is Dart-and-frozen while melb is still Python. Either melb may take a new alembic migration (then syd's drift definitions lag, and "the drift baseline" is undefined — is it syd's head or melb's?), or melb may not (then C8's "no schema freeze is needed WHILE BUILDING" is false the moment the first island switches). The design asserts both. The invariant at stake — *both islands speak one schema* — is broken on one path or the other. Class: **under-counted coupling / broken invariant across the switch.**

- **The "one boot" has no drain protocol.** §2.1: "Python stops and Dart takes `.aiko-worker.lock`." §2.2 records effects *inside* episodes; F6 notes in-flight bus messages are lost "not new: any `update.sh` restart does the same today." But the design never specifies how Python is *stopped* so that in-memory work (the Hub fan-out, the ordered topology queue, the 16-table transaction, `echo.mark_sent`-then-`bus.send`) is drained rather than cut. A SIGTERM mid-cascade rolls back cleanly (SQLite is crash-safe) — but a cut between a bus publish and its matching DB row leaves an effect *outside any episode*, which no ledger ever sees. C5 asserts "no state outside SQLite and memory blocks Dart" but never bounds the transition itself. Class: **missing failure mode.**

- **C3's rollback invariant is one-directional for credential bytes.** C3 verifies "**Dart reads every row Python wrote**, including py_webauthn's re-encoded COSE `public_key`." But §2.8 stores "the **original** COSE bytes." So Dart-written passkey rows differ in representation from Python-written rows, and rollback requires Python to read Dart's original bytes — a direction C3 does not assert and that py_webauthn's re-encode-on-store assumption may not satisfy. The invariant — *the same credential is the same bytes on every path* — is assumed, not conserved. Class: **unstated assumption / under-counted blast radius.**

- **The double-implementation window has no termination bound and no size.** C9: the Python gateway "keeps shipping during the port," every behavior lands twice, "if the port runs long, the cost compounds." C2: the ~13k lines of in-process service tests "have to become episodes or Dart unit tests," and open-variable 10 says "**nobody has estimated this**, and it is probably the bulk of the port." A months-long bet whose bulk cost is unestimated and whose oracle (see flaw 1) cannot prove its motivating class is a blast radius under-counted by the design's own admission. Class: **under-counted blast radius.**

**What holds:**

- **The seam choice is correct and it dissolves the right problem.** "One boot, no second live writer" (§2.1) genuinely retires #46 and #42 as *prerequisites*, and it is the one thing three blind sparks converged on. The rejection of the route-strangler (R1) is backed by measurement (§A2), not taste.
- **ISL-0001/ISL-0002 are preserved during the port** (alembic stays authority, drift opens faithfully, FK-off cascades ported as code), and rollback is a pin change on an unchanged schema.
- **Targeting the production pin and speaking both dialects** (§2.4), with two explicit `:0`/`:1` filters rather than `*`, is the correct call and correctly refuses to couple the bus bump to the port.
- **The separable fixes (steps 0a–0e) deliver value alone**, and 0(c) (the D1 fix, measured broken) and 0(e) (pyright in CI) are real, cheap wins independent of the port.
- **The claims-to-falsify (§5) and the honest rejection list (§6, §9)** are unusually clean. The design knows most of its own risks; it just hasn't closed the oracle gap.

**If RECAST, what to fold back:**

- **Make the ledger witness order and atomicity, not just final state.** Record per-episode *effect ordering* and *DB commit boundaries* (a sequence of diffs, not one canonicalised diff), and add a fault-injection episode class: crash mid-cascade, kill between DB write and bus publish, restart mid-episode, concurrent arrival. Assert the 16-table deletion is all-16-or-none via a mid-episode snapshot. Without this, §2.3's "the applier is proven by the episode ledgers" is unproven for the class CRUCIBLE names.
- **Reconcile the WebAuthn frame explicitly.** State the policy as "Dart's accept-set is a *superset* of Python's until the `aaguid` census proves a format/flag is unused in prod; only then may Dart *add* rejections." Gate every strictness (§2.8) behind the census, and make differential agreement the *initial* contract. A lockout is the same outage as a rollback (C3) and must be a red episode, not a silent one.
- **Define the drain (§2.1/C5).** Specify stop-admission → drain in-flight → seal → release `.aiko-worker.lock` → start Dart, and promote F6 from "noted loss" to a specified step.
- **Resolve the freeze window.** Set the global drift baseline to the head at the *second* island's switch, and forbid migrations between the first switch and the ADR (or switch both islands atomically). Correct C8 accordingly.
- **Make C3 bidirectional** for `public_key` bytes — either assert Python reads Dart-written original COSE bytes, or store re-encoded bytes to match Python.
- **Bound C9 and size the port.** Put a cap or a feature-freeze date on the double-implementation window, and produce the §7.10 size estimate *before* step 5 — a months-long bet on an unestimated bulk cost is itself the broken invariant.

---

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** The sole-owner cutover holds. The proposed proof does not yet justify it: matching synthetic ledgers cannot establish security, concurrency correctness or production compatibility. Fold back to a bounded conformance harness, explicit transactional invariants and a rehearsed release transition. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. Here, the wasted work is building deterministic replay machinery before deciding what evidence permits the switch.

**Fatal flaws:**
- The original cost falsifier has become unfalsifiable. Research says roughly 13k lines of service tests need replacement; step 7 accepts that work without an estimate, budget or stop condition. C7 measures useful preliminary outputs, not whether the full rewrite remains affordable. A big bang with useful preparatory steps is still a big bang.
- The Decision boundary conflates parsing with authorization and state-dependent decisions. Accepting a message depends on membership, bans, session generation and current database state. A sealed MessageAccepted constructed before those checks can become stale before application. The design must place authoritative checks and writes in the same transaction, and define when external effects become eligible.
- The oracle proves agreement with selected Python executions. It does not prove forbidden effects never occur, intermediate database states preserve invariants, or races remain safe. A final row diff misses a transient unauthorized fanout followed by cleanup. Normalizing identifiers, tokens and unordered frames can also erase meaningful failures unless the comparator's equivalence rules are independently specified.
- The WebAuthn compatibility gate measures the wrong thing. AAGUID does not establish the attestation format received, and stored credentials describe past enrollment rather than future registration behavior. Restricting formats, changing origin configuration and introducing a custom CBOR parser are deliberate acceptance-policy changes. They contradict the blanket claim of unchanged behavior unless separately specified and tested.
- The switch has no complete release-state model. Two pins, compose drift, standup, seed selection and compatibility guards remain open until step 8. An unchanged schema is insufficient for rollback: incompatible bus/gateway combinations, consumed challenges, refresh-token rotation, pending external effects and file ownership matter. The subsequent drift-authority change also ends the claimed unchanged-schema rollback window without defining its replacement.
- The database authority claim overstates the guarantee. Drift definitions and migration snapshots do not compile-check live schema correctness; the research explicitly establishes runtime validation and exporter gaps. Stamping user_version during setup also turns opening the database into a metadata write before the adoption protocol and recovery behavior are specified.
- The D1 repair is asserted before its replacement path is proven. The bundle shows Category incrementals arriving in Python-repr form and separately shows the consumers rejecting malformed incremental arity. Switching shares establishes delivery, not correct application. This must pass add, remove, reconnect and snapshot/incremental agreement tests before becoming the reference behavior.

**What holds:**
- One gateway owns SQLite writes, sockets, topology and bus ingestion. Moving that ownership together avoids building temporary distributed coordination.
- An observer removes the gateway's unnecessary Actor dispatcher. Explicit support for both known dialects and CI against production pins address measured hazards.
- A real-server harness, real WebSocket clients and recording external-service stubs provide useful regression coverage immediately.
- Keeping Alembic authoritative during development and preserving stored timestamp bytes are sensible compatibility constraints.
- The design correctly excludes production traffic recordings and separates independent Python fixes from the rewrite.

**If RECAST, what to fold back:**
- Before step 5, inventory observable behaviors and security invariants, estimate their replacement cost, and set an explicit go/no-go threshold with Nick. Apply the original fallback if the threshold fails.
- Replace universal deterministic replay with scripted conformance tests and narrow golden vectors. Add controlled clocks only where needed. Specify which values and orderings may differ; verify token claims, signatures, identifier relationships and deadlines directly.
- Define Decisions as authenticated commands carrying provenance. Recheck mutable authorization inside the owning transaction. Specify commit, fanout, push and bus-publication ordering, including failures between each pair of effects.
- Add adversarial schedules for ban versus send, deletion versus push, refresh-token reuse, challenge replay, topology removal and echo arrival. Give each invariant an independent expected result rather than accepting Python agreement as sufficient.
- Make WebAuthn a separate policy and compatibility gate covering existing COSE rows, supported algorithms, representative enrollment clients and malformed inputs. Use the AAGUID census only as supporting evidence.
- Move the two-image release contract and native-asset packaging proof ahead of broad implementation. Rehearse every allowed upgrade and rollback state, define readiness and rollback triggers, and state exactly when Python rollback ceases to be supported.
- Make schema adoption explicit, exclusive and recoverable; validate the complete schema before stamping metadata. Describe schema safety as checked migration discipline. Dijkstra: "Simplicity is prerequisite for reliability."


---

## Tesla, the Arc-Prophet's Design Strike

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Verdict:** RECAST

**Summary:** The one-boot skeleton is in tune, and the oracle is tuned to a harmonic: it canonicalises away the bytes a rollback must read, and the sealed Decision never leaves the box, so the ledger stays quiet until the first unscripted input shakes the island to glass.

**Fatal flaws:**
- DESIGN §9, §2.3, and §3 steps 5–6. The second cast threw out the decision tape and left the applier with no input socket. Episodes enter through the wire, and the parsers that turn bytes into a `Decision` are the *next* step. A green ledger names an episode. The variant that fired is inaudible. Design 15 is quoted and then locked inside the process under test, which is the third time this score has been named and hidden (the temper, the fusion, §9). The frequency that breaks it is an applier branch no scripted journey takes: choice versus default, which source wins, all or none — the same three defects the port claims to make into compile errors.
- DESIGN §2.2 against §0 and claim C3, with RESEARCH §A3. Normalisation binds ids, replaces clocks, and compares fan-out as a set. Rollback needs the raw stored bytes: DATETIME with a space, booleans as 0/1, JSON text, py_webauthn's re-encoded COSE. A normaliser that parses a date will call `'… …'` and `'…T…Z'` the same instant, and the history mis-sorts the moment Python is restored. `validateDatabaseSchema` compares affinity, and BOOLEAN, DATETIME, and JSON all fall to the same affinity, so the §2.5 CI check stays green on that exact frequency.
- DESIGN §3 step 0(c) against §2.4 and step 4, and Fold F7. The measured fix is to consume the Category `entries` share. The captured frame is `(update entries.d1probe ((* d1probe * * * ()) None None))`. Step 4 still tells the builder to attach `channel_list`. Built as written, boot-time channels pass and the first channel created at runtime goes silent again — D1, re-struck, because the cast carries two scores. The removal frame was never captured. F7 says a known bug is not a specification; the episode suite will record pre-fix silence if it is not rebuilt after 0(c). Beside that, RESEARCH §A1 Q1(c): the February pin's other incremental encoder is an f-string, and a "more correct" consumer diverges from production. The design counts two chat dialects and has no name for this third wire.
- DESIGN §2.8, step 0(d), and C3. The census reads `aaguid`. The lockout lives in COSE `alg` and in `clientDataJSON`, which that column does not carry. The package stores original COSE bytes; the rows already on disk are py_webauthn's re-encoding. A credential minted on Dart, then rolled back, meets a verifier that has never been shown that spelling. Checks stricter than py_webauthn 3.0.0 (`crossOrigin`, `topOrigin`, 1023-byte ids) reject assertions the live island accepts. The only authenticator we own is P-256, fmt none. It cannot sing the Android client's note. Syd's first sign-in is the performance, at 3am, with the door locked from the inside.
- DESIGN §0 and §4 against §2.7, §7.3, and ISL-0003. The throttle says rollback "restarts the previous Python image" as if that were a mechanism. The switch replaces one image and three commands with two pins, and `update.sh` is built to refuse compose drift. That is a second compose shape. Unstated MQTT client id and session policy: the echo set is empty after the boot, so one redelivery inserts a second message row. §2.1 retired #42 because there is no second live writer. Redelivery is a second delivery to the one writer. F6 names loss, which is today's restart. Duplication is the new harmonic.
- DESIGN §2.5, C8, and open variable 11. Once syd has switched, every release is schema-frozen until melb and the drift ADR. Syd exists to find the bug. The bug a hand-faithful drift finds on a live volume is a schema bug, and the design forbids the migration that answers it. The operator can roll syd back or rush melb onto the same build to end the window.
- RULINGS, the silent-desync rule, against DESIGN §2.3. An unknown wire key is ignored, so a mismatch looks healthy. Nothing binds the Dart HTTP, WS, or CBOR parsers to that law. §2.6 tells the app tab the wire is unchanged, so they will ship an extra field without asking. The first such build is a 4xx storm only on the island that already switched.

**What holds:**
- One writer, one boot, process memory moving together. The three sparks that never saw the route menu still chose this, and RESEARCH §A2 agrees. #46 stays a good idea and stops being a toll on the port.
- The gateway as observer, service id 0, no `topic_in`. The latent "post as anyone" surface goes quiet.
- Two explicit filters, `:0` and `:1`, so a future `:2` fails loud. Prod pins on the rig. The pinned `bus-e2e` job. D1 fixed in Python before it can be recorded as truth — the instinct is right; the target share is what the cast then loses.
- No tape in production. The three privacy rulings survive.
- Alembic stays authority while building. FK stays off. Cascades stay code. `passkeys_server` stays rejected. Shelf stays the server. The WebAuthn package gets its own strike and is not published early.
- The image honesty in §1 and §4. The registrar and ChatServer keep their Python, and the 143 MB stays. CRUCIBLE's "the runtime drops" is corrected here.
- Step 0(e). A type checker in CI really does dissolve the `assert_never` motivation, and the design keeps it as a step beside Nick's ruling rather than pretending it replaces the port.

**If RECAST, what to fold back:**
- In §2.2 and C3, split the oracle into two equalities. The semantic ledger may canonicalise ids, clocks, and unordered fan-out. DATETIME, BOOLEAN, JSON, and `public_key` are compared as raw stored bytes, both directions: Dart reads a Python-written alembic seed, Python reads a Dart-written copy. The normaliser is forbidden to parse those columns. Add that rollback episode to step 2, beside C1.
- Give the harness a decision trace that is compiled out of prod and is never a table in the island database: each episode records the `Decision` sequence, and a mismatch names the variant. Step 5 injects decisions at that seam so the applier can go green before the parsers exist. This is the fusion's construct-oracle, not a production `decision_log`.
- Move the C2 falsifier to the end of step 2. Publish the map from the in-memory machines (echo, rate limit, topology queue, provider-token caches, the 16-table purge) to the episode that hits each branch. If the map cannot be written, stop and take R5 before step 3. In C9, a Python gateway change that alters observable behaviour fails CI until its episode exists.
- Make step 0(c) override §2.4 and step 4 in one sentence: the observer consumes the Category `entries` share, not `channel_list`. Capture the removal frame. Rebuild the bus episodes after the Python fix. Treat an f-string incremental on the old share as a known February-pin defect, marked per F7, not as a third dialect to emulate.
- In §2.8, census `alg` from the stored COSE, not only `aaguid`. Keep writing py_webauthn's re-encoded `public_key` bytes until rollback is retired. Every check stricter than 3.0.0 stays off until a corpus of assertions the current verifier accepts is green, including the app's `clientDataJSON` shapes. The software authenticator alone is not that corpus.
- Rewrite the §0 throttle. Rollback is a rehearsed compose transition between one image with three commands and two pins, and `update.sh` must know both shapes. State the MQTT client id and clean-session policy so the switch's failure is the loss F6 already accepts. #42 stays a non-prerequisite only under that policy.
- Collapse the freeze. The drift ADR lands in the same change as syd's switch, or syd soaks for a named maximum and then either rolls back or melb switches. Schema work queues behind that window. No release in the window carries an alembic head bump, and a schema-shaped syd bug is a rollback, not a migration.
- In §2.3, bind every parser to the silent-desync rule, and add one episode whose body carries an unknown field and still succeeds. Step 1 injects the clock. C1 includes an episode that crosses the challenge TTL, the JWT expiry, and the 300s lease.


---
---

# Round 2

**Overall verdict (round 2): RECAST (4 of 4).** No DISSOLVE. The findings turned SUBTRACTIVE: three families independently said delete the two-image split, and two said the port does not need the schema-authority change.

## Round 2 synthesis

**Round-1 ledger, as the panel judged it.**
- **Closed:** 2 (in intent), 3, 4, 6 (partly), 7, 9, 10, 11, 13, 15, and 14's timing.
- **Still open:** 1 (WebAuthn), 5 (the switch/drain), 12 (adoption path), and 14's fallback.

**Deduped flaws, most severe first:**
1. **WebAuthn parity is a scope bomb, and has no transactional home.** Raised by Noether, Carnot and Tesla, with Maxwell on the instrument.
   - py_webauthn verifies every attestation format that arrives (tpm, apple, android-key, safetynet, fido-u2f, packed with certs).
   - "Strict DER" is a second accept-set.
   - Challenge consume, credential insert and sign-count must commit in one transaction.
   - The staging RP needs app builds that do not exist.
2. **Deploy over-complication.** Raised by Noether, Carnot, Tesla and Maxwell. The two images (`BUS_IMAGE`, two compose shapes) save ~34 MB while Python stays anyway. The switch protocol rebuilds `update.sh`.
3. **The schema authority change is not needed by the port.** Raised by Noether, Tesla and Carnot (decouple).
   - The adoption path does not run as written: `validateDropped:true` rejects `alembic_version`, `onCreate` runs at `user_version` 0, and the validator re-enters the open path.
   - The authority change creates the freeze and the rollback cliff.
4. **The licensing "fallback".** Raised by Noether, Carnot and Tesla.
   - **CHECKED against fact before folding.** All 52 commits in `aiko_services_dart` are Nick's, and upstream `aiko_services` is Apache-2.0. So it is not a third party's unlicensed code; the gate is Nick's licensing decision, with the Andy ask as his chosen courtesy.
   - The panel's conclusion ("vendoring is a violation") rested on a wrong premise. Its fold ("delete the vendoring escape hatch") still lands: silence must not select anything.
5. **The drain cannot converge, and readiness is premature.** Raised by Carnot and Tesla. The producer stays live, so the topology queue never empties. Existing sockets keep submitting. Push retries are in-process. Ready must mean "snapshot applied".
6. **Effects after revocation have no linearisation rule.** Raised by Carnot. Kill-after-commit has no expected world, and a retry would duplicate (Tesla).
7. **F7's known-bug mark is an unbounded exception.** Raised by Carnot and Tesla. It must never cover stored bytes or the accept-set.
8. **Rollback triggers are undefined.** Raised by Carnot, Tesla and Maxwell: no baselines, no count floors, and a silence trigger that cannot tell a quiet channel from a dead relay.
9. **The injectable Decision seam, the trace and the every-branch coverage map are machinery.** Raised by Carnot and Tesla. Replace them with a fixed journey list carrying success, reject and race obligations.
10. **D1's fix makes a dormant hard-delete live.** Raised by Maxwell (verified: `main.py:136`). The Category share is unmeasured and has no fallback (Noether). Assert exactly one ChatServer match (Noether).
11. **R5 is not cheap.** Raised by Carnot. A Python HTTP shell over Dart decisions needs FFI/IPC. The honest fallback is "keep Python, with the new tests and pyright".
12. **#21 collides with the freeze** (Maxwell). **The §0 throttle is false**, because 0(b) and 0(c) are production changes (Tesla).

**Disposition: RECAST.** Fold into DESIGN round 3. The fold should SHRINK the design. Re-strike once more, round 3 of at most 3, with `/design-temper` reloaded (the producer-first lens merged mid-run, enspyrco/claude-skills#543).
**Struck:** dt-1792894809 · bundle: CRUCIBLE, RULINGS, RESEARCH, SPARK, TEMPER r1 synthesis, DESIGN r2 (104 KB) · landed so far: Maxwell

<!-- arrival order, not ranking -->

## MaxwellMergeSlam's Design Strike (round 2)

**Verdict:** RECAST

**Summary:** Every round-1 flaw I raised is closed, but two folds bought machinery the island already has for free, one fold silently dropped the #21 collision, and the D1 fix the design now leans on is a data-destroying path wearing the label "separable".

`Ellis: "Hans, bubby! I'm your white knight!"` That is a fold that walks in confident and makes it worse.

**Closure check (my round-1 flaws):**
- `/openapi.json`: CLOSED (§2.6, step 1).
- WebAuthn parity: CLOSED in principle (§2.8). Its *instrument* has a new hole (below).
- Settings parser: CLOSED (§2.3).
- byte-for-byte → app-observable: CLOSED (§2.2).
- kill criterion: CLOSED (gate G, R5 named).
- C10: CLOSED as a measurement (step 0g).
- Andy asked early: CLOSED (step 0f).
- "then watch": HALF. Triggers are named "above baseline", but no step measures a baseline. The island has no current numbers for sign-in failures or push ratios on syd.

**Fatal flaws:**
- **[new flaw from a fold: under-counted blast radius] The D1 fix is a data-destroying path, filed as a separable chore.** In `client.py:_on_share_event`, `remove` is *the ONLY hard-delete trigger* for a channel (and its messages, memberships, reactions). Step 0(c) moves the trigger's SOURCE from `channel_list` (which never fires runtime removes) to the Category share (which does). That turns a dormant delete into a live one. If the Category snapshot on reconnect ever omits a channel transiently, or a HyperSpace remove is fired by anything else on the bus, messages are hard-deleted. "Decision B" (disconnect is not removal) was designed against the OLD source's semantics. This is a state-lifecycle change, so it is **cage-match by law**, and it needs an episode for "reconnect snapshot omits X" before it ships. §3 0(c) calls it "proven on add, remove, reconnect and snapshot/incremental agreement" and prices none of the deletion risk.
- **[fold dropped an open variable] #21 now collides with the freeze, and the design no longer says so.** Round 1's open variable 11 ("whether #21 or any other schema-touching work lands inside the window") was deleted in round 2. #21 (per-device sealed-ring capability) is a field on the device row: an alembic migration plus a wire change with the app tab, with its own release timing that the app controls. If it is not shipped before syd's switch, it cannot ship until the authority change. Restore it, name #21, and make gate G check it.
- **[over-complication] The switch protocol rebuilds what `update.sh` already is.**
  - §2.1 adds a Caddy admission stop with 503 + `Retry-After`, a drain step and a separate backup.
  - Today, every release already stops the gateway container (uvicorn drains in-flight requests on SIGTERM within its graceful timeout), takes `update.sh`'s backup, and starts the new one. While the gateway is down, Caddy already answers 502, as on every release.
  - The only genuinely new part is the two-pin compose shape, which §2.7 already owns.
  - Simpler: **the switch is `update.sh` with the new compose shape**, plus a stated graceful-shutdown timeout. Delete the Caddy step.
- **[over-complication] A new health endpoint for the triggers.** §2.9 adds "one added for the triggers". A new observability surface on an island whose standing frame is "the island declines to know" (RULINGS, 2026-09-05) deserves more than half a clause. Simpler: read the triggers from the existing logs during the soak (`docker logs` on the box, which is Nick's hands anyway), and measure the baselines from the same logs on Python first.
- **[the instrument is impossible as specified] The staging-RP device matrix needs an app build that does not exist.** Passkeys are scoped to an RP id. The iOS app can only use a passkey for a domain in its Associated Domains entitlement, and Android needs `assetlinks.json` on that domain plus the app's `apk-key-hash` in our origins. A "staging RP" therefore needs app-tab builds entitled for staging: a cross-tab cost the design never prices. Simpler, within the rulings: **use syd's real RP** (it is the test island) with Nick's own devices and a test account. Capture those assertions against Python as vectors, then verify them offline in Dart. These are tester-generated bytes from consenting testers, not user traffic, so no tape of anyone's use is made.

**What holds:**
- The frame: one writer, one switch, observer not Actor, both dialects, no production tapes.
- Gate G with R5 as a named outcome is the best single fold. It turns "a rewrite you can't stop" into a decision with an exit.
- Conformance tests with independent expected results instead of universal replay: a real subtraction.
- Parity-first WebAuthn and py_webauthn-compatible storage until rollback retires.
- Validate-then-stamp, and Python rollback ends explicitly at the authority change.

**If RECAST, what to fold back:**
- Step 0(c): reclassify D1 as a state-lifecycle change (cage-match by law). Add episodes for "reconnect snapshot omits a channel", "a foreign HyperSpace remove", and "a Category remove for a channel with messages". Decide explicitly whether a Category `remove` may hard-delete, or only soft-mark until confirmed.
- §7: restore the freeze-window variable, naming #21. Gate G checks that #21 has shipped, or is scheduled after the authority change, before syd switches.
- §2.1: collapse the switch protocol to `update.sh` with the two-pin shape plus a stated graceful-shutdown timeout. Delete the Caddy admission step.
- §2.9: drop the new endpoint. Triggers are read from existing logs, and their baselines are measured from Python on syd during steps 1 to 3.
- §2.8: replace the staging RP with syd's real RP, a test account and Nick's devices, with captured vectors verified offline in Dart.


*Maxwell check after striking: a share `remove` reaches `channels_service.hard_delete_channel` (`client.py:169-172` → `main.py:136`), which logs "HARD-DELETED … (+ its messages + memberships)". Confirmed.*


---

## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The design conserves the central symmetry—one writer, one switch, same observable bytes—but the folds break three others: WebAuthn’s accept-set, the bus topology source, and the legal right to use `aiko_services_dart`; and they add machinery (two images, drift authority) that a simpler move dissolves.

**Fatal flaws:**
- **Round-1 #1 is named, not closed: WebAuthn parity is a scope bomb** (§2.8; unstated assumption / under-counted blast radius). §2.8 promises the Dart verifier’s accept-set equals py_webauthn 3.0.0’s. That means implementing every attestation format py_webauthn dispatches—none, fido-u2f, packed, tpm, apple, android-safetynet, android-key—plus its quirks (ignored `crossOrigin`, unparsed `topOrigin`, non-empty-only credential ID). The implementation plan only lists pointycastle ES256/RS256, cryptography Ed25519, and a narrow CBOR parser. The `aaguid`/`alg` census cannot narrow the accept-set without becoming the policy change the fold forbids.
- **WebAuthn verification has no home in the parse→Decision→applier split** (§2.3; missing failure mode / broken one-door invariant). WebAuthn finish is stateful: challenge lookup, single-use consumption, credential public key, sign count, signature verification. If the parser verifies, it reads mutable DB state outside the owning transaction; if the applier verifies, the design must say so and price crypto inside the transaction. As written, the one-door law is broken for the riskiest path.
- **The licensing fallback is not a fallback; it is a violation** (§7.1; broken invariant / legal conservation). The design says if Andy does not answer, “vendor the roughly 1.5k lines used under the island’s AGPL.” `aiko_services_dart` has no licence. You cannot conserve authorship by relicensing unlicensed code. The real options are written permission or a clean-room reimplementation.
- **The `channels` Category `entries` share is an unmeasured replacement** (§2.4; unstated assumption / missing failure mode). The design replaces the broken `channel_list` with a share not measured in RESEARCH. Step 0(c) is supposed to prove it, but the design treats it as settled and gives no fallback if add/remove/reconnect/snapshot agreement fails.
- **The switch protocol does not drain the push queue** (§2.1; missing failure mode / under-counted blast radius). “Push queue empties” within a timeout is undefined when `push_service.schedule_wake` holds in-process future retries. If the timeout fires, are scheduled pushes dropped, duplicated, or migrated? The design accepts bus loss but does not price push loss.
- **Two images at the switch are over-complication** (§2.7; over-complication / coupling to remove). `BUS_IMAGE`, two-pin compose, `update.sh`/preflight changes, two attestations, and native-asset packaging add deploy surface for roughly a 34 MB gateway-layer win; Python remains for registrar and ChatServer. The simpler move is one image with both runtimes during the transition, split later only if the size win matters. This is design-16 generality creep.
- **The drift authority change is over-complication** (§2.5/§2.9 step 9; over-complication / unnecessary coupling). Moving schema authority from Alembic to drift creates a freeze window, a rollback cliff (“Python rollback ends”), and a permanent need to mirror every Alembic migration into drift. The port does not need this. Keep ISL-0001: Alembic is sole authority; run migrations from a Python init container or the bus image; use drift only as a runtime validator. This removes the freeze and the authority-change risk.
- **The two-island soak has no melb-rollback branch** (§2.9; missing failure mode). If melb switches and then rolls back, syd is still on Dart, the schema freeze continues, and the authority change is delayed indefinitely. The window can stay open.
- **Bus discovery with two explicit filters can match both `:0` and `:1`** (§2.4; unstated assumption). The design says two filters, not `*`, but does not say to assert exactly one match. If both exist, which ChatServer wins?
- **Round-1 #2 and #5 are gated, not yet closed** (§2.2, §2.1/§2.9; named but open). The conformance episodes and switch protocol are the right folds, but the effect ledger, decision trace, push drain, and Dart-startup failure path are not specified enough to prove atomicity, ordering, or absence of transient forbidden effects. Gate G is the right stop; until then these remain open.

**What holds:**
- The core symmetry holds: Dart never runs as a second live writer; one boot takes `.aiko-worker.lock`; process memory moves together; #46 and #42 leave the critical path.
- The bus observer, not Actor, with two dialect filters; the February pin as target; the pinned `bus-e2e` job.
- The conformance oracle with independent expected results for invariants, plus app-observable and stored-byte equalities, closes round-1 #2, #3, #10, #11, #15 in intent.
- Parity-first WebAuthn intent is the right frame; it just needs the scope and the transaction home named.
- Alembic stays authority while building; FK off; cascades as code; the `IslandDateTime` write format.
- Gate G with R5 as a named outcome, and step 0 separable fixes, are the right stop conditions.

**If RECAST, what to fold back:**
- **Scope WebAuthn parity explicitly.** Either commit to implementing py_webauthn’s full accept-set, or get Nick’s ruling to narrow it using the `aaguid`/`alg` census with a migration/lockout plan. Put WebAuthn verification inside the applier’s owning transaction and test it with replay, sign-count, and challenge-consumption races.
- **Replace the licensing fallback.** Delete “vendor under AGPL.” Write “written permission or clean-room reimplementation of the four bus operations.”
- **Prove or replace the Category share.** Add the `channels` Category `entries` share to §7 open variables. If step 0(c) fails, fall back to a fixed `channel_list` or a DB-poll topology source.
- **Simplify the deploy.** Keep one image during the transition; defer the two-image split to a separate, post-port optimization. Remove §2.7’s `BUS_IMAGE`, two-pin compose, and `update.sh` changes from the critical path.
- **Keep Alembic as sole authority.** Remove step 9’s authority change. Run migrations in a Python init container or from the bus image; use drift only for runtime validation. This dissolves the freeze window and the rollback cliff.
- **Define the missing failure modes.** Push-queue drain semantics; Dart startup/validation failure rollback; melb rollback branch; bus discovery exactly-one-match assertion.

---

## CarnotCodeCarver's Design Strike (round 2)

**Verdict:** RECAST

**Summary:** The sole-owner port still holds, but several folds name obligations without closing them. The largest remaining holes are WebAuthn parity, post-commit authorisation, shutdown, and schema adoption. Remove the mandatory image split and injectable Decision framework before adding more machinery. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are.

**Fatal flaws:**
- R1 #1 — OPEN. §2.8 promises py_webauthn's entire accept-set, including certificate-bearing attestation formats, but specifies only signature libraries and a narrow CBOR parser. It provides no implementation boundary for packed certificate attestation, TPM, Apple, Android or their verification rules. The device matrix exercises particular devices, not this accept-set. Fold-back: enumerate every supported format and verification dependency, with positive and negative differential vectors; alternatively change the Python registration policy separately before the port. A census cannot substitute for implementing the promised verifier.
- R1 #2 and #8 — PARTIAL. Rechecking authorisation inside the transaction closes stale command acceptance. It does not close stale external effects: send commits, ban commits and disconnects, then send's queued fan-out or push runs. §2.2 forbids that outcome, but §2.3 supplies no ordering mechanism that prevents it. Fold-back: define the linearisation points and serialize revocation with effect dispatch, or explicitly define which already-committed effects remain permitted. Test that exact interleaving with barriers.
- R1 #5 — PARTIAL. Caddy refusing new upgrades does not stop existing WebSockets submitting work, and the MQTT subscriber can keep replenishing the topology queue. Therefore 'drain' has no defined admission boundary. The timeout also lacks an abort-versus-force policy. Fold-back: explicitly quiesce existing socket commands and bus ingestion, name what happens to pending effects, then drain or abort before changing ownership. Prove this with active sockets and continuous bus traffic.
- R1 #12 — PARTIAL. Validation-before-stamping is a requirement, not yet an executable opening path. The research establishes validateDatabaseSchema as a drift_dev runtime helper; the design places it inside NativeDatabase.setup, before drift's version/open machinery, without explaining how it runs without re-entering that machinery. 'Stored-byte checks' also require cross-runtime fixtures and cannot establish themselves merely by opening a live file. Fold-back: prove a separate migration-disabled validation connection or command against all 22 tables, then stamp transactionally. Keep cross-runtime byte conformance in CI and rehearsal.
- R1 #14 — ASK TIMING CLOSED; DEPENDENCY PERMISSION OPEN. Gate G accepts Andy's absence, and §7 offers vendoring unlicensed code under AGPL as a fallback. Copying those lines does not supply permission to redistribute them. Fold-back: require an explicit compatible grant before incorporating this dependency, or independently implement the four-operation adapter. Delete the purported vendoring escape hatch.
- R1 #6 — PARTIAL. The soak has an eventual deadline, but the authority transfer is forced immediately afterward without its own readiness condition. The blueprint admits snapshot/export and customConstraints unknowns, yet step 3 proves adoption rather than an actual drift-owned upgrade. Ending Python rollback is therefore coupled to an unproven migration system. Fold-back: rehearse the first real drift migration, seed creation and recovery before either production switch; require that evidence before ending rollback.
- NEW — 'Known bugs are marked' is an unbounded exception to parity. Any mismatch can be labelled F7 and escape the port's acceptance contract, including behaviour the app relies on. Fold-back: freeze an explicit exception list before gate G; every exception needs independent expected behaviour and a separately approved compatibility decision. New discoveries must update that list through the same review.
- NEW — Rollback triggers are named but not operationally defined. 'Above baseline' has no interval, minimum sample or threshold; 'normal quiet period' cannot distinguish a broken relay from a naturally quiet channel. A new health endpoint cannot infer those missing facts. Fold-back: specify bounded measurements and decision rules at gate G, and use a synthetic relay probe carrying no user traffic. Rehearse each trigger causing the actual rollback.

**What holds:**
- R1 #2 — The replacement of universal replay with independently specified conformance episodes is a substantive closure of the oracle's original conceptual flaw. Atomicity, forbidden effects and schedules now have explicit obligations; the effect-ordering hole above remains.
- R1 #3 — CLOSED as a conformance requirement: raw storage comparison and bidirectional reads preserve what the normaliser erased. Exact JSON byte equality may exceed rollback's actual needs, but it no longer hides incompatible representations.
- R1 #4 — CLOSED at this design stage: a coverage map, estimate and explicit stop before the bulk implementation are legitimate gates. Their existence does not prove C7's claim that all preliminary work pays for itself.
- R1 #7 — CLOSED structurally: Python fixes the topology source first, and add/remove/reconnect/snapshot tests precede Dart reliance. The replacement share remains an explicit feasibility test.
- R1 #9 — CLOSED for retaining /openapi.json. Serving the frozen artifact preserves its existence. Route-name parity alone does not establish schema parity; request and response conformance must carry that obligation.
- R1 #10 — CLOSED for application envelope comparisons and unknown request fields. Narrow the blanket 'every parser' wording: compatibility rules for HTTP fields do not automatically define safe handling of unknown cryptographic structures.
- R1 #11 — CLOSED: effective Settings now has a differential vector contract.
- R1 #13 — CLOSED: the defect-location premise is measured before it determines implementation order.
- R1 #15 — CLOSED as a test obligation: expiry boundaries are explicit. Ensure injected time reaches every tested expiry source rather than only the HTTP layer.
- One live owner, no production tapes, an observer rather than an Actor, production-pinned bus testing, FK-off cascades and Alembic authority during construction remain sound constraints.

**If RECAST, what to fold back:**
- Delete the mandatory two-image transition from the port. Keep the existing image and release contract, add the Dart executable, and switch only the gateway command. Python already remains for the bus. Split images later if the measured size benefit warrants the extra pin, compose states and rollback matrix.
- Remove the universal injectable Decision seam unless a concrete service boundary needs it. Typed commands can remain ordinary internal values. Drive conformance through public inputs and test transaction-owning services directly where fault barriers are needed; this removes a second harness API and trace maintenance without sacrificing the sole-owner switch.
- Do not treat R5 as an inexpensive fallback yet. 'Dart decisions inside a Python HTTP shell' introduces an unspecified FFI or IPC boundary and transaction owner. Either design that small boundary explicitly or make gate G's fallback retain Python with the improved tests and type checking.
- Move the irreversible schema-authority change behind its own proven readiness gate. A bounded production soak and a migration-tool replacement are separate decisions; coupling them creates avoidable entropy.
- Keep the conformance suite, but replace coverage-by-one-episode with a finite obligation map: each critical invariant needs its success case, rejected case and relevant race or failure case. A branch being visited is not evidence that its invariant survives.
- Dijkstra: "Simplicity is prerequisite for reliability." The next fold should subtract release states and test interfaces while making the remaining security and shutdown contracts executable.


---


## Tesla, the Arc-Prophet's Design Strike (round 2)

*Verbatim, except one leading sentence of the seat narrating its own read ("The strike is incomplete until the omitted middle of the design is in hand…"), which ran into the header on the same line. A cleanup `sed` dropped the header with it; restored here.*

**Verdict:** RECAST

**Summary:** The one-boot frame is still the right frequency, but three round-1 folds only named their flaw, and the folds that did close grew a second product — a verifier, a schema mirror, and a two-pin release — that will arc to ground at the switch.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**

- **Flaw 5 is still open (§2.1, §2.9).** The drain condition cannot converge. Admission stops at Caddy; the ChatServer keeps publishing, so "the topology queue empties" is a wait on an open input. Every real switch therefore takes the timeout, and the protocol never says what the timeout means. Step 5 then reports ready after the schema check alone, and step 6 re-admits traffic while the Category snapshot is still in flight. A channel born in that window is in the bus and not in the file; the silence trigger does not see it, because the silence trigger is tuned to a healthy quiet island — absence of chat, which is also what a dead relay sounds like. The MQTT policy, the backup, and the end of Python rollback are real. The drain is a drawing of a drain.
- **Flaw 1 is still open (§2.8).** Parity-first is the right law, and re-encoded `public_key`, the later-PR list, the `alg` census, and the device matrix all belong to it. The same paragraph then builds the verifier out of "strict DER and range checks." That is a second accept-set, smuggled back in as a library choice. py_webauthn 3.0.0 also patches a known-bad EdDSA header and re-encodes COSE; neither quirk is in the parity list. The matrix can discover the lockout only after steps 5–7 have already built the stricter primitive. A person who can sign in on Friday cannot sign in on Saturday, and the row in `passkey_credentials` looks perfect.
- **Flaw 12 is still open (§2.5).** Stamp-after-validate is written down, and it is not yet true. `validateDropped: true` treats `alembic_version` as a foreign body, so the boot check fails on every island file the moment it is turned on (RESEARCH §A3: that flag exists specifically so extras such as `alembic_version` are ignored). `enableMigrations: false` and `schemaVersion` are absent; with `user_version` still 0, drift's own open path runs `onCreate` before the four-step story gets a vote. "Opening the file has written nothing" is a wish about drift's executor, not a property the section establishes. The bidirectional stored-byte check is placed on that same boot path, where Python is not present to read anything.
- **Fold 8 closed the stale verdict and opened a write outside the transaction (§2.3).** A `Decision` that carries no "is allowed" is the right shape, and re-checking membership, ban, and session generation inside the owning transaction matches the one-door law. The parsers are still the things that "turn bytes into a Decision," and WebAuthn's challenge consume is a guarded `UPDATE` that must commit with the credential row, or not at all. A parser that consumes the challenge, and an applier that then rolls back, burns a single-use challenge; a crash the other way round leaves a credential that can be replayed. Sign-count updates have the same seam. The fold never names which writes are part of the command and which writes are the transaction.
- **The trace will grow a retry (§2.2, interacting with §2.1).** "Kill between commit and bus publish" is listed and given no expected world. The harness-only trace makes that hole visible for the first time: `MessageAccepted` committed, publish absent. The resonant repair is an at-least-once retry. There is no outbox, `#42` was correctly kept off the critical path, and the MQTT section forbids redelivery only for the broker session. An applier retry is a different oscillator, and it inserts the duplicate row the round-1 check thought it had retired.
- **F7 against C3 (§2.2).** "Dart need not reproduce it" is an unaudited licence to diverge. Nothing says a known-bug mark is forbidden on stored bytes, date spelling, or the WebAuthn accept-set. Rollback is Python reading Dart's file. One marked bug in a writer and the nightly byte check becomes the rollback trigger, or worse, nobody marks it and Python mis-reads a column it used to write.
- **Over-complication, the way design 16 died (§2.5, §2.7, §3 steps 2–3, gate G).** Three machines are doing work the ore never asked for. The faithful 22-table drift mirror, with `customConstraint` archaeology and a `user_version` stamp, survived the withdrawal of the compile-time claim; what remains is a second schema authority kept honest by CI. The two-image contract (`BUS_IMAGE`, both compose shapes, `update.sh` taught a new language) is scheduled at step 3, before gate G, to save the ~34 MB gateway layer while registrar and ChatServer keep Python anyway (RESEARCH §A5). On an R5 or stop outcome that machinery is a deploy migration with nothing to deploy. The coverage map of every in-memory machine is a verification product placed ahead of the port. Simpler moves: raw SQL against the alembic file until the authority change, with a `sqlite_master` dump as the CI oracle; one image and one pin until the Python gateway is actually gone; a fixed episode list (register, sign in, join, send, echo, ban, delete-account, challenge-replay, kill-after-commit) and a gate that sits on the size estimate before any of that machinery is built.

Round-1 ledger, so a closed item is not re-litigated: **closed** — 2 (sequence, independent results, faults, no production tape), 3 (raw-byte equality as a test), 4 (gate G exists, with R5 and stop), 6 (the window ends; melb or rollback; Python rollback ends at the authority change), 7 (leave `channel_list`; prove the Category path on the rig), 8's recheck itself, 9, 10, 11, 13, 15, and 14's timing (the ask is at step 0). **Still open** — 1, 5, 12, and 14's fallback (silence may still vendor ~1.5k unlicensed lines into an AGPL binary; §7 item 1).

**What holds:**

- One writer, one boot, process memory moving together. `#46` and `#42` stay off the critical path for the right reason: there is never a second live writer.
- The gateway is an observer with service id 0, two explicit dialect filters, the February pin as the target, clean session, random client id, QoS 0. A future `:2` fails loudly. No production tape, which is what the three rulings require.
- Alembic remains the authority while building; FK stays off; cascades stay code; `IslandDateTime` writes the space-separated stamp Python already stores.
- WebAuthn policy creep is forbidden in words, storage stays re-encoded COSE, and the device matrix is before the switch. The primitive contradicts the words; the words are still the right law.
- Step 0's separable fixes, especially the Category-share proof and the finish-handler 4xx map, can ship without this design and should.
- Gate G can still say stop, and steps 0, 1, and 4 have a life of their own. Step 3, as written, does not.

**If RECAST, what to fold back:**

- **§2.1.** Write the timeout as the path that actually runs: SIGTERM with the queue still full, then Dart applies a full Category snapshot and catches up incrementals before it is allowed to report ready. Re-admit only after that. Delete "wait until the topology queue empties" as a convergence condition while the producer is alive.
- **§2.9.** Replace the quiet-period silence trigger with a positive heartbeat: the observer's subscription is up and a sync has been applied. Keep sign-in and 5xx triggers, and give them a count floor so one failure on a small island is not an infinite rate.
- **§2.8.** The parity verifier agrees with py_webauthn 3.0.0 on its own fixtures, including non-canonical signatures and the EdDSA header patch. Move "strict DER and range checks" onto the later-PR list beside `crossOrigin` and the 1023-byte cap. Add one sentence: a known-bug mark may not change the accept-set.
- **§2.5.** During the port, Dart opens the alembic file and does not adopt it. No `user_version` stamp, no migrations, `validateDropped` left off. Boot is read-your-rows SQL plus `busy_timeout=5000`. The bidirectional stored-byte check lives only in CI (C3). A checked-in `sqlite_master` dump from the alembic seed is the schema oracle. Drift's typed mirror, if it is still wanted, begins at the authority change, not at step 3.
- **§2.3.** Parsers return a command and write nothing. Challenge consumption, credential insert, sign-count update, and session-generation bump happen in the applier's one transaction. State that list; it is short.
- **§2.2.** The kill between commit and publish expects: row kept, effect dropped, no retry, no outbox. A known-bug mark may not cover `DATETIME`, `BOOLEAN`, `JSON`, or `public_key` bytes.
- **§2.7 and §3.** Remove `BUS_IMAGE` and the two-compose release contract from this design. One image, today's `update.sh`, until the Python gateway is gone. Move gate G to the moment the episode estimate exists, and make the episode list the nine journeys above rather than a coverage map of every branch. §7 item 1: Andy's silence does not select vendoring; only his yes, or a Nick exception written down, does.
- **§0.** "Nothing runs in production before step 8" is already false: 0(b) and 0(c) are production behavior. Say so, or the throttle is a lullaby.


---
---

# Round 3 (final)

**Overall verdict (round 3): RECAST (4 of 4).** No DISSOLVE. This was the first strike carrying the PRODUCER FIRST lens, and every seat answered it.

## Round 3 synthesis

**Round-2 ledger, per the panel.** Closed:
- 2: one image, `GATEWAY_IMPL`;
- 3: alembic authority, Dart as client, no freeze or cliff;
- 4: licensing as a decision;
- 6: the linearisation rule and journey 10;
- 7: the bounded known-bug list;
- 8: thresholds at gate G (manual);
- 11: R5 replaced by "stop";
- 12.

Converted to Nick decisions: 1 (the WebAuthn transactional home is closed; registration is a). Still open: 5 (now the ready gate) and 10 (hard-delete authority).

**Deduped flaws, most severe first:**
1. **The ready gate makes HTTP availability depend on the bus.** Raised by ALL FOUR. At the prod pin, a stale retained `(primary found …)` can stall discovery forever (RESEARCH Q1c), Caddy does no health checking, and so the island becomes a permanent 502, sign-in and history included. The gate also cannot be satisfied by the `channel_list` fallback (Carnot). And "exactly as during every restart today" is false (Noether).
   - **Producer first:** the harm the gate guarded (a reconcile acting on a partial view) is produced by the reconcile path. Disarm reconcile until the first full snapshot, and the gate is unnecessary.
   - **Fold:** bind after the lock and the DB open, as Python does. Topology reconcile is disarmed until one full snapshot from the single chosen source. Bus-dependent actions (publish, relay) refuse until then; HTTP reads are never gated.
2. **A bus `remove` still has authority to hard-delete history.** Raised by Carnot, Tesla and Maxwell. The Category transition also ships in 0(c) before the harness exists (Tesla), and a re-lease fallback could become a second live feed.
   - **Producer first:** the gateway's `remove` → `hard_delete_channel` cascade is ours.
   - **Fold:** a bus `remove` unsubscribes and marks, and never deletes. Deliberate deletion goes through the owning service, attended. One topology source at a time: the re-lease is a *replacement*, never a second subscription. The Category switch leaves the port's critical path and is reviewed separately (#24). This is still Nick's ruling, but now as a recommendation.
3. **The journeys do not cover what steps 5 and 6 build.** Raised by Tesla. Moderation (ban disconnect and fan-out), calls, OAuth and ID-token sign-in, and recovery re-key are absent, so gate G's estimate is blind again. Journey 5's early echo has no expected world.
   - **Fold:** extend the list to every user-visible family before the estimate. Anything without a journey stays out of the Dart binary syd runs.
4. **The licence is selected by presence in the image.** Raised by Tesla. Step 3 puts the Dart executable into the published image before Nick's licence act; `GATEWAY_IMPL=python` is not the act.
   - **Fold:** CI proves the build. Published images exclude the Dart gateway until the licence is decided.
5. **Exactly-one has no actuator** (Noether: `/health` is read by nothing), **and the two-dialect producer is ours** (Maxwell). Tesla: keep the assertion.
   - **Fold:** the prod build matches ONE protocol, derived from the pinned ARG (`:1` only behind a canary build flag). On zero or two matches, the bus capability is never granted: no publish, no subscribe. The condition is visible in the logs that §2.9's triggers read.
6. **The `GATEWAY_IMPL` failure and rollback contract is missing.** Raised by Noether and Tesla.
   - **Fold:** the entrypoint never falls back (a crash is a crash). Configure commits, then restarts, then verifies; a failed verification does not revert, so rollback is reachable while down. The switch is proved by the new process's identity, not by a serve-check against the old one. Compose forwards the key.
   - Plus the one-image-many-roles audit (memory `feedback_one_image_many_roles_audit`): HEALTHCHECK, entrypoint and env defaults hold for both implementations.
7. **WebAuthn sign-in must not shrink to a census.** Raised by Tesla and Noether.
   - **Fold:** sign-in always implements the ES256/RS256/EdDSA ceiling; the census orders the work only. Option (a) is reframed as *reproduce py_webauthn's actual accept-set, including its non-checks* (no trust roots except Apple's), which is far smaller than "implement every format".
8. **C9 was false.** Raised by Tesla. The dual implementation lasts until a later decision deletes the Python gateway, and CI requires both binaries to meet the journeys. Dart's expected alembic head is read from the image's own alembic, not a hand constant.
9. **The query layer.** Carnot: prefer `package:sqlite3` directly, because migration machinery with no migrations is just another refusal path.
10. **Minor.** Name the injected-clock mechanism (a test-only control, compiled out). Define "invariant 7b" (compose forwards every `Settings` field the image reads; CLAUDE.md memory `concept_inert_var_config_not_forwarded`).

**Named tradeoffs (owner + cost + rationale):**
- **Nick:**
  - two gateways in one image (wider attack surface) until the Python gateway's later deletion, in exchange for rollback that never ends;
  - the image stays Python-sized;
  - rollback triggers are manual, read from logs (an automatic flip would be a new flapping producer);
  - restart losses are accepted as today;
  - the registration policy;
  - an effect computed before a revocation's commit may reach a socket before the disconnect lands (Noether: acceptable, recorded).
- **Migration authors:** keep both gateways compatible with every new alembic head.
- **Gateway maintainers:** the frozen `/openapi.json` and the step-7 inventory.

**Disposition.** RECAST again, and **the 3-round budget is spent**. The skill says to STOP and report provisional.

The ten folds above are narrow and mostly deletions. They are applied to `DESIGN.md` as **post-round-3, UNSTRUCK**.

The severity trend argues the case both ways:
- *against* a 4th round: findings fell from 15 structural to 12 subtractive to 10 narrow, and the frame held in all 12 strikes;
- *for* one: round 3's worst finding was a defect round 3's own fold INTRODUCED. A fold that can add a total-outage path can do it again, and Nick has overridden a round cap before when severity, not count, was the signal (memory `feedback_cage_match_stop_signal_is_severity_trend`).

**Nick's call:**
- **(a)** accept the design as PROVISIONAL and go to Blade, with gate G as the next real check; or
- **(b)** one more strike on the post-r3 folds only.
**Struck:** dt-1792895490 · first strike carrying the PRODUCER FIRST lens (enspyrco/claude-skills#543) · bundle: CRUCIBLE, RULINGS, RESEARCH, SPARK, TEMPER r1+r2 syntheses, DESIGN r3 (106 KB) · landed so far: Maxwell

<!-- arrival order, not ranking -->

## MaxwellMergeSlam's Design Strike (round 3)

**Verdict:** RECAST (narrow: three producer-first deletions, no structural change)

**Summary:** The subtraction worked, and the frame now holds. But three of round 3's own new checks guard outcomes that our own code produces and could simply be unable to produce, and one of them quietly makes the whole island's HTTP availability depend on the bus.

`Hans Gruber: "When you steal $600, you can just disappear. When you steal 600 million, they will find you, unless they think you're already dead."` A check that guards our own producer is a dead man nobody went looking for.

**Producer first:**
- **Ready-after-snapshot (§2.1).**
  - *Harm:* a channel created during the switch is missing from the new gateway's subscriptions.
  - *Producer:* relying on incrementals to learn topology.
  - *But:* a new lease always delivers a full snapshot (measured in D1), so the harm self-heals when the snapshot lands, and the producer is already neutralised by the lease mechanism.
  - The gate then **produces a new harm of its own:** if mosquitto or the ChatServer is down at boot, Dart never binds its HTTP port, and the island serves NOTHING (sign-in, history, DMs, push registration) because of the bus. Today's Python serves regardless and connects to the bus lazily.
  - **Delete the gate.** Serve immediately, as Python does. The snapshot arrives when the bus does, and bus health is a heartbeat trigger (§2.9), not a precondition for HTTP.
- **Exactly-one ChatServer assertion (§2.4).**
  - *Harm:* the gateway binds to the wrong or a second ChatServer.
  - *Producer:* OUR discovery accepting two dialects (`:0` and `:1`) in production. `:1` exists only because CI's `bus-e2e` runs geekscape master.
  - Once #23 adds the pinned job (step 0a), production needs exactly the dialect its pinned `aiko_chat` speaks, which is known at build time from the Dockerfile ARG.
  - **Make dual-match impossible:** the production build matches ONE protocol string, derived from the pinned ARG (the Dart equivalent of Python importing `get_server_service_filter()` from the co-installed aiko_chat). `:1` support lives behind a build flag for the canary job only. The assertion becomes unnecessary.
- **Journey 9 plus Nick's hard-delete ruling plus "a snapshot that omits a channel → no hard-delete" (§2.4, #24).**
  - *Harm:* messages, memberships and reactions destroyed because a bus event said `remove`.
  - *Producer:* OUR reconcile worker calling `hard_delete_channel` on a bus `remove` (`main.py:136`). The trigger (a HyperSpace remove, a transient snapshot gap) is external; the destruction is ours.
  - **Make it impossible:** a bus `remove` only UNSUBSCRIBES and marks the channel `removed_upstream`. Hard deletion becomes an explicit, attended action (operator or moderator, through the owning service), never a reaction to an unattended bus event.
  - That is the lens's own example ("a dangerous step moved out of the path that runs unattended"). The snapshot-omission guard and most of journey 9's defensive cases become unnecessary.
  - It is still Nick's ruling (it changes behaviour), but the design should RECOMMEND this, not merely ask.
- **`alembic_version` assertion at Dart open (§2.5).**
  - *Producer of skew:* an image whose Dart binary and alembic directory disagree. One image built from one commit makes that structurally impossible on the normal path, because the entrypoint migrates to the head the same build carries.
  - *Remaining trigger:* an operator restoring a NEWER volume under an OLDER image (rollback across a migration). That is external, and alembic already refuses it (#392). **Keep the assertion**, for that stated reason only.
- **Stored-byte CI equality (§2.2).**
  - *Producer:* two libraries writing the same columns. That cannot be removed while two gateways exist (the port IS the second writer).
  - The producer-side fix already exists (`IslandDateTime`, re-encoded COSE), and the CI check verifies it. **Keep.**
- **Rollback triggers and the heartbeat (§2.9).**
  - *Producer:* Dart defects, which cannot be removed in advance. **Keep.**

**Fatal flaws:**
- **[producer-first] Delete ready-after-snapshot.** As cast, it turns a bus outage into a full island outage, which is a regression against today. Serve immediately; bus state is eventually consistent by the lease snapshot.
- **[producer-first] Make dual-dialect discovery impossible in production builds.** Delete the exactly-one assertion in its favour.
- **[producer-first, Nick's call] A bus `remove` must not be able to hard-delete.** Recommend unsubscribe-and-mark, with hard deletion attended. #24's risk shrinks from "can destroy messages" to "can hide a channel", and journey 9 sheds its defensive cases.

**What holds:**
- **One image with `GATEWAY_IMPL`.** Rollback that never ends is the single best change in three rounds: it deletes the freeze, the cliff, the second pin and the drain protocol at once.
- **Alembic as authority, with Dart as a client** that never writes on open (C4's probe still owed).
- **The ten journeys with obligations,** the linearisation rule, and journey 10's "row kept, publish dropped, no retry".
- **WebAuthn split into authentication parity and a registration choice** for Nick.
- **The licensing finding,** checked against fact; silence selects nothing.
- **Gate G with "stop"** as an honest outcome, and steps 0 to 4 paying for themselves.

**If RECAST, what to fold back:**
- §2.1: delete "ready means … Category snapshot". Dart binds when the DB opens, as Python does. Bus health feeds the heartbeat trigger only.
- §2.4: the production build matches the single protocol of its pinned aiko_chat, derived from the Dockerfile ARG. `:1` sits behind a canary build flag. Delete the exactly-one assertion. Update C5.
- §2.4, §3 0(c), §7.4: recommend that a bus `remove` unsubscribes and marks `removed_upstream`, never hard-deletes; hard deletion is attended. Present it to Nick as the recommended answer to his ruling, and trim journey 9 accordingly.


---

## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** Round 3 conserves one writer, one schema authority, one image and one switch — but it silently breaks the invariant that **HTTP availability is independent of bus availability**, and it claims that broken symmetry is "exactly as during every restart today."

**Producer first:**

- **The ready-before-bind gate (§2.1).** Harm: a user's message is dropped, or a WS subscriber is silently unsubscribed, because Dart served before it knew the topology. Trigger (external, unpreventable): the share reply is late, the registrar is slow, the broker blips. **Producer (ours): the boot ordering — bind the HTTP listener, *then* subscribe — plus process-wide readiness.** It is removable. Make boot a single ordered step (subscribe before bind), and make readiness *per-path*: bus-dependent routes (WS subscribe, bus ingest, publish) return 503 until the observer is up; HTTP reads are never gated. Then the process-wide gate, and the outage it manufactures, become unnecessary.
- **The exactly-one discovery assertion (§2.4).** Harm: the gateway publishes as a user to a dead or stale ChatServer and the message vanishes. Producer: selection among an ambiguous match set. The check is the right *shape* (fail closed, don't pick), but it has no actuator — see Fatal flaw 2. Fix the action, not the flag.
- **The alembic-version assert (§2.5).** Harm: Dart reads a schema it was not built for and returns wrong data. Producer: compiled queries coupled to a mutable schema. Partly removable — the CI oracle (Dart's queries tested against the alembic-built seed, refreshed per migration PR) is the structural fix. The assert is belt-and-braces; not fatal.
- **The stored-byte comparator (§2.2).** Producer: two date/boolean/JSON codecs. Removable — one `IslandDateTime`, one encoder. **Already done**; the comparator is verification, not the fix.
- **The one-transaction service function (§2.3).** Producer: a stateful step split across transactions (spent challenge, no credential). Removable by construction. **Already done** — good producer-first thinking.
- **The rollback triggers (§2.9).** Producer: a bad build that changes behavior silently. Not removable; a signal is required. Review normally.

**Fatal flaws:**

- **F1 — Ready-before-bind couples HTTP liveness to bus liveness, and the parity claim is false (§2.1).** Class: unstated coupling / under-counted blast radius. Today the Python gateway binds after migrations and *before* discovery completes; a bus outage degrades the bus, not the API. This design binds only after a full Category snapshot, so a slow registrar, a broken share, or a broker blip now takes the **entire HTTP surface** down, not just the relay. The sentence "exactly as during every restart today" is the design's own symmetry claim and it does not hold: the outage window is strictly larger and, in the failure case, unbounded. This is the one that must close.
- **F2 — The exactly-one assertion has no actuator (§2.4).** Class: a guard that cannot act. "Fails `/health` loudly, rather than picking one" — but §2.1 states Caddy does no health checking, and no rollback trigger in §2.9 reads `/health`. At the prod pin the stale-retained registration fix (`7bb08db`) is *not present*, so a power cycle can leave two matches and this fires for a reason the design does not own, and nothing happens. A hole in a guard is evidence the guard is the wrong shape: make the gateway **refuse the bus-dependent action** on ambiguity (never publish, never subscribe), and wire the condition to a rollback trigger — or drop the assertion and state the deterministic pick rule.
- **F3 — `GATEWAY_IMPL` failure behavior and rollback reachability are unspecified (§2.1, §2.9).** Class: missing failure mode. If `GATEWAY_IMPL=dart` and Dart fails its DB assert or cannot take `.aiko-worker.lock`, the entrypoint has already `exec`ed it, the container exits and crash-loops, and the island is down. Rollback is "the same setting flipped back," but §2.1 says `island configure` "checks the island serves afterwards" — if it verifies-then-commits, or reverts on verify failure, the operator cannot switch back while the gateway is down. State the entrypoint never falls back (a crash is a crash, as today), and that configure **commits before it verifies**.

**What holds:**

- One writer, one boot; process memory (Hub, echo, rate limiter, topology queue) moves together. #46 and #42 stay off the critical path.
- One image, `GATEWAY_IMPL`, no `BUS_IMAGE`, no second compose shape, no `update.sh` change (r2-2 closed; R11).
- Alembic stays sole authority; Dart is a client that never writes on open; no freeze, no rollback cliff, no 22-table mirror (r2-3 closed; R12; §7.2).
- Licensing: silence selects nothing (r2-4 closed; §7.1).
- Observer, not Actor; two explicit dialect filters; Feb pin; no production tapes.
- The linearisation rule and journey 10 close kill-after-commit (r2-6), with one named edge: an effect computed *before* a revocation's commit may still reach a socket before the disconnect lands. Acceptable, but record it.
- Ten journeys with obligations replace the coverage map, trace and injection seam (r2-9 closed); C2 bounds their coverage explicitly.
- The WebAuthn **transactional home** is closed (§2.3); the registration accept-set is a named Nick decision, not a hidden scope bomb (r2-1 converted, not eliminated).
- Gate G with "stop" as a defined outcome; R5 deleted (r2-11 closed).
- The D1 fix lands in Python first, under cage-match, before the Dart port trusts the share (r2-10 on the Python side).

**If RECAST, what to fold back:**

1. **§2.1 — delete the process-wide ready gate.** Bind the HTTP listener as today; make readiness per-path. Bus-dependent routes (WS subscribe, bus ingest, `send_message` publish) return 503 until the observer has applied its first snapshot; HTTP reads, `/openapi.json` and health are served immediately. Replace "Dart does not bind its HTTP port at all" with "Dart binds, and the bus-dependent paths are closed until the snapshot lands." Then remove the false parity claim, or replace it with the honest one: HTTP availability is no longer coupled to bus availability.
2. **§2.4 — give the exactly-one assertion an actuator.** On zero or two matches, the gateway refuses to publish and refuses to subscribe (capability never granted), and the condition becomes a rollback trigger in §2.9. Do not leave it as a health flag nothing reads.
3. **§2.1, §2.9 — state the failure and rollback contract.** Entrypoint never falls back to Python on Dart failure. `island configure --gateway-impl` writes the setting, then restarts, then verifies; verification failure does not revert the write, so rollback is reachable while the gateway is down. Rehearse both directions on the rig.
4. **§2.4 — one topology source, one snapshot name.** The fallback ("periodic re-lease of `channel_list`") is the mechanism whose *incrementals* D1 says are broken; its *snapshot* works. Say so, and make readiness wait for "a full topology snapshot from whichever source is configured," not specifically "a full Category snapshot."
5. **§2.8 — reframe option (a) as parity, not implementation.** py_webauthn with no trust roots accepts nearly every `fmt` by parsing and not chaining (only Apple's root is checked). Option (a) is "reproduce py_webauthn's actual accept-set, including its non-checks," which is far smaller than "implement every format." Frame it that way so option (b) is chosen on real evidence and the estimate is not inflated.
6. **Minor:** §2.2's "injected clock" is stated as solved but the mechanism is not named (test-only control surface compiled out of prod, or an external clock); §2.1 cites "invariant 7b," which is not defined in this bundle. Name the clock mechanism; define or drop the reference.

Record as a named tradeoff with an owner: **the two-gateway image widens the attack surface for the duration of the port, in exchange for rollback that never ends** (§4) — owner Nick, revisited when the Python gateway is deleted by its own later decision.

---

## CarnotCodeCarver's Design Strike (round 3)

**Verdict:** RECAST

**Summary:** The subtraction substantially holds, but r2-10 remains open: topology discovery still carries authority to erase history, and its fallback contradicts readiness. Fold topology back to relay attachment and remove that fallback dependency. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are.

**Producer first:** Producer first: The harm is users losing stored channel history and memberships after a topology change. A Category removal or incomplete reconciliation is the trigger; the gateway's remove → hard_delete_channel cascade is the producer. That coupling can be deleted: bus topology should control relay attachment, while an explicitly authorised owning service controls deletion. The schema and deploy subtraction already remove other producers: Dart cannot migrate the file, and two gateways cannot own it concurrently. Discovery and cryptographic validation remain necessary because the gateway must interpret external service identities and untrusted authentication bytes.

**Fatal flaws:**
- Make accidental history erasure impossible: remove bus topology's authority to hard-delete channels, messages and memberships; then Nick's hard-delete permission gate and destructive-removal safeguards become unnecessary. §2.4 acknowledges the newly activated cascade but leaves its disposition to a later ruling. Under the requested final-round standard, r2-10 is named, not closed. This production behaviour change is also unnecessary to replacing the gateway executable.
- The topology fallback cannot satisfy the startup contract. §2.4 permits periodic channel_list snapshots when Category fails, but §2.1 forbids binding HTTP until a full Category snapshot has been applied. Selecting the documented fallback therefore leaves the replacement unavailable. Delete the unproven Category transition from the port's critical path and use one selected snapshot source consistently for startup, renewal and reconnect.

**What holds:**
- R2-2, 3, 4, 11 and 12 close structurally: one image, one pin, Alembic authority, explicit licensing, an honest stop outcome, and no schema freeze. This removes machinery and entropy.
- R2-5 closes for the stated availability contract: an ordinary restart accepts dropped push retries and unsubscribed bus traffic; positive readiness replaces an impossible drain.
- R2-6 and 7 close at the design level: committed-message effects have an explicit ordering rule, kill-after-commit has an expected result, and known-bug exceptions cannot excuse authentication or storage divergence.
- R2-1 has a workable transactional home. Implementing full registration parity or separately narrowing Python first are honest alternatives, provided Nick selects the target before approving its cost. A census informs that policy decision; it cannot prove an accept-set.
- R2-8 and 9 have concrete implementation obligations: Nick sets rollback thresholds and soak duration at gate G; ten journeys retain reject, race and fault expectations, supplemented by converted tests and the step-7 inventory.
- Keeping both gateways and maintaining compatibility with every new Alembic head are continuing costs, not fatal flaws. Nick owns that compatibility commitment until Python rollback is explicitly retired.

**If RECAST, what to fold back:**
- Keep topology removal non-destructive: detach the relay and preserve stored state. Route deliberate deletion through the existing authorised owning service.
- Select one topology snapshot mechanism before implementation approval. Define readiness and heartbeat against that same mechanism; Category can remain a separately reviewed improvement.
- Record owners for the remaining tradeoffs: Nick for registration policy, restart losses and rollback lifetime; migration authors for both-gateway schema compatibility; gateway maintainers for the frozen OpenAPI contract and test inventory.
- Prefer sqlite3 directly unless drift provides a demonstrated query benefit. With Alembic owning DDL and no generated table mirror, retaining migration machinery merely creates another refusal path. Dijkstra: "Simplicity is prerequisite for reliability."


---

## Tesla, the Arc-Prophet's Design Strike (round 3)

*Verbatim, except a leading sentence of the seat narrating its own read ("The brief is truncated at the bus section…"), which ran into the header; split off.*

**Verdict:** RECAST

**Summary:** The subtraction holds the one-writer frame, and then the ready-gate tunes the whole island to the registrar's note — a frequency the February pin can sustain forever — so the restart that was meant to match today's outage becomes a 502 that does not end.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Producer first:** The new listen-gate (§2.1) is aimed at a real harm: a partial Category view hard-deleting channels, or traffic arriving before the observer has subscribed. The producer of that harm is the reconcile path, and it can be made unable to run before one full snapshot. Once reconcile is dark, the listen-gate is unnecessary, and it is itself the producer of a worse harm: every HTTP route stays unbound for as long as the bus withholds a snapshot. The `alembic_version` assert stays. A volume can sit ahead of an older image, the schema is bytes we parse, and no type can make a wrong head unrepresentable. Derive the stamp from that image's own alembic heads so the assert cannot false-fire inside the image that just migrated. The exactly-one ChatServer assert stays. A broker we do not control can present zero or two `chat_server` matches, and the design already has no pick-one branch. The stored-byte comparison stays while both binaries are allowed to be the writer across a restart. The rollback triggers stay human-actuated. An automatic flip would be a new flapping producer. Their owner is Nick. The census-as-allow-list does not stay. It produces sign-in lockout for a key registered after the count. Always verify the ceiling ES256, RS256 and EdDSA, and the census stops being a gate. The image-build that copies the Dart executable in at step 3 produces the licensing outcome §7.1 forbids. Silence selects nothing only if a release image cannot contain the binary until Nick licenses it. The journey oracle does not cover the switch. Steps 5 and 6 still produce moderation, calls and OAuth with no expected world. Make those paths unable to reach syd by leaving them out of the Dart process until each has a journey inside the step-2 estimate.

**Fatal flaws:**
- §2.1's ready rule is the old non-converging drain, moved onto the listen socket. Ready requires a full Category snapshot before the port opens. RESEARCH Q1(c) measured the production pin: a stale retained `(primary found …)` leaves every client waiting forever, Python and Dart alike. Caddy has no health check, so that wait is a total 502, including history and sign-in, which do not need the share. Journey 10 already accepts a committed row whose publish was dropped. §2.4's `channel_list` re-lease is not in the ready predicate, so the fallback cannot unblock boot. The prod-pin primary wait is the future input. This check is the wrong shape.
- §10 maps r2-9 onto "ten journeys," and the ten are not the surface that flips. §3 step 5 still builds moderation and calls. Step 6 still builds ID tokens and OAuth. Recovery re-key, which disconnects every socket, is in neither the journeys nor that list. Gate G takes the step-2 estimate before any of that mass is counted, so the stop/go that closed round-1 flaw 4 is blind again. Journey 5 names "an echo arriving before commit" and states no world for it. A named race with no expected result is the linearisation fold left open.
- §2.4 says the Category share does not ship until journey 9 is green and Nick has ruled on hard-delete. §3 step 0(c) ships that share in production Python before the harness in steps 1–2 exists. Today `remove` already calls `hard_delete_channel` (`client.py:169-172` → `main.py:136`) and has never fired under a live lease. Connecting it first, testing it second, is the dormant delete waking at 3am on a glitch. The fallback sentence ("bounds staleness by the period") can be read as a second live feed beside Category. Two topology producers and one hard-delete function is a split-brain delete.
- §7.1 says the dependency enters a release only after Nick licenses it, and silence selects nothing. §3 step 3 puts the AOT executable into today's image, and unrelated releases are not frozen. The attested image is the bundle. Presence in the layer is the selection §7.1 says must not happen. CRUCIBLE's licensing hole is the AGPL image carrying the library, whether or not `GATEWAY_IMPL` defaults to python.

**What holds:**
- One process, one lock, one boot. Rollback is the same setting on the same volume. `#46` and `#42` stay off the critical path. Restart losses match today's: in-process push retries dropped, publishes during the gap dropped, no retry, no duplicate (journey 10).
- Alembic remains the only schema authority. No `user_version` stamp, no freeze, no authority cliff. FK stays off. Cascades stay code. `IslandDateTime` writes the space-separated form. The drift-versus-`sqlite3` probe fails closed if `onCreate` runs on `user_version=0`.
- Observer, not Actor. The February pin is the target. `:0` and `:1` are explicit. Exactly one match. MQTT stays clean session, random client id, QoS 0, which is why a redelivery duplicate does not fire under this policy.
- WebAuthn's round-2 bomb is closed as a decision, not as a smuggled policy. Authentication parity includes non-canonical DER, the EdDSA header patch, and re-encoded COSE. Registration is Nick's (a) or (b) at gate G. Challenge, credential and sign-count share one transaction (§2.3). Stored form stays Python-readable for as long as rollback exists.
- R5 is gone. Two images are gone. The known-bug list cannot cover stored bytes, the accept-set, or anything the app reads. `/openapi.json` and the settings vector are priced. Gate G may stop, and steps 0–4 still pay.
- Named tradeoffs, not fatals, to record with an owner. Nick owns them. The image stays Python-sized plus one executable until a later decision (§4). Both binaries remain behavior-locked with no end date. §4 states that. C9's "bounded by gate G's estimate" is the false sentence. Soak triggers are manual, read from existing logs, with N and M set at gate G. Nick is the watcher. No actuator flips `GATEWAY_IMPL`.

**If RECAST, what to fold back:**
- §2.1: bind after the worker lock and a successful open of the alembic file. Hard-delete and any other topology reconcile stay unconnected until one full snapshot has been applied from the single chosen source. `/health` may report the bus. The listen socket may not wait on it. Cite the stale-primary wait in the claim beside C5.
- §2.2 and §3: before the step-2 estimate, extend the journey list to every user-visible family steps 5–6 still name — moderation (ban disconnect and fanout), calls, OAuth and ID-token sign-in, recovery re-key — each with a success, a reject, and a race world where one exists. Anything still unnamed is not in the Dart binary that syd runs. Journey 5's early echo resolves to exactly one stored row.
- §2.4 and step 0(c): the share PR contains its own green cases, and it does not wire `remove` to `hard_delete_channel` unless Nick's ruling in that PR says so. The `channel_list` re-lease is a replacement source, never a second live subscription.
- §7.1 and step 3: CI may prove the native-asset build. The published island image does not contain the Dart gateway until the licence act. `GATEWAY_IMPL` defaulting to python is not that act.
- §2.8: sign-in always implements the three-algorithm ceiling. The `alg` census orders the work and informs registration option (b) for `fmt`. It does not shrink the sign-in accept-set.
- C9: the dual-implementation agreement lasts until the later decision that deletes the Python gateway. CI fails unless both binaries meet the journey. The head stamped into the Dart executable is read from that image's alembic, not from a hand constant.
- §2.1's configure proof: `docker compose config` shows the key is forwarded under invariant 7b. The proof that the switch happened is the new process's identity after the restart. A serve-check against the still-running process does not count.


# Round 4 (bonus, Nick's choice): the post-r3 folds only

**Overall verdict (round 4): RECAST (4 of 4).** No DISSOLVE. The frame was not reopened by any seat. **Every finding is about a post-r3 fold, and three of them are defects the folds INTRODUCED.** By the stop rule set for this round, the design stays **PROVISIONAL**: these are recorded and dispositioned here, not folded and re-struck.
**Struck:** dt-1792896455 · families seated: Maxwell + Noether, Carnot, Tesla (Wu disabled) · bundle: round-4 brief, RULINGS, RESEARCH, TEMPER extract (status + r3 synthesis), DESIGN with the r3 folds (89 KB); SPARK excluded · PRODUCER FIRST lens · arrival order: Maxwell, Carnot, Noether, Tesla

## Round 4 synthesis

| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | "stays out of the binary" contradicts one-process replacement; image exclusion × no fallback = an outage the operator causes themselves; folds applied to 1 of N sites |
| Carnot (GPT) | RECAST | the topology lifecycle is undefined; the ten-journey residue escapes the gate; journey 11 contradicts linearisation |
| Noether (DeepSeek) | RECAST | committed-without-effect; zombie `removed_upstream`; synchronous `package:sqlite3` on the event loop |
| Tesla (Grok) | RECAST | "a gateway that proves itself alive while its topology is a fiction": the write window, the unread mark, the SHA→protocol map, the actuator vs artifact |

**Fold-INTRODUCED defects (the round-3 lesson recurred):**

1. **`removed_upstream` is a lifecycle with no readers, no exit, and no answer for omission.** Raised by all four. The no-delete fold removed the destruction but left a mark nobody reads. A marked channel is still joinable and writable, with relay dead. A later `add` has three possible outcomes. A full-replacement snapshot cannot say "gone".
   - **Disposition, fold (subtractive):** *bus topology has authority over SUBSCRIPTIONS only, never over channel rows or their visibility.* A `remove` or an omission unsubscribes, and nothing else changes. There is no mark. `add` re-subscribes, idempotently on the existing row (Maxwell, Carnot; Noether's option 2).
   - **Tesla's alternative is REJECTED for the round-3 reason.** Tesla had omission exclude the channel from every read predicate. A ChatServer restart with a partial list (runtime channels aren't in its snapshot, D1) would then hide live channels from users: a fold adding an outage producer. The gateway's SQLite is the sole persister (memory `reference_hyperspace_is_service_graph_not_datastore`).
2. **The image exclusion, the failure contract and configure combine into a dead cell.** Raised by Maxwell and Tesla; Noether raised it as "the CI-tested digest ≠ the published digest"; Carnot dissented ("does not itself break rollback"). `island configure --gateway-impl dart` against a Dart-less published image commits, restarts and crash-loops, and the failure contract forbids both fallback and revert. The same happens on a runbook image rollback to a pre-Dart tag while `.env` says `dart`.
   - Carnot is right that *flip-back rollback* survives: configure needs no running gateway. That makes the outage recoverable, not absent.
   - **Disposition, fold (producer first):** the image declares the implementations it carries (an OCI label set at build); configure refuses a value missing from the label of the image pinned in `image.env`; the entrypoint's exec failure names the cause. A CI boot of the published digest at `python`. State that the Dart-inclusive digest is unpublished until the licence act, which becomes an **explicit precondition of step 8 and an input to gate G**. Rewrite §2.1 and §4, which still say the published image holds both.
3. **The fold reached 1 of N sites.** Raised by Maxwell, Carnot and Tesla. "Ten journeys" survives in step 2 (**the gate G estimate, exactly where r3 #3 said it went blind**), step 7, gate G's stop clause, C1 and C2. Step 4 still says "both dialects". §7.7 still lists drift vs sqlite3 as open.
   - **Disposition, fold:** sweep, with a grep as the assertion.

**Other findings (narrow, not introduced):**

4. **Committed-without-bus-effect during a stall.** Raised by Noether, Tesla and Carnot. "Bus-dependent actions refused until the first snapshot" plus commit-then-dispatch → a send commits, fans out locally, returns 2xx, and never relays.
   - **Disposition: named tradeoff, and the proposed fold is REJECTED.** Noether's and Tesla's fix is to refuse before commit. During a stall that RESEARCH line 48 says can last *forever* at the prod pin, **that makes sends fail island-wide: round 3's total-outage path, moved onto the most-used route.**
   - Python in the same stall commits, fans out locally and drops the publish (memory `reference_aiko_upstream_review_2026_10_25`: "silent publish-drop acked"). So the behaviour matches Python.
   - **Producer-first, subtractive:** delete the "refused" clause for publish entirely. Publish is best-effort exactly as in journey 10. The partial-view harm it guarded has no producer left once flaw 1's fold lands, because topology can no longer change rows.
   - Owner: Nick. Cost: cross-client relay is silently lost during a bus stall, as today. Mitigation: the §2.9 positive-heartbeat trigger.
5. **Zero or two discovery matches: is it re-evaluated?** Raised by Maxwell. Zero is the ordinary state when the gateway boots before the ChatServer. Two is the ordinary transient during a ChatServer restart inside its lease. Latched, it becomes a process-lifetime bus outage, and a rollback to Python "fixes" a problem Python never had.
   - **Fold:** withhold while the count ≠ 1; grant on exactly one; never latch.
6. **The protocol comes from a SHA→protocol map, not from the installed package.** Raised by Tesla and Carnot. The ARG is `3e4e822`, a SHA and not `:0`. So the fold's "equivalent to Python's import" claim is false.
   - **Fold (deletes the map):** read the version constant at process start from the co-installed `aiko_chat` that this image's ChatServer runs. Bind the `:1` canary to the unpublished `bus-e2e` artifact.
7. **The expected head is baked in a different job.** Raised by Tesla (RESEARCH §A5: binaries and image are separate jobs).
   - **Fold:** read the head at process start from the alembic tree the entrypoint just ran; fail the build on two heads.
8. **`package:sqlite3` is synchronous FFI on the event loop.** Raised by Noether. Journey 7's 16-table cascade would stop the world.
   - **Fold:** the database lives in one dedicated isolate. It is the natural home of §2.3's one-transaction rule, so this also serialises writers.
   - Tesla adds: one write helper owns the `IslandDateTime`/BOOLEAN/JSON spellings, and raw execute is unavailable to command code.
9. **Topology source handoff.** Raised by Carnot and Noether. Queued work from a retired source; the order of unsubscribe and subscribe.
   - **Mostly dissolved by flaw 1's fold:** a stale callback can only add or drop a subscription, which the next snapshot heals. Remaining fold: retire the old source's owner (subscriptions plus queue) before the new one applies its snapshot.
10. **"Any family without a journey stays out of the Dart binary" cannot coexist with one-process replacement.** Raised by Maxwell and Tesla. A missing family 404s on syd: R1 entered through a side door. It also conflicts with §2.6 route parity. Families still without a journey, counted from the route decorators: communities, `/keys`, `/blocks`, recovery enrol/policy/status, `passkey/add`, `/island(s)`, `/gateways`, `/capabilities`, `/register`, `/login`, `.well-known`.
    - **Fold:** delete the rule. Every route family in the frozen `/openapi.json` has a journey before gate G's estimate, and CI maps path prefixes to journey ids, failing on orphans.
11. **Journey obligations.** Journey 11's "no later frame" contradicts the linearisation boundary (Carnot). It becomes "no frame computed after the ban's commit". Journey 5's early echo needs a named serializer (Tesla). The DB isolate from flaw 8, plus a `UNIQUE(channel_id, client_msg_id)` key (#42), is that serializer.
12. **The dual-write tax.** Raised by Maxwell. C9 makes every feature from go onward be written twice, with no end.
    - **Named tradeoff:** Nick owns it; the cost is written in §4. The end of the melb soak forces a dated keep-or-delete-Python decision.

**What holds (all four):**
- the frame;
- HTTP never waits on the bus (confirmed equal to Python's stall behaviour, RESEARCH line 48);
- no fallback plus identity-proved switch;
- the no-delete direction;
- the WebAuthn sign-in ceiling and option (a);
- C9's correction itself;
- MQTT clean-session/QoS 0;
- the licence exclusion's motive.

**The severity read.** No round-4 finding is a total outage; round 3's was. The worst is an operator-caused crash-loop that one configure command recovers. **Two of the panel's own proposed folds would have ADDED an outage path** (refuse-before-commit; omission hides channels). That is the strongest evidence that another fold-and-strike round is now more likely to inject a defect than to remove one. The folds above are almost all deletions (the mark, the refusal, the SHA map, the "stays out" rule). The only additions are the image label, the DB isolate and the journey-orphan CI check.

**Disposition.** RECAST, recorded PROVISIONAL per this round's stop rule. Nick's call:
- **(a)** apply these folds to DESIGN.md as `[r4-fold]` (mostly subtractive), and go to Blade with gate G as the next real check; or
- **(b)** stop here, with the design PROVISIONAL and these findings as the open list.

## MaxwellMergeSlam's Design Strike (round 4)

**Verdict:** RECAST

**Summary:** Most of the folds hold. Two of them say something no other part of the plan can do: "a family without a journey stays out of the binary" can't coexist with a gateway that replaces the whole process, and "published images exclude Dart", combined with "no fallback" and "configure commits first", creates a new self-inflicted outage cell. A third fold was applied to only 1 of the places that needed it, and the missed places include gate G's own estimate. *Hans Gruber: "When you steal $600, you can just disappear. When you steal 600 million, they will find you."* Small folds can slip through. A fold that touches the estimate gets found.

**Producer first:** The folds add three checks. For each, the outcome it guards and who produces that outcome:
- **(1) No bus capability on zero or two discovery matches.** The outcome is a double-dialect publish. Its producer was our two-dialect build, and the fold deleted it. Good, but the check then over-reaches (flaw 3).
- **(2) Reconcile disarmed until the first snapshot.** The outcome is a reconcile acting on a partial view. Its producer is the remove→delete cascade, and the no-delete fold removes it, which makes most of this check unnecessary (flaw 4).
- **(3) Configure verifies by the new process's identity.** The outcome is a switch that silently didn't happen. Its producer is the old process passing a serve-check, and the fold correctly makes it unable to. But the fold creates a new outcome, "the selected implementation is absent from the image", and its producer is ours: configure accepts a value the pinned image cannot honour. **Make that impossible:** the image declares the implementations it carries (an OCI label written at build time), and configure refuses a value not in the label of the image pinned in `image.env`. The entrypoint then cannot be asked to exec a binary that isn't there. No runtime check and no fallback are needed (flaw 2).

**Fatal flaws:**
- **1. Flaw class: wrong option-frame. "Any family without a journey stays out of the Dart binary syd runs" (§2.2, r3-fold) cannot coexist with "Dart replaces one process" (§2.1).** One process serves one island. A family left out of the Dart binary is a family that 404s on syd while `GATEWAY_IMPL=dart`, which is the per-route strangler R1 rejected, entered through a side door. The fifteen journeys still leave these route families un-journeyed (counted from `src/aiko_gateway` route decorators):
  - communities (create, discover, join);
  - `/keys` (signing-key publish, an authorship trust boundary);
  - `/blocks` (named in §2.3's re-check list, never tested);
  - recovery enrol/policy/status (journey 15 covers only re-key);
  - `passkey/add`;
  - `/island` and `/islands` (the mark and manifest);
  - `/gateways` and `/capabilities`;
  - `/register` and `/login`;
  - the `.well-known` files.

  So the rule as written either blocks step 8 forever or ships a broken syd. Fold: the rule becomes **"every route family in the frozen `/openapi.json` has a journey before gate G's estimate"**, mechanised by a CI check that maps each openapi path prefix to a journey id and fails on any orphan. The count is then derived, not asserted as "fifteen".
- **2. Flaw class: missing failure mode, introduced by combining two folds. Image exclusion (step 3) × no fallback × configure-commits-first (§2.1) = a gateway that crash-loops on its own operator's command.**
  - On any published image before the licence act, `island configure --gateway-impl dart` commits, restarts and execs a missing binary.
  - Worse, after the licence: `.env` says `dart`, and an operator follows `backup-and-rollback.md` to an older pinned image that predates Dart. The implementation axis and the image axis are independent, and their cross product has a dead cell nobody priced.
  - Rollback by flipping the setting stays reachable (configure doesn't need a running gateway), so the island isn't down permanently. It is still an outage that one config command produces.
  - Fold: the image label plus a configure refusal (see Producer first). Also, the entrypoint's exec failure names the cause (`GATEWAY_IMPL=dart but this image carries: python`). Also, make **Nick's licence act an explicit precondition of step 8**: as written, syd cannot run Dart from a published image before it, and gate G doesn't list it.
- **3. Flaw class: missing failure mode. "On zero or two matches, the bus capability is never granted" (§2.4, r3-fold) doesn't say whether it is re-evaluated.**
  - Zero matches is the ordinary state when the gateway boots before the ChatServer registers.
  - Two matches is the ordinary transient when the ChatServer restarts inside the registrar's lease and the old entry lingers.
  - Read as permanent for the life of the process, it turns compose start order into a bus outage for the whole process lifetime. HTTP survives, but publish and relay are dead until someone restarts the gateway, and the §2.9 heartbeat trigger would "roll back" to Python, which never had the problem.
  - Fold: the capability is **withheld while** the match count is not exactly one, and granted when it becomes one. Re-evaluated on every registrar update. Never latched.
- **4. Flaw class: half-built state machine / check whose producer is gone. `removed_upstream` (§2.4, r3-fold) has no named reader and no exit.**
  - What does a marked channel do? Does the app still list it? Are sends accepted, and stored but never relayed?
  - What happens when a later snapshot or `add` names it again? Is it re-subscribed and unmarked, or does it get a duplicate row?
  - Is a reconnect snapshot that *omits* a channel a `remove` (so a partial view now unsubscribes live channels)?
  - The fold deleted the destruction but replaced it with an undefined lifecycle. Producer first again: the gateway's SQLite is the sole persister (HyperSpace is a service graph, not a datastore), so **bus topology should have authority over SUBSCRIPTIONS only, never over channel ROWS' lifecycle.** Fold: a bus `remove` (or an omission) unsubscribes, and nothing more. No mark, no row change. A later `add` re-subscribes, and is idempotent on the existing row.
  - That makes "reconcile disarmed until first snapshot" mostly unnecessary. A partial view can then only cost a temporarily missing subscription, which the next snapshot heals, never a state change. Keep the disarm only if a measured case still needs it.
- **5. Flaw class: the fold reached 1 of N sites (incomplete fold).** The journeys fold left "ten journeys" in:
  - **step 2**, the gate G estimate, which is exactly where temper r3 #3 said the estimate went blind;
  - step 7 (acceptance);
  - gate G's "stop" outcome;
  - C1 and C2.

  The one-protocol fold left step 4's "both dialects, exactly-one assertion". The sqlite3 fold left §7.7 still listing drift-versus-sqlite3 as open. Each is a one-line fix, but step 2 is load-bearing. Fold: replace each one with a reference to the derived journey set (flaw 1), and grep for the residue.
- **6. Flaw class: under-counted cost. C9-corrected (both binaries must pass every journey with no end bound) prices the gate-G-to-deletion period at nothing.** Every feature from go onward is written twice for CI to pass, with no end condition. That includes v0.10.0 wire work, #21's per-device sealed-ring field and the #3096 agent identity. The fold is honest about its scope but names no owner and no forcing function. Fold: a named tradeoff, with Nick as owner and the dual-write tax as the cost. The **end of the melb soak forces a dated keep-or-delete-Python decision**, so "a later, separate decision" has a date.

**What holds:**
- **HTTP never waits on the bus.** RESEARCH line 48 confirms that the stale-retained-primary stall hits Python and Dart alike, so serving HTTP during a stall matches Python, and the round-3 total-outage path is gone. Sends during a stall still return 2xx and relay nothing, exactly as Python does today. That is out of scope here.
- **The failure contract** (no fallback, commit before verify, identity-proved switch) is the right shape apart from the dead cell in flaw 2.
- **One protocol per build from the pinned ARG.** One Dockerfile feeds both implementations, so the ARG and the installed package cannot diverge in a published image. Only the re-evaluation is missing (flaw 3).
- **The no-delete direction** is correct (flaw 4 only shrinks it further).
- **`package:sqlite3`**, and the expected head derived from the image's own alembic. Same image, same head; no new dependency.
- **The WebAuthn sign-in ceiling** and the reframed option (a).
- **Journeys 11 to 15's obligations** are well-formed individually. The defect is coverage, not content.

**If RECAST, what to fold back:**
- §2.2: replace "Fifteen journeys … any family without a journey stays out" with "every route family in the frozen `/openapi.json` has a journey before the gate G estimate; CI maps path prefixes to journey ids and fails on orphans". Add the missing families from flaw 1.
- §2.1 and step 3: an image label lists the carried implementations; `island configure --gateway-impl` refuses values absent from the pinned image's label; the entrypoint's exec error names the cause. Gate G and step 8 list Nick's licence act as a precondition.
- §2.4: the bus capability is withheld while matches ≠ 1 and re-granted on exactly one, never latched.
- §2.4 and §7.4: bus `remove` or omission → unsubscribe only; no `removed_upstream` mark; no row change; `add` is idempotent. Re-justify or delete "reconcile disarmed until first snapshot".
- Sweep the residue: steps 2, 4 and 7, gate G's stop clause, C1, C2 and §7.7.
- §4: the dual-write tax as a named tradeoff (Nick), with a dated keep-or-delete decision at the end of the melb soak.


## CarnotCodeCarver's Design Strike (round 4)

**Verdict:** RECAST

**Summary:** The frame holds, but the folds leave topology lifecycle undefined and fail to carry all fifteen journeys into the release gate. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. These gaps create avoidable entropy.

**Producer first:** Harm: an upstream disappearance disables durable local channels, or stale topology revives a relay that should be detached. Trigger: removal, reconnection, or source replacement. Producer: our topology handler coupling transient discovery to durable channel state. Remove that coupling: keep relay attachment state separate from local channel existence and permissions. Harm: silently discovering no server. Trigger: build metadata differs from the installed package. Producer: independently deriving Dart's protocol from an ARG while Python derives it from installed code. Produce both from the installed artifact instead. Network discovery and untrusted WebAuthn parsing remain necessary boundaries; their producers cannot simply be deleted. Dijkstra: "Simplicity is prerequisite for reliability."

**Fatal flaws:**
- Make transport disappearance unable to mutate durable channel availability: separate relay attachments from local channels; then removed_upstream as a permission sentinel becomes unnecessary. The fold introduces that mark without specifying its storage, readers, clearing rule, or interaction with joins, sends and re-adds. Unsubscribe alone leaves unanswered whether local sends succeed while upstream delivery is impossible. Define the pre-snapshot and disconnected behavior of each affected action, including whether refusal happens before persistence. Otherwise HTTP can answer successfully but wrongly while the bus is unavailable.
- One topology source at a time specifies subscription count, not ownership of queued work. After replacement, callbacks or queued reconcile commands from the old source can still run against the new source's state; reconnect can likewise retain the previous snapshot's capability. Give one attachment owner responsibility for its subscriptions and queued work, retire that owner completely before replacement, and reset snapshot authority on retirement. Specify full-snapshot reconciliation and re-add behavior so omitted channels cannot retain ghost relays.
- The protocol fold does not establish equivalence with Python: an ARG identifies intended source, while Python imports the installed package. An override, changed installation step, or canary flag can make those disagree, leaving Dart permanently refusing bus operations. Generate the discovery descriptor from the installed package used by that image, and consume that same descriptor in Dart. This deletes the independent protocol mapping rather than adding another mismatch guard.
- The fifteen-journey fold never reaches the executable build contract. Steps 2 and 7 still require ten journeys, gate G estimates from those steps, and C1/C2 still describe ten. Thus moderation, calls, social sign-in, OAuth and recovery can escape the estimate or completion gate despite C9's broader promise. Journey 11 also demands 'no later frame', contradicting the explicitly accepted delivery of effects computed before revocation. Carry all fifteen through estimation, both-binary CI and switch eligibility, and make journey 11 use the stated linearisation boundary.

**What holds:**
- Binding HTTP independently of discovery removes the known total-outage producer. Disarming topology reconciliation is the correct placement of the remaining dependency.
- Removing unattended hard-delete authority is a substantial improvement; deliberate deletion can remain with the owning service.
- Excluding Dart from published images before licensing does not itself break rollback: the Dart switch becomes available only in a later image containing both gateways. CI can exercise an unpublished candidate image.
- No automatic fallback, configuration committed before verification, and verification of the replacement process are coherent. Implementation identity must actually belong to the replacement instance.
- C9 correctly retains both gateways as maintained implementations. Direct package:sqlite3 removes unnecessary migration machinery while preserving alembic authority.
- The full authentication algorithm ceiling and reproduction of the existing registration accept-set preserve the intended parity boundary.

**If RECAST, what to fold back:**
- Specify local-channel behavior independently of relay availability, including pre-snapshot reads, writes, persistence and delivery acknowledgements.
- Define topology attachment ownership, retirement, reconnect invalidation and snapshot reconciliation as one lifecycle.
- Derive discovery metadata from the installed artifact rather than independently interpreting its build ARG.
- Replace every operative ten-journey requirement with fifteen and align moderation's race expectation with the accepted linearisation rule.
- State that pre-licence images support Python only; selectable Dart releases contain both implementations and pass all fifteen journeys against each.


## NoetherNullPointer's Design Strike (round 4)

**Verdict:** RECAST

**Summary:** The post-r3 folds close the total-outage path, but they open new broken invariants: a committed row can be left without its bus effect, a removed channel can be served half-alive, and synchronous SQLite can freeze the event loop.

**Producer first:**
- The refusal “bus-dependent actions are refused until the first snapshot” guards against acting on a partial topology. The producer of the new harm is our command handler that commits the DB transaction before it reaches that refusal. It can be made unable: check the bus-capability precondition at the command boundary, before the transaction opens, and refuse the whole command.
- The `removed_upstream` mark guards against hard-deleting history. The producer of the new harm is our bus `remove` handler that writes the mark but leaves every read and write path unchanged. It can be made unable: make the mark authoritative (filter reads, reject writes) or drop the mark and route deletion through the owning service.
- The “one topology source at a time” rule guards against double subscription. The producer of the new harm is our source-switch code with no atomic handoff. It can be made unable: freeze the source per build, or apply the new source’s full snapshot before cancelling the old subscription.
- The “published images exclude Dart until licence” gate guards against unlicensed distribution. The producer of the new harm is our release process that strips Dart after CI. It can be made unable: do not publish a release that claims both binaries until the licence is decided, or publish the exact CI-tested image.
- No check added for SQLite. The producer of the event-loop freeze is our direct synchronous `package:sqlite3` call on the main isolate. It can be made unable: run the database in a dedicated isolate.

**Fatal flaws:**
- **Broken invariant: “a successful write means its bus effect is at least attempted.”** The fold “HTTP never waits on the bus” disarms topology reconcile and refuses bus-dependent actions until the first snapshot (§2.1), but the one-transaction rule (§2.3) commits rows before effects are dispatched. A route that writes locally and then publishes can commit, get its publish refused, and still return success. The user sees a stored action that never reaches the ChatServer. This is the same shape as journey 10’s kill-after-commit, but caused by our own refusal layer rather than a crash.
- **Missing failure mode: `removed_upstream` has no read semantics.** §2.4 says a bus `remove` unsubscribes and marks the channel, never deletes. But nothing in the design says what reads the mark. If no route filters on it, the channel remains visible, joinable, and writable while the gateway is no longer subscribed to it. That is a half-alive channel: upstream is gone, local API still serves it. The fold prevents destruction but creates a zombie state.
- **Coupling: synchronous SQLite on the event loop.** §2.5 selects `package:sqlite3` directly. It is a synchronous FFI API. Called from the main isolate, every DB operation blocks HTTP, WS, and bus handling. A long transaction—e.g. the 16-table account deletion in journey 7—becomes a stop-the-world pause. Python’s sync DB work runs in a threadpool; Dart’s does not unless isolated. The design does not state an isolate or async executor.
- **Ordering gap: topology source handoff.** §2.4 says a re-lease is a “replacement, never a second live subscription,” but it does not define the handoff. Unsubscribe-then-subscribe loses events between the last old message and the new snapshot. Subscribe-then-unsubscribe briefly has two live sources, violating exactly-one. A channel add or remove during the switch can be lost or duplicated.
- **Supply-chain divergence: CI-tested image ≠ published image.** §3 step 3 and §9 exclude Dart from published images until the licence, but C9 requires both binaries to pass every journey in CI. If the attested release image omits Dart, the both-binaries rule does not bind the artifact that is actually deployed. The CI test and the release artifact are not the same object.

**What holds:**
- The one-image / `GATEWAY_IMPL` / same-volume rollback frame survives.
- Alembic as sole schema authority and Dart as a client holds.
- The one-protocol-from-ARG discovery fold holds; zero/two matches refusing the bus capability is fail-safe.
- The listener no longer waiting on the bus holds for HTTP reads.
- The bus `remove` no longer hard-deletes history: the destructive producer is removed.
- The fifteen journeys materially broaden coverage; C9 is correctly unbounded from gate G.
- `package:sqlite3` is directionally right for avoiding migration machinery, but only with isolation.

**If RECAST, what to fold back:**
- Move the bus-capability precondition to the command boundary. Before opening the transaction, refuse any command whose effect set includes publish/relay. Then no committed-but-unpublished row exists.
- Define `removed_upstream` semantics: every read filters it, every write rejects it, and deletion goes through the owning service. Or remove the mark entirely and make attended deletion the only path.
- Run SQLite in a dedicated isolate. Never call synchronous `package:sqlite3` from the event-loop isolate.
- Specify the topology source handoff: either make the source immutable per build, or apply the new source’s full snapshot and reconcile before cancelling the old subscription.
- Align release and CI: either publish the Dart-inclusive image once the licence is decided, or state explicitly that the both-binaries CI gate does not apply to published images until then.

## Tesla, the Arc-Prophet's Design Strike (round 4)

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Verdict:** RECAST

**Summary:** The stall that used to hold the door shut now finds the door open, writes rows, and hums all night — three folds (HTTP unbound from the bus, `removed_upstream` with no reader, protocol baked from an ARG) share one frequency: a gateway that proves itself alive while its topology is a fiction.

**Producer first:** The post-r3 folds add real checks. The producers split as follows.

- **Partial-view destruction** (reconcile hard-deletes, or writes, from a snapshot that never finished). Trigger: the prod pin's stale retained `(primary found …)`, which can stall discovery forever. Producer: ours — the reconcile path, and any command that commits from that same view. Disarming reconcile removes the delete producer. It leaves the write producer standing. Make a committed bus-effect impossible before the first snapshot: those commands fail before commit. The post-commit "refuse publish" check then becomes unnecessary.
- **History burned by a bus `remove`.** Producer: ours — `remove` → `hard_delete_channel`. The fold deletes that producer. Keep it deleted. The new mark is a second producer (a channel that still accepts joins and sends after unsubscribe) unless every read and write folds the mark in. A replacement snapshot that treats omission as a no-op is the same producer by another input.
- **Speaking the wrong dialect, or two.** Producer: ours — the filter we choose. Reading `_VERSION` from the co-installed `aiko_chat` at process start, the file the ChatServer in this image already runs, removes the ARG-to-protocol translator. Zero matches then means the server is absent (external). That refusal stays. The build-time map does not.
- **A switch that certifies the process which is already gone.** Producer: ours — a serve-check Python can pass. Identity proof stays; the old process cannot emit the new identity. The crash-loop is a different producer: `configure` writing `GATEWAY_IMPL=dart` when the published image has no such executable, under an entrypoint that never falls back and a verification that never reverts. Make that value unwritable unless the image contains the exe. The non-revert rule can remain for a process that actually started.
- **Schema the SQL does not match.** Producer cannot be removed: the file is alembic's, opened from outside. The equality refusal stays. The false refusal from a head baked in a different job than the entrypoint's alembic is ours, and it goes away if the process reads the head from the alembic tree this container just migrated with.
- **An unjourneyed family going dark at the switch.** Producer: ours — shipping a partial process in a design that replaced the whole gateway. §2.6 route parity already forbids a missing route. Delete "stays out of the Dart binary." Keep the journeys, and point C1, C2, and step 7 at the whole set.

**Fatal flaws:**

- **§2.1 "HTTP never waits" plus C5 — the write window is the old outage, transposed.** Dart binds after the lock and the DB open, and sign-in, history, and DMs stay up. That part holds. Publish and relay are refused until one full snapshot, and §2.2 dispatches those effects only after commit. Journey 10 already fixes the meaning of a refused post-commit publish: the row is kept, the publish is dropped, there is no retry. During the stall that RESEARCH §A1 Q1c names — discovery waiting forever on a stale retained primary — every send and join therefore commits against a projection the process has admitted it does not have, returns success, and never replays. Local fan-out makes the lie visible to sockets on this process; the ChatServer relay stays empty. `/health` may mention the bus and still listen. The switch contract proves identity, so this state counts as a successful cut. The heartbeat in §2.9 is a manual log trigger whose N and M are unset until gate G, and a rollback does not unwrite the rows. A periodic re-lease (§2.4) that drops "snapshot held" before the replacement snapshot lands repeats this window on a timer. C5 states the falsifier and then leaves the commands that trip it on the serving path.
- **§2.4 `removed_upstream` and journey 9 — the mark is unread, and a replacement snapshot cannot retire a channel.** Nothing in the design reads the mark. After a bus `remove`, the gateway unsubscribes and the channel row remains joinable, listable, and writable: half-alive, local history growing, relay dead. Journey 9 says a reconnect snapshot that omits a channel does no hard-delete, and it does not say the omission marks, unsubscribes, or drops the channel from the visibility predicate. A re-lease that is a full replacement therefore has no legal way to express "this channel is gone." At the prod pin, incremental removes do not arrive (RESEARCH D1), so the mark stays unset and the channel set only grows. A later `add` of a marked row has no specified transition: clear-and-resubscribe, insert-again, or resubscribe while the mark still says removed. Three outcomes, one sentence.
- **§2.4 "derived from the pinned ARG" — the claimed equivalence to Python's import is false.** Python takes the protocol from `get_server_service_filter()` inside the package it actually imported. The ARG is a git SHA (`3e4e822`), not `:0`. Some table in the Dart build translates SHA to protocol. That table diverges when the SHA bumps `_VERSION` and the table does not, or when the checkout the compiler read is not the checkout the image installed for ChatServer. Divergence is a zero-match: the bus capability is never granted, while HTTP serves and the identity proof reports `dart`. The refusal is the right shape for "two dialects." It is the wrong producer for a pin that drifted inside one image. `:1` behind a canary flag is a second binary that must never be the exe a prod image execs; the fold does not bind the flag to the unpublished artifact.
- **§2.1 failure contract × §3 step 3 image exclusion × §2.2's fifteen journeys — the actuator, the artifact, and the gate disagree.** Configure commits `GATEWAY_IMPL`, restarts, never falls back, and a failed verification does not revert. Published release images exclude the Dart gateway until the licence act, while `island configure` is a separate released binary that step 3 grows with `--gateway-impl`. That option can commit `dart` against a digest whose entrypoint has nothing to exec: crash-loop, alembic on every restart, identity proof unable to pass, the island down until a second configure. §2.1 and §4 still say today's image gains the Dart executable and keeps both gateways. C9 demands both binaries meet every journey; step 3 tests a with-Dart image and publishes a without-Dart image, so the attested digest is not the digest that passed. Inside the journey fold, the table has fifteen and C1, C2, step 2, and step 7 still say ten. "Any family without a journey stays out of the Dart binary" cannot be satisfied together with §2.6 (a route in one binary and not the other fails CI) or with replacing the whole process. Communities, blocks, and the read-only surface have no journey. Step 7 can go green while moderation, calls, social sign-in, OAuth, and recovery — the rows this fold added — are still red. Journey 5's new obligation (an echo before the local commit yields exactly one row) has no serializer: linearisation places the echo mark with the post-commit effects, and the only dedupe is the in-memory content set. Two inserts remain able to write.
- **§2.5 `package:sqlite3` — the converter was the enclosure, and the baked head can lie.** The version refusal stays: the file is external, and a hand-written statement cannot be made safe by deleting the comparison. What the fold removes is the single type boundary. `IslandDateTime` is a name; `package:sqlite3` binds whatever string each call site passes, and `'T'` sorts after `' '` (RESEARCH §A3). Stored-byte CI covers the writes that exist when the oracle is written, and steps 5 and 6 add the writes that matter. BOOLEAN and JSON split the same way, one interpreter per query. Separately, the expected head is "read from the image's own alembic at build time," while RESEARCH §A5 builds `island-binaries` and the image in different jobs. Two exes can bake two heads against one volume, and the equality check then refuses a schema this image's own entrypoint just applied.

**What holds:**

- The frame these folds sit in: one process inside today's image, `GATEWAY_IMPL`, alembic as the only schema authority, no freeze and no rollback cliff. Twelve strikes left it standing. It was not re-opened here.
- Disarming topology reconcile until one full snapshot. That is the delete producer, removed at the source. It is the right answer to the round-3 gate, and it stays.
- A bus `remove` that does not cascade into messages and memberships. Attended deletion through the owning service matches ISL-0002. The hole is the unread mark, not the spared history.
- Never falling back inside the entrypoint, and proving the switch by the new process's identity. A serve-check the old process could pass would re-open temper r3 #6.
- Sign-in implementing the full ES256/RS256/EdDSA ceiling, with the census ordering work only. Option (a) as py_webauthn's actual accept-set, non-checks included, is an honest size for gate G.
- C9's correction itself: both gateways meet the journeys until a later decision deletes the Python one, and that obligation is not bounded by gate G. What fails is the journey set the sentence is pointed at, and the image the binaries are measured in.
- MQTT policy (clean session, random client id, QoS 0) matching the pin, so a restart still does not resurrect a session.
- The licence exclusion's motive. Presence in the attested AGPL image is the selection §7.1 withholds. The fold is right to keep the exe out of the published digest. It is wrong about what else it thought could stay coupled to that digest.

**If RECAST, what to fold back:**

- In §2.1 / C5, split the listen socket from the command. HTTP reads, sign-in, and history bind after the lock and the DB open. Any command whose correctness includes a bus effect (send, join against the live topology, publish, relay) fails before commit until the single source has applied one full snapshot, and a re-lease keeps the previous snapshot's capability until the replacement snapshot has applied. Delete post-commit "refuse."
- In §2.4, one applicator owns the projection. A snapshot is the whole set: present means subscribed and visible; absent means unsubscribed and excluded from every read and write predicate; rows and history stay. Incremental `remove` is that same transition for one channel. `add` clears it and resubscribes. Name this as the only reader of `removed_upstream`. Journey 9's "omit does not hard-delete" becomes "omit takes the channel out of the predicate and leaves the bytes."
- In §2.4, derive the one protocol at process start from the co-installed package's version constant in this image, the same file ChatServer runs. Keep the zero-match and two-match refusal. Delete the build-time SHA-to-protocol map. Bind the `:1` canary flag to the unpublished `bus-e2e` artifact so a release recipe cannot set it.
- In §2.1 and §3 step 3, make `configure --gateway-impl dart` refuse to write the key unless the image it is about to restart contains the exe. The published, Dart-excluded digest therefore rejects the value cleanly. Add a CI boot of that published digest at the default `python`, and state that the with-Dart digest is unpublished until the licence act. Rewrite §2.1 and §4 so they stop saying the published image already holds both executables.
- In §2.2, §3, and §5, retarget C1, C2, step 2, and step 7 from ten journeys to the full set, and list the user-visible families still absent (communities, blocks, the read-only surface) inside that set before gate G. Delete "stays out of the Dart binary"; §2.6 route parity is the constraint. For journey 5, name the serializer: one in-flight slot per send, echo ingest waits on it, so the early echo and the local insert cannot both commit.
- In §2.5, keep `package:sqlite3` and the version refusal. Read the expected head at process start from the alembic tree the entrypoint just ran, and fail the build if that tree has two heads. Close the format at one boundary: services write through one helper that accepts `IslandDateTime` and the BOOLEAN/JSON spellings, and a raw execute is unavailable to command code. Stored-byte CI stays the witness, run on the queries steps 5 and 6 actually add.
