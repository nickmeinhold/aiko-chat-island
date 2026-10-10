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
