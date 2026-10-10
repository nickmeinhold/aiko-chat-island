# 18-TEMPER.md: operator opt-in auto-update

**Overall verdict: RECAST, 4/4** (Maxwell, Kelvin, Carnot and Tesla all RECAST; zero DISSOLVE)
**Struck:** dt-1791609536, 2026-10-10. Families seated: Maxwell + Kelvin + Carnot + Tesla (Wu disabled). Full 4-way.
**Bundle:** design 18 + `docs/crucible/reactive-deploy/TEMPER.md` (12 KB).

## Per-family verdicts
| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | "Never passed /health" is not "never served"; the step order contradicts its own quarantine rule |
| Kelvin (Gemini) | RECAST | The health check is the event horizon, so make it an invariant; let the operator set the window |
| Carnot (GPT) | RECAST | Rollback is a state equivalence proof: inventory the whole state, gate admission, rehearse on a copy |
| Tesla (Grok) | RECAST | SQLite WAL is three files, so a one-file restore replays the failed generation; a clock is not a canary |

## Fatal flaws (deduped, most severe first)
1. **Health is not admission (4/4).** uvicorn serves every route the instant it listens, and `/health` is a watcher-side timer, not a gate. The new process can accept REST/WS writes and **ack MQTT QoS 1** during the window; the restore then rewinds them, so the loss is silent. → FOLD: an entrypoint invariant. The new image may migrate and may answer `/health` on a health-only port, but may not bind the client port or consume the bus until the watcher ADMITS it. The guarantee becomes "never admitted ⇒ never served".
2. **A one-file restore under WAL replays the failed generation (Tesla; Carnot: no state inventory).** `aiko.db-wal`/`-shm` survive a stop, and copying back only `aiko.db` leaves the new WAL to be replayed onto it. Other persistent state (mosquitto's volume) is unaccounted for. → FOLD: back up with `sqlite3 .backup` (or a checkpoint, then copy), restore the db/wal/shm SET atomically and unlink a stray WAL, plus a written STATE MANIFEST: every volume, restored or proven disposable.
3. **The step order contradicts the quarantine rule (Maxwell, Tesla).** The pull happens after the stop, so "transient faults abort before step 2" is false, the pull adds to the downtime, and exits between stop and health have no path. A slow table-rebuild migration also looks identical to a bad image inside the 120 s. → FOLD: Tesla's three phases. A: pull + verify, services up, retry-not-quarantine. B: **rehearsal**, i.e. migrate and boot the candidate against a `.backup` COPY with no client port and no bus. C: cutover only if B passed, and every exit from C restores the set and starts previous.
4. **The stagger is a clock, not a canary (Carnot, Tesla).** A digest quarantined on imagineering is still pulled by enspyr 24 h later, so a bad release still hits both, a day apart. → FOLD: a promoted channel. Imagineering follows `:latest`; others follow a tag advanced ONLY after the canary has run the digest for the soak without quarantine. Promotion candidate: a CI job reads the canary's PUBLIC `/health` `ref` (already published), so no box holds registry credentials. Kelvin's `AUTO_UPDATE_WINDOW` (cron spec) folds in as the operator's time window.
5. **The compat floor is not a total order (Tesla), and CI under-tests it (Carnot, Tesla).** Alembic ids are graph nodes, "max over applied" is undefined, and declaring an old id opens the guard for every old image. → FOLD: the descendant-most floor on the graph, refusal on incomparable floors, and a boot test of "my head is the floor or a descendant of it". A backward floor is allowed only when CI booted THAT declared old binary and wrote to every table the revision rebuilt. **Open question 3 answered: no permissive default for table rebuilds.**
6. **Compose can't express a digest or a local tag (Maxwell).** `image: …:${ISLAND_VERSION:-edge}` can't take `@sha256` or `aiko-island:previous`, and `:-edge` is design 16's empty-pin trap. → FOLD: `${ISLAND_IMAGE:?}`, plus a named one-time compose hand-sync on each box (the #2301 class), stated as the bootstrap step.
7. **No terminal state for a double failure, and the rollback pin is prunable (Maxwell, Tesla).** The previous image also unhealthy means nothing defined, and `aiko-island:previous` dies to `image prune -a`. → FOLD: pin by image id + digest and re-pull the digest if the tag is gone. If the previous image also fails: stay stopped, mark the HOST failed (not the release), suspend auto-update, and page on a path that does not run on the downed island.
8. **Health covers one of three services (Maxwell).** → FOLD: healthy = all three containers healthy AND `/health` = 200.
9. **The guard protects only rollbacks to images that contain it (Maxwell).** → named limitation; the watcher refuses auto-restore to a pre-guard image (version label).

## What holds (unanimous or near)
- The three-part shape (channel, restore-on-failure, compat guard) and `AUTO_UPDATE=off` by default.
- The compat guard's safe default (an undeclared floor is the revision itself, so refuse, not corrupt). It's needed whether or not a watcher ever ships.
- The island pulls and CI holds no SSH; shell + systemd only (ISL-0003 intact); a shared `flock`, same principal.
- Separating "did not start" from "started and failed", once it's drawn at the right step.

## Disposition
**RECAST → fold into design 18 v2, re-strike (round 2 of ≤ 3).**
**The danger to watch, from design 16:** each round's fix created the next round's flaw. Most of this round's folds ADD mechanism (admission, rehearsal, promotion). The round-2 bar, set before folding: **v2 must not grow past ~1.5× v1**, and each new piece must remove a failure mode, not relocate it. Specifically, rehearsal (fold 3) should make the cutover's restore path rarely exercised. If v2 needs more than that, stop and reconsider the frame, not another round.



---

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The three-part shape (channel, restore-on-failure, compat guard) is right, but its load-bearing claim, "a version that never passed /health never served a write", is false in this codebase, and the update sequence contradicts itself on where a transient fault aborts.

**Fatal flaws:**
- **§3 "Why the restore loses nothing" is an unstated assumption, and it's FALSE (illegal move).** `/health` (main.py:416) returns 200 once uvicorn is up and a session can COUNT one table. `aiko_connected=false` still returns 200. But uvicorn serves every REST and WS route from the instant it starts, not from the instant the watcher sees 200. Failure case: a release whose `/health` raises (a bug in `_reachability`, say) while `/v1/.../messages` works. Clients reconnect within seconds and post for 120 s, the watcher declares "never healthy", and the restore **deletes those messages**. "Never passed health" is not "never served". John McClane: "Come out to the coast, we'll get together, have a few laughs." The window is open the whole time.
  **Fix by removing the coupling, not guarding it:** the new container boots **HELD**. Every non-`/health` route returns 503 and WS refuses, until the watcher releases it after health. Then "never admitted ⇒ never served" is true by construction.
- **§3 step order contradicts its own quarantine rule (missing failure mode).** "A transient fault (pull error, GHCR 5xx) aborts before step 2", but the pull is step 4, AFTER stop (2) and backup (3). As written, a GHCR blip happens with the island stopped, and the sequence has no path back except the failure arm, which quarantines a good release. It also adds the whole pull to the downtime.
  **Fix:** pull and verify the digest FIRST (step 0), and only then stop.
- **§3 assumes compose can run a digest and a local tag. It can't today (under-counted blast radius).** All three services use `image: ghcr.io/nickmeinhold/aiko-chat-island:${ISLAND_VERSION:-edge}`. Neither `@sha256:…` nor `aiko-island:previous` fits that template, and `:-edge` is exactly design 16 round 4's empty-pin-falls-to-main trap.
  **Fix:** `image: ${ISLAND_IMAGE:?}`. But that is a compose change, so the FIRST auto-update-capable release needs a hand-sync of `docker-compose.yml` on every box (the #2301 class). The design must name that bootstrap step rather than discover it via `preflight-compose-drift.sh` refusing.
- **§1's compat guard protects only rollbacks TO images that contain it (first-arrival).** Every image published before the guard still serves blindly on an unknown revision. So the guard's protection starts one release after it ships, and "This also protects a manual rollback" is true only for targets newer than the guard.
  **Fix:** state this as a named limitation, and have the watcher refuse to auto-restore to a pre-guard image (it can read the image's version label).
- **§3 health covers one of three services (missing failure mode).** Registrar and chat share the image and have their own compose healthchecks. The watcher's criterion reads only the gateway's `/health`, so a release that kills ChatServer is recorded "current".
  **Fix:** "healthy" means all three containers report healthy AND the gateway answers `/health`.
- **No terminal state for a double failure (missing failure mode).** Restore, then the previous image also unhealthy (disk full, OOM, host fault). The design ends at "verify /health" with no branch. That's also the false-quarantine case: a host fault inside the health window quarantines a good release.
  **Fix:** if the previous image is also unhealthy, stay stopped, mark the HOST (not the release) as failed, stop all auto-updates until the operator clears it, and do NOT quarantine the digest.

**What holds:**
- The channel IS `:latest`; no new CI. `AUTO_UPDATE=off` by default keeps today's behaviour exactly, and `update_nudge` still covers the operator who never opts in.
- The stagger answers the old TEMPER's "dual-prod simultaneous outage" with a setting instead of machinery.
- The compat guard's comparison is elegant and correct. "The floor revision exists in my script directory" is exactly "my code is at least the floor" on a linear alembic history, and the safe default ("an undeclared floor is the revision itself") means a forgetful author causes a refusal, never a corruption.
- Stop-before-backup does make the backup consistent, and it removes the old-container write window, once the HELD admission removes the new-container one.

**If RECAST, what to fold back:**
- Add the HELD/admission mode (a new-image feature, set by env from the watcher) and restate §3's guarantee as "never ADMITTED ⇒ never served".
- Reorder: pull + digest verify → stop → backup → up (held) → health (all three) → admit, or restore.
- Name the compose bootstrap (`${ISLAND_IMAGE:?}`, a one-time hand sync).
- Name the guard's first-arrival limit.
- Add the double-failure terminal state; quarantine only release-attributable failures.

---

## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** The design correctly crystallizes the lessons from the prior meltdown, but it models the system in a vacuum, ignoring ambient heat from coupled services that could cause a phase transition.

**Fatal flaws:**
- **Equivalent Downtime is a Thermodynamic Mirage (Section 3, Open Question 1):** The design equates automated downtime with manual downtime. This is a failure of state. Manual downtime is a coordinated, scheduled event. Automated downtime is an unannounced entropy spike for any coupled service. An island that is not truly isolated will radiate failure. `Deckard: "Replicants are like any other machine. They're either a benefit or a hazard. If they're a benefit, it's not my problem."` — for an opt-in feature, the operator must be made aware of the full hazard potential, not have it buried in an open question.
- **The Health-Check Singularity (Section 3, Open Question 2):** The claim of a lossless restore depends entirely on a single, infinitesimally thin barrier: that no writes can occur before the `/health` check passes. This is treated as a question when it is, in fact, the event horizon. A single implementation bug here and the design collapses back into the very corruption-on-rollback state it was built to prevent. A design that lives this close to a singularity is not safe; it is merely metastable.

**What holds:**
- The **Compat Guard (Section 1)** is a solid-state solution to the schema-drift problem, creating a safe-by-default refusal that protects against manual and automated rollbacks alike. It's a proper cryo-shield.
- The **backup-and-restore cycle (Section 3)** is the correct mechanical sequence for this kind of operation, assuming its boundary conditions hold. It correctly identifies the quiescent state for a clean backup.
- The **staggered channel rollout (Section 2)** correctly bounds the blast radius of a "healthy-but-wrong" release, preventing a single flawed release from causing a flash freeze across all production systems.

**If RECAST, what to fold back:**
- Do not leave the downtime window to chance. Add an `AUTO_UPDATE_WINDOW="<cron_spec>"` setting to the operator's controls. The operator must define the low-entropy window for this automated change; the system cannot be allowed to induce chaos at random.
- Elevate the health-check assumption to a **core safety invariant**. Mandate a new, specific CI job: a "chaos test" that runs in parallel with the booting service and hammers it with write attempts. This test must assert that all writes fail until the moment `/health` becomes green. Do not trust the boundary; verify it with fire.

---

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. This design fixes the old fatal in spirit: rollback now means image plus database restore, and the compat guard changes silent corruption into refusal. But the candidate still leaks entropy through the definition of "never served" and "state". As cast, it can still accept or process writes during the trial window, then restore an older DB and erase real work. Recast around an explicit write-quiescence gate and a complete persistent-state restore contract.

**Fatal flaws:**
- The rollback safety proof depends on "the new version never served", but the actual gate is `/health`, not traffic admission. A process can bind ports and accept writes before `/health` first passes, especially if health is app-level readiness rather than network isolation. If that happens, step 6 restores the old DB and loses accepted writes. That is the same thermodynamic sin as before: pretending an irreversible state transition is reversible because the gauge has not gone green yet.
- The design restores only `aiko.db`. That is not yet a proven complete serving state. If the island has WAL/SHM sidecars, uploaded files, caches that become authoritative, vector stores, broker queues, local media, pending jobs, or any other persistent volume content, image+single-file DB restore is not a rollback of the system. Feynman: "The first principle is that you must not fool yourself." The doc needs a state inventory, not a hope that SQLite is the whole universe.
- Stopping gateway, registrar, and chat does not prove the island is write-quiescent. MQTT publishers, retained/QoS messages, external clients, cron jobs, webhooks, or Docker restart behavior may enqueue or deliver work during the window. The open question on bus traffic is load-bearing, not decorative.
- The trial deployment is run against production state. That couples validation and mutation. The simpler dissolving alternative is to test the new image against a restored copy of production data in an isolated network first, then take the real outage only after it passes. That deletes much of the need for heroic rollback reasoning.
- The compat-floor CI story under-tests the claim. Testing only previous-release code against new-head schema verifies N-1, not an arbitrary declared `compat_floor`. If floors can name older revisions, CI must test the oldest declared compatible code, plus preferably every supported rollback target. Otherwise `compat_floor` becomes a wish with YAML syntax.
- The channel delay bounds synchronized blast radius but does not bound third-party blast radius by evidence. `latest` has no health signal, no release metadata contract, and no fleet-level canary result. A 24-hour age check is a clock, not a thermodynamic reservoir; entropy does not decrease merely because we waited.

**What holds:**
- The old fatal is correctly identified: image rollback without data rollback is false safety for a self-migrating service.
- The safe-by-default `compat_floor` idea is directionally right. Refusing old code on unknown schema is much better than serving with a warning.
- The lock-principal issue from the prior temper is explicitly addressed by requiring manual and timer deploys to share the same effective user.
- Separating transient preflight failures from started-but-unhealthy releases is a real improvement over false quarantine.
- Opt-in default-off auto-update is the right blast-radius posture for operators who do not want this risk.
- The stagger is useful, especially for Nick's two islands, though it is not a substitute for a canary verdict.

**If RECAST, what to fold back:**
- Make traffic admission explicit: during an auto-update, either firewall/remove routes before the new container starts, or start the candidate on an isolated compose project/network with no external ingress. Only switch production traffic after health and smoke checks pass.
- Define `serving state` as a manifest, not `aiko.db`. Include SQLite WAL/SHM handling, all writable volumes, broker persistence, uploads, queues, and any derived stores that cannot be safely rebuilt. Restore the whole manifest or prove each excluded item is disposable.
- Add a write-quiescence protocol: stop ingress first, drain/disable workers, pause MQTT subscriptions or document QoS/retained-message behavior, then take the snapshot. Dijkstra: "Simplicity is prerequisite for reliability." The simple invariant is: no component can accept a write until promotion.
- Prefer a two-phase local canary: snapshot production state, boot candidate isolated against the snapshot, run `/health` plus smoke paths, then perform the short real deployment. This dissolves the riskiest coupling between validation and mutation.
- Tighten compat CI: for every migration declaring `compat_floor`, boot the exact oldest compatible image against the migrated schema and run the smoke suite. If only N-1 is supported, say that and reject older floors.
- Require release metadata for the channel: digest, publication time, migration-present flag, minimum compose version, and optional canary status. Do not infer all of that from a moving `latest` tag.
- Recast verdict target: keep the design, but only after the fold-back names the invariant as `auto-update may not expose a candidate to production writes before promotion, and rollback restores the entire serving state`. Hamming: "The purpose of computing is insight, not numbers." Here the insight is that rollback is not an action; it is a state equivalence proof.

---

## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** The old fatal note is still in the room: a restore that copies one SQLite file, on one failure edge, after the new process has already been allowed to write, will arc to ground as a hybrid database and a quarantine that never warns the second island.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- Design 18 §3 step 3 treats `aiko.db` plus `integrity_check` as the whole instrument. SQLite in WAL mode is three files. A clean-looking stop often leaves `aiko.db-wal` and `aiko.db-shm` on the volume. The new entrypoint migrates the live volume before `/health` exists. Step 6 copies the old main file back over the new one and leaves the new WAL beside it. On the next open, SQLite replays the failed generation's frames onto the restored file, or declares the image malformed. Rollback `/health` then fails with no third state specified. That is the original cascade — forward schema, old binary, souvenir backup — reached through the step that was supposed to be the spine. `integrity_check` on the main file alone sings green while the energy sits in the sibling.
- The numbered run pulls at step 4, after the stop at step 2. The quarantine paragraph says a pull error, a GHCR 5xx, or a full disk aborts before step 2. Both cannot be true. Every exit after the stop except "no `/health` in 120s" is undrawn: backup fails, `integrity_check` fails, pull fails, `up -d` fails, the previous tag is missing. The island is already silent. A transient fault in that window is an outage until a human appears, and a full-disk copy of `aiko.db` is exactly the fault a small box will hit at 3am. The 120s budget also quarantines a good release whose table-rebuild migration is still running, because a slow migration and a bad image are the same signal.
- "Never healthy" is not "never served." `up -d` binds the port as soon as the process listens. `/health` is a watcher-side timer, not a gate. Open question 2 names this and leaves it open. The load-bearing sentence — between steps 2 and 6 no process accepted a write — is false of the new process itself. It writes the migration immediately, and it can accept gateway, registrar, and chat writes, and ack MQTT, for the whole 120s while still looking unhealthy. Manual deploy today does not rewind the database after that window. This restore does. A QoS 1 ack followed by a file rewind is silent loss: the publisher will not send again, and the row is gone. A release that would have passed `/health` at second 121 loses those writes and gets quarantined.
- The stagger does not conduct. `AUTO_UPDATE_DELAY_HOURS` is a clock. Imagineering at 0 can quarantine a digest at T+5min, and enspyr at 24 still pulls it, because quarantine is local and `:latest` is unchanged. Third-party islands inherit the same clock with no wire back to the canary's death. The TEMPER's blast radius was occurrence on every bad release. A phase shift still rings both bells, one day apart, and a healthy-but-wrong release is promoted by elapsed time. A canary nobody's success is allowed to cancel is just a delayed second outage.
- §1's boot check — floor id present as a file in the script directory — is the right refuse for an unknown future revision, and the wrong comparison for the floor you actually store. Alembic ids are hashes on a graph. "Max over applied revisions" is a total order the graph does not have. Two incomparable floors, or a string-max of hashes, records a floor that is not the descendant-most. A later declaration of an ancient id is present in every old image, so the guard opens. The CI proof boots only the previous release and runs its smoke suite. The TEMPER already showed a shallow green on N+1 with the wrong binary. Smoke will certify a permissive floor. Open question 3, inviting a default of "those rebuilds would very likely survive," is the same note tuning the safe default back to serve-anyway.
- `aiko-island:previous` does not survive `docker image prune -a` or a human `system prune` once `up -d` has dropped the old container. The design claims prune cannot reap it. During the health wait the tag is the only pin. If it is gone, step 6 starts nothing.

**What holds:**
- Default `AUTO_UPDATE=off` leaves today's island exactly as it is. The danger is opt-in, and the opt-in is what Nick will turn on.
- An undeclared `compat_floor` equal to the revision itself flips the current unknown-revision path from "serve anyway" to `MIGRATE_REFUSE_TOO_OLD`. That part is needed whether or not a watcher exists, and it makes a manual rollback refuse instead of corrupt when no backup is restored.
- The island pulls. CI is not given SSH into the box. The timer is shell, on the same principal as a manual `update.sh`, with `flock -n` shared by both, and a busy lock exits rather than queues.
- Classifying "did not start" separately from "started and failed" is the right cut. It is simply drawn at the wrong step, and "failed `/health`" still mixes a bad image with a sick host.
- Naming healthy-but-wrong as invisible to auto-rollback is honest. Compose drift aborted by `preflight-compose-drift.sh` before the stop is the right shape for a preflight.
- Pinning the running digest before mutation, and refusing to retry a quarantined digest until a new release supersedes it, are the right local rules once the restore path is real.

**If RECAST, what to fold back:**
- Rewrite the spine in §3 as three phases. Phase A, services still up: resolve the index digest, pull that digest, `flock`, preflight compose. A network or disk fault here retries next poll and must not quarantine. Phase B, rehearsal: `sqlite3 .backup` of the live database onto a side directory, replay migrations there, boot the new image against only that side database, and require `/health` with the service port and the MQTT consumers unbound. Phase C, cutover, only if rehearsal passed: stop gateway, registrar, and chat; checkpoint or `.backup` the live database; move `aiko.db`, `aiko.db-wal`, and `aiko.db-shm` as one set; start the new image; wait for `/health`. Every exit from phase C — pull is no longer in this phase, backup failure, migrate failure, health timeout, missing previous tag — restores that three-file set, unlinks a WAL left by the failed start, starts `aiko-island:previous`, and if that health also fails, stays stopped and pages. No third attempt.
- Close open question 2 in the design as an entrypoint invariant: the process may migrate and may listen on the health port, and it may not bind the client port or ack bus traffic until it is ready. State that a restore after an ack is data loss, so the cutover image is forbidden from consuming until `/health` is true.
- Replace the soak clock with a channel the canary writes. Imagineering auto-follows `:latest`. Enspyr and third parties auto-follow a tag that advances to a digest only after the canary has run it for the soak and has not quarantined it. A local quarantine must be able to withhold that promotion. Elapsed time alone must not move the second island.
- In §1, define the stored floor as the descendant-most declared floor on the alembic graph, and refuse to boot if two applied floors are incomparable. The boot test is "my head is the floor, or my head is a descendant of the floor," walked on the graph. Keep the default floor equal to the revision itself. Answer question 3 in the doc: table rebuilds do not earn a permissive default. A floor may point backward only when CI has booted the declared old binary and run a write against every table that revision rebuilt, not a liveness smoke.
- Pin `aiko-island:previous` by image id and by digest, and state that rollback re-pulls the digest if the local tag was pruned. Say who is paged when both generations fail health, on a path that does not run on the island that is down.


---
---

# Round 2: design 18 v2

**Overall verdict: RECAST, 3/3 seated** (Maxwell, Carnot and Tesla all RECAST; zero DISSOLVE)
**Struck:** dt-1791616026, 2026-10-10. Families seated: Maxwell + Carnot + Tesla. **Kelvin DARK:**
Gemini auth error "no valid license" (#3501), NOT quota; it needs a fresh `gemini` login. This
is a 3-way strike, so it's a coverage gap, not a SOUND vote.
**Bundle:** v2 + this file's round 1 (38 KB). Maxwell's strike was written first; Carnot and Tesla
landed together when the collector returned.

## Per-family verdicts
| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | The pin has no home outside the SOPS-checked `.env`; the canary's soak proves only as much as its traffic |
| Kelvin (Gemini) | DARK | auth/licence error, not run |
| Carnot (GPT) | RECAST | The marker is a scar, not a proof; promote from an explicit canary verdict, not liveness |
| Tesla (Grok) | RECAST | Phase B is a second engine on the same fuel line; cutover still times migration with the 120 s knife; promotion is a self-reported latch |

## What round 2 confirms (unanimous across both rounds where it was struck)
- **Record admission, never rewind past it.** All three: finding 1's silent loss is GONE; the remaining price is an outage, which is the honest price.
- **Unknown revision ⇒ refuse, with no floors.** All three: finding 5 was dissolved, not rebuilt badly. **The compat guard has now held across two rounds and is buildable independently.**
- `.backup` + unlink db/wal/shm (finding 2 closed), Phase A retry-not-quarantine, `${ISLAND_IMAGE:?}`, all-three health, `flock`, pull-not-push, default off.

## Fatal flaws (deduped, most severe first)
1. **Phase B relocated a failure mode (Tesla; Carnot and Maxwell on its bus-less half).** On-host rehearsal shares the live box's disk and memory, and it has quarantine authority, so ENOSPC or an OOM kill condemns a good release and, on the canary, freezes `:stable` for the whole fleet. It also can't hear the bus (`--network none`). **This is the round-2 bar's "relocate, not remove" tripwire.** → Tesla's fold is a SUBTRACTION: delete on-host Phase B. The canary's real cutover is the rehearsal the fleet trusts. Any local smoke that remains loses quarantine authority.
2. **Cutover still times migration with the 120 s knife (Tesla).** Phase C re-runs the migration on live data under a shorter cap than the rehearsal. A slow rebuild either takes the restore arm (and gets quarantined) or touches the marker just before the timeout (and goes terminal). → One long budget covering migrate + listen; a migration still running is a wait, not a verdict.
3. **The marker's identity and placement (3/3).** It's keyed by build ref, so a stale `.served-vX` from an earlier life suppresses a safe restore. It's written before lifespan, so a lifespan crash counts as "may have served". → Key it by candidate digest (and container id). The watcher clears it before cutover and reads it only after `compose stop`. Maxwell: the app writes it at end of lifespan, just before the bus subscribe and the bind.
4. **Promotion is a self-reported, one-shot latch over an unmeasured canary (3/3, three different fixes).** `started_at` is the canary vouching for itself in one sample. A post-promotion death doesn't un-promote. The soak proves only as much as the canary's traffic, and the phones ring on enspyr, not imagineering (Maxwell). → **Nick's call** (see disposition): consecutive-sample memory in CI (Tesla), an explicit canary verdict artifact (Carnot), or activity evidence / canary = busiest island (Maxwell).
5. **The pin has no home (Maxwell).** A watcher write to the SOPS-rendered `.env` trips #320's byte-for-byte drift check on every update. → A watcher-owned second `--env-file`; `ISLAND_VERSION` leaves SOPS (ct#5165) as a named prerequisite.
6. **The terminal state has no recovery ritual, and the only lever written down is the serve-anyway override (Tesla, Carnot).** → Write the recovery into §3, forbid `MIGRATE_ALLOW_UNKNOWN_REVISION` on that path, and persist diagnostics (phase, digests, schema heads, marker state, guard refusal).
7. **The dead-man page rings on the living (Tesla).** An intentional cutover is the same silence as a dead island, so the operator learns to ignore it. → The watcher renews an off-box lease before stopping anything; the page fires on lease expiry plus host-failed state.
8. **Bootstrap cliff, state manifest as prose (Carnot).** → Exact first-guarded-release steps. The manifest becomes a checked list per mounted path (this partly overlaps Maxwell's finding 5).

## Disposition
**RECAST → v3 = round 3, the LAST** (≤ 3). The round-2 bar asks whether the folds shrink or grow.
The two biggest folds **shrink** v3: deleting on-host Phase B, and one clock instead of two. The
narrow ones (marker identity, pin home, lease page) are local. **The growth risk is finding 4:**
Carnot's verdict artifact plus smoke suite is the design-16 ratchet if taken whole. Take the
smallest promotion signal that removes the self-report, and name healthy-but-wrong detection as
a tradeoff owned by the canary's operator (Nick), not a mechanism. If v3 can't fold 1–7 within
~1.5× v1 by SUBTRACTING Phase B, stop: ship the compat guard alone and leave auto-update parked.
**Kelvin's seat should be restored before round 3**; a final round shouldn't run short a family.

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** "Record admission, don't gate it" is a real dissolve of round 1's worst finding, but v2 smuggles in two new couplings: its pin lives in the file the operator's drift check says must equal SOPS byte-for-byte, and its canary's evidence is only as good as the canary's traffic, which nobody measured.

**Fatal flaws:**
- **§3 Bootstrap: `ISLAND_IMAGE` has no home that doesn't collide with something (coupling to remove, not guard).** The watcher "sets `ISLAND_IMAGE=<registry>@<digest>`", and `update.sh` "writes `ISLAND_IMAGE` from `ISLAND_VERSION`". But written WHERE? Today the box's `.env` is SOPS-rendered and checked byte-for-byte against SOPS (the Dart `check_env_drift`, #320). Any watcher write to `.env` makes the box read as drifted on every auto-update, and the operator learns to ignore the drift alarm, which is the alarm design 16 died to install. Hans Gruber: "I wanted this to be professional." **Fix:** the pin lives in its OWN watcher-owned env file, passed as a second `--env-file` (Compose merges repeated `--env-file`, already measured), and `ISLAND_VERSION` LEAVES SOPS (ct#5165). That's two designs touching one variable, so name the dependency instead of discovering it.
- **§2: the canary's evidence is proportional to the canary's traffic, an unstated assumption (wrong option-frame).** The promotion rule is "imagineering reported `ref == vX` and has been up ≥ 24 h". That proves liveness, which the watcher already proved. It proves "healthy-but-wrong didn't happen" only if imagineering was USED for those 24 h. The handsets that ring live on enspyr (the real Android rings were through enspyr), so the island with the traffic is the one that FOLLOWS. A release that breaks calls promotes cleanly off an idle canary. **Fix:** either name the canary as the island with real use (and accept that Nick's busiest island takes every release first), or make promotion require activity evidence (the island mark's public aggregate activity bucket, already decided public, being non-zero across the soak). Don't let "soak" quietly mean "elapsed time on an idle box"; that's v1's clock, renamed.
- **The `.served` marker is keyed on build `ref`, not on THIS attempt (first-arrival / stale state).** A release that was applied, rolled back by hand, and then reapplied finds `.served-vX` already present from the earlier generation. A same-ref rebuild does too. The watcher then reads "may have served" for a generation that never got past migrate, and goes to a needless host-failed outage. **Fix:** the watcher deletes `.served-<ref>` for the candidate before Phase C step 2, and the marker carries the container id, which the watcher checks.
- **The marker is placed as conservatively as possible, and on a migrating release that converts every post-start failure into an OUTAGE (relocated, not removed?).** Marker-present + migrated release ⇒ previous refuses ⇒ terminal state. That's right over silent loss. But the marker fires before `exec uvicorn`, so a lifespan crash (bad config, a bus client that throws on connect) counts as "may have served" when it provably served nothing. Uvicorn runs lifespan startup BEFORE it binds the port. The common release failure is exactly the one that turns into a page. **Fix:** the app writes the marker itself, at the end of lifespan startup, immediately before the bus subscribe and the bind. That's one line of app code, and it narrows "may have served" to "did start serving".
- **Rehearsal can't see the failure class the cutover is most likely to hit (missing failure mode).** Phase B is `--network none`, so the bus path is never exercised. After the marker fix, the residual outage case is "boots, then dies on the bus". Accept and NAME it, or let rehearsal join a throwaway mosquitto on an internal network (no client port), which costs one `docker network create`.

**What holds:**
- The dissolve of finding 1 is real: "data is never rewound past a recorded start" is a property of the rule, not of timing, and Kelvin's chaos test genuinely has nothing left to prove.
- Collapsing the compat guard to "unknown ⇒ refuse" with no floors is the honest reading of v1. Every floor was self, so the table was ceremony. The PR#116 reversal is correctly argued from PR#116's own "NOT yet enforced".
- Promotion read from a public URL, with quarantine-withholds-promotion falling out of `ref ≠ vX` for free: no new credential, no new signal.
- The `OnCalendar=` window, the `.backup` (no WAL) restore, and the mosquitto manifest argument from clean sessions are each the smallest correct thing.

**If RECAST, what to fold back:**
- Name the pin's home: a watcher-owned `--env-file`, with `ISLAND_VERSION` out of SOPS (ct#5165) as a stated prerequisite.
- Define what the canary's soak proves, and make it require activity or name the canary as the busiest island.
- Move the marker into the app at end-of-lifespan, keyed by container id; delete a stale one before cutover.
- Either give rehearsal a throwaway broker or name "dies on the bus after start" as the residual outage.
- Add a manual promote override (a security hotfix shouldn't wait 24 h) next to `PROMOTION_HOLD`, and a retention rule for `pre-<digest>.db` (disk full was round 1's named 3 am fault).


---

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. v2 honestly closes several v1 wounds: it stops pretending `/health` is admission, deletes fake compat floors, moves soak into promotion, and rehearses on copied data. But it has relocated too much of the safety proof into a fragile marker, a public canary contract, and an under-specified rehearsal/cutover state machine. The design is closer, not yet reversible. Dijkstra: "Simplicity is prerequisite for reliability." This still needs one more fold-back, not a bigger machine.

**Fatal flaws:**
- The `.served-<build ref>` marker is a conservative scar, not an admission proof. It is written after migration and before `exec uvicorn`, so marker-present means only "migration completed," not "served." That is safe against data loss, but it changes the product semantics: many post-migration failures become terminal instead of recoverable. The doc calls this honest, but undercounts the operational blast radius: every health regression after migration strands the host until a human intervenes.
- The marker identity is too weakly specified. It is keyed by build ref, while the watcher reasons by candidate digest. Rebuilds, retags, multi-arch index changes, or a repeated failed attempt of the same ref can make old marker state influence a new decision. The rollback safety boundary should be keyed to the exact image digest plus schema head, not a human release ref.
- Phase B rehearse-with-`--network none` may certify the wrong thing. The promoted health condition requires `aiko_connected: true`, but rehearsal intentionally makes it false and then asks whether bus-less boot is enough. That open question is load-bearing. If the gateway’s durable behavior depends on broker connection, startup ordering, subscriptions, retained messages, or MQTT client state, rehearsal has deleted the very coupling that can fail in production.
- The channel design makes `:stable` depend on one public `/health` record and process uptime. That bounds some entropy, but it is not a release verdict. A canary can be healthy while workers are broken, writes are semantically wrong, migrations damaged rare paths, or the operator manually repaired it. `PROMOTION_HOLD` is a hand brake, not telemetry. Feynman: "The first principle is that you must not fool yourself." A public health endpoint is not a canary result unless the canary writes an explicit pass/fail artifact.
- The design still has a wrong option frame around healthy-but-wrong. It declares such releases out of scope, while auto-promotion is exactly the mechanism that spreads them. If the system promotes based on health, then the design owns the definition of health enough to say what smoke writes, worker checks, and rollback/quarantine signals are required before `:stable` advances.
- The terminal state is safer than corruption, but it is too coarse. "Previous refused" may mean the compat guard correctly protected data, but it may also mean bad label parsing, missing image metadata, compose drift, disk pressure, bad env, or a watcher bug. Marking the host failed and not quarantining is reasonable, but the design does not preserve enough forensic state to distinguish release fault, host fault, and updater fault.
- State inventory is improved but still asserted rather than nailed down. `mosquitto_data` is excluded because the gateway uses a clean session, but the design does not prove registrar/chat, retained messages, QoS choices, worker locks, future uploads, generated artifacts, or side effects outside SQLite remain disposable. Entropy hides in the unlisted writable path.
- The one-time compose hand-sync is a bootstrap cliff. The watcher refuses pre-guard targets and `preflight-compose-drift.sh` enforces sync, but the design does not define how an operator safely reaches the first guarded release when the old compose format cannot express `${ISLAND_IMAGE:?}`/digest semantics. That is not fatal to the steady state, but it is fatal to the first update path if left as ceremony.

**What holds:**
- Unknown Alembic revision now refuses by default. That is the correct second-law move: once schema state has advanced irreversibly, old code must not pretend reversibility exists.
- Deleting backward floors is a real simplification. The v1 graph problem is not solved; it is dissolved until there is CI proof strong enough to carry it.
- The three-phase watcher shape is sound: prepare while live, rehearse on a copied database, then make cutover short. This removes wasted outage time and avoids quarantining ordinary pull failures.
- Using SQLite `.backup` and deleting WAL/SHM on restore correctly closes the v1 WAL hole.
- Default `AUTO_UPDATE=off`, explicit `stable` versus `latest`, shared `flock`, no registry credentials on boxes, digest pinning, and a host-failed terminal state are all strong moves.
- The design is refreshingly honest that after marker-present migration, automatic data restore is forbidden. Hamming: "The purpose of computing is insight, not numbers." The insight here is right: rollback is a state-equivalence proof, not a command.

**If RECAST, what to fold back:**
- Key `.served` markers by exact candidate index digest plus schema head, not build ref. Define marker lifecycle: when created, when retained, when cleared, and how repeated attempts behave.
- Replace public `/health` promotion with an explicit canary verdict artifact produced by the canary watcher after soak: digest, started_at, all container health, gateway health, migration result, smoke-write result, rollback/quarantine state, and operator hold. CI should promote `:stable` from that artifact, not infer success from liveness.
- Close Phase B’s open question by adding a minimal broker in rehearsal or explicitly proving broker state is irrelevant. If production cutover requires `aiko_connected: true`, rehearsal should test that path too.
- Define the smoke suite that makes `:stable` meaningful: at least one durable write/read through the gateway, worker/container health for all three services, and whatever bus path is necessary to catch registrar/chat regressions.
- Write the persistent-state manifest as a checked artifact, not prose. Enumerate every mounted writable path per service and classify it: restored, disposable, externally durable, or forbidden. Future writable volumes should fail preflight until classified.
- Make terminal-state diagnostics part of the design: persist candidate digest, previous digest, schema head before/after, marker status, phase, command exit, health bodies, and whether the compat guard refused. The page should tell the operator which thermodynamic wall was hit.
- Spell out the bootstrap path for the first guarded release: exact manual steps, minimum version label, what `preflight-compose-drift.sh` checks, and the point after which auto-update may be enabled.
- Verdict target: RECAST, not DISSOLVE. The simpler alternative is not "no auto-update"; it is "promote only from an explicit canary verdict, and make marker/state identity exact." That trims wasted work while preserving Nick's requested operator opt-in auto-update.


---

## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** The `.served` marker removes the silent rewind, and the design then tunes three clocks to the wrong frequency: a rehearsal that spends the live box's own disk and memory and condemns the release for it, a cutover that still judges a slow migrate in 120 seconds, and a `:stable` latch driven by a timestamp the canary speaks about itself.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- **§3 Phase B is a second engine on the same fuel line, and it is allowed to condemn the digest.** Rehearsal runs a full candidate, plus an online `.backup`, on the production host while the live stack is up. On a one-disk island the copy and the live database share a filesystem, so the 3am full-disk fault from round 1 still wedges the serving process, only now it happens before any cutover. The same doubling is a memory spike: the OOM killer's nearest victim is the live gateway. The design's attribution sentence is the break: rehearsal failure quarantines the digest because "it failed on a copy of real data, so the release is at fault, not the host." ENOSPC, an OOM, a scratch-volume permission, and a live `/health` blip during the copy are host energy. Phase A already knew that a disk fault retries and does not quarantine; Phase B forgets. On the canary that sticky quarantine also freezes `:stable`, because promotion moves only while the canary is actually serving the newest ref. One small box's disk becomes a fleet-wide stall. The isolation boundary is unnamed: a rehearsal started with the production compose project recreates the live containers, and "everything still up" is then false.
- **§3 Phase C.3 still times the migrate with the 120-second knife. The 300-second rehearsal is discarded.** The marker is touched after `migrate` returns and before `exec uvicorn`. A table rebuild that is still running at 120 seconds has no `.served-<candidate>` yet, so the watcher takes the marker-absent arm: restore the backup, start previous, see previous healthy, quarantine the candidate. A rebuild that finishes and touches the marker near second 100, with uvicorn still coming up, takes the marker-present arm: on a release that added a revision the compat guard refuses and the host goes terminal. Rehearsal was the piece that was supposed to take slow migrations off the outage clock. Cutover runs that same migration again, on the live volume, under a shorter cap, and both arms punish a release the rehearsal may already have watched succeed. Finding 3's "slow rebuild and bad image are the same signal" is still the cutover signal.
- **§3 open questions 1 and 3, with the §1 override, leave the original event horizon unplayed, and the page teaches the operator to un-refuse.** Round 1's writes happen after the process listens and the lifespan joins the bus. That entire stretch sits after the marker. Phase B uses `--network none`, so `aiko_connected: false` is structural and the bus-join path is the one path the rehearsal cannot hear. A migrating release that dies there is marker-present, guard-refuses, host-failed, with `pre-<digest>.db` sitting unused. The only lever written down is `MIGRATE_ALLOW_UNKNOWN_REVISION=1`, which is PR#116's serve-anyway restored as the 3am runbook, because the terminal state has no recovery ritual. The marker's own frequency is wrong in three more ways. It is keyed by build ref, so a stale `.served-<ref>` from an earlier life of that version suppresses the safe restore when a later attempt migrates and dies before its own touch. Under `restart: always` it proves the entrypoint crossed one line once, which open question 1 already suspects, and a restart policy left armed fights the watcher during rollback. Registrar and chat mount no volume and write no marker, and `up -d` starts them with the gateway, so they can emit off-box effects while the marker is still absent and the restore still claims to lose nothing by construction.
- **§2's promotion is a self-reported latch, and §3's dead-man rings on the living.** The hourly job trusts `started_at` from the public `/health` body, one sample, joined to `build.ref` rather than the digest that actually soaked. A clock fast by a day, a field that reports build time, or a missing field parsed as zero collapses `SOAK_HOURS`. "A canary that rolled back reports `ref ≠ vX`" withholds promotion only before `imagetools` moves `:stable`. After the latch, a death at hour 25 still feeds every opted-in island. Quarantine does not un-promote. The deleted `AUTO_UPDATE_DELAY_HOURS` returns as a one-shot delay whose oscillator is the subject under test. The off-box page is silence during that same window. Phase C's intentional stop is silence of the same kind, so a successful cutover pages, the operator learns that the page means "the update," and the real host-failed silence arrives on a trained-out channel.

**What holds:**
- Recording admission and refusing to rewind past it is the right reduction. A restore that will not cross `.served` cannot silently eat an ack. Finding 1's data loss is gone, and the remaining price is an outage, which is the honest price once the recovery path is real.
- `sqlite3` `.backup` plus deleting `aiko.db`, `aiko.db-wal`, and `aiko.db-shm` is a whole SQLite set. Finding 2's hybrid replay is gone. Leaving `mosquitto_data` unrestored matches a clean session and matches today's manual deploy.
- Unknown revision refuses, the watcher never sets the override, and backward floors stay deferred until a graph walk and a CI boot of the old binary exist. Finding 5 is removed rather than rebuilt badly.
- Phase A pulls and drift-checks before the stop. Registry and preflight faults retry and do not quarantine. The island pulls, CI holds no SSH, `flock -n` is shared with `update.sh`, and `AUTO_UPDATE=off` is the default.
- `${ISLAND_IMAGE:?}` with a named compose hand-sync closes the empty-pin trap. Previous is pinned by id and digest. Healthy means all three containers plus gateway `/health`. The host-failed terminal state is the right third state for a double failure. Its pager and its recovery are the parts that still hum.

**If RECAST, what to fold back:**
- Delete on-host Phase B, or strip it of quarantine authority. The canary's real cutover is the rehearsal the fleet is allowed to trust. A second process on the production box is how this round relocates the failure mode. If a local smoke remains, a resource fault (the copy will not fit, the live stack blipped, the candidate was OOM-killed) retries next poll and does not quarantine, and the smoke runs in a named compose project that cannot recreate the live services.
- Give Phase C the long clock. One budget, the rehearsal's 300 seconds, covers migrate plus listen. Quarantine a digest when the candidate exits non-zero or fails `/health` inside that budget after the marker. A migrate still running inside the budget is a wait, not a bad release.
- Close open questions 1 and 3 in the doc. The marker is the candidate digest, it lives on `aiko_data`, and the watcher reads it only after `compose stop` has dropped the restart policy. Roll back or supersede and the marker is removed. Registrar and chat do not migrate and do not touch it; their off-box effects are accepted loss on any stop, same as a manual deploy. Write the terminal recovery into §3: with no evidence of a client write, restore `pre-<digest>.db` and start previous; `MIGRATE_ALLOW_UNKNOWN_REVISION` is forbidden on that path, and the page text says so.
- In §2, the CI job keeps an external memory: the first hourly sample whose reported digest equals the newest release and whose health is green, promote only after `SOAK_HOURS` of consecutive green samples, reset on any miss. A `started_at` in the future or a missing digest fails closed. Store and promote that digest. A later canary miss before the fleet has pulled moves `:stable` back or holds it. The latch has to be able to open.
- The watcher pets the off-box dead-man before Phase C stops anything, with a lease longer than the cutover budget plus rollback. The page fires when the lease expires and `deploy/.auto-update/state` is still host-failed. A successful cutover must not be the same silence as a dead island.
- Default `OnCalendar` to the low-entropy window already named for enspyr (`03:00..05:00`). Every 15 minutes remains an explicit operator edit.

