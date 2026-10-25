# TEMPER.md: Dart port of the island gateway, round 1

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
