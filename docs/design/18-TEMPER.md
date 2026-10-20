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
Gemini "no valid license" (#3501). **Cause (claude-skills tab, verified 14:35): Nick cancelled
Code Assist and unassigned its licence today**, so the `gemini` CLI is dark everywhere and a
login won't fix it. Kelvin is being moved to a direct API call (claude-skills
`fix/kelvin-stderr-separate`). This is a 3-way strike, so it's a coverage gap, not a SOUND vote,
and NOT evidence about Gemini's quality.
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
**Round 3 runs without Kelvin** (Nick, 2026-10-10 16:26: "qwen is going to take a while, just roll without kelvin"; a Qwen seat is planned). Gemini was evaluated the same day and is being dropped: on design tempers it yielded 0.34 unique-folded flaws per strike against Carnot's ~1.0, and it vouched for 35+ flaws that others folded (claude-skills `docs/crucible/kelvin-yield/design-temper/`).

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



---
---

# Round 3: design 18 v3 (the LAST)

**Overall verdict: RECAST, 3/3** (Maxwell, Carnot and Tesla all RECAST; zero DISSOLVE). **Round cap reached.**
**Struck:** dt-1791624402, 2026-10-10. Panel: Maxwell + Carnot + Tesla (Kelvin dropped on evidence; Qwen not yet seated). 64 KB bundle. Maxwell's strike was written first; Carnot and Tesla landed together.

## Per-family verdicts
| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | The lease measures the watcher's pulse, not the island's: a release that dies after being recorded current never pages |
| Carnot (GPT) | RECAST | The `.served` boundary is gateway-only while three services start; the manifest is prose |
| Tesla (Grok) | RECAST | A global `last_red_at` is struck by the cutover itself, so `:stable` freezes; deleting the marker before the backup lets a failed backup delete the live db |

## Fatal flaws (deduped, most severe first)
1. **Phase B can delete the only good database (Tesla).** v3's step 1 deletes `.served` *before* `.backup` + `integrity_check`. A backup that fails (disk filled after the Phase A check) then falls into the marker-absent arm, which deletes `aiko.db` and copies in the failed backup. **Introduced by v3.** → Reorder: a checked `pre-<digest>.db` must exist before `.served` is deleted or any failure arm touches the live set. A backup failure starts previous on the untouched db, retries, and doesn't quarantine.
2. **Container-id matching reintroduces round 1's silent rewind (Tesla).** Under `restart: always`, container C1 writes the marker, serves, and dies; the watcher reads after stop and sees C2. A strict id match reads as absent, so the restore runs and C1's acks are lost. **Introduced by v3** (added to fix round 2's stale marker). → Marker-present = a marker for this DIGEST exists; containers append; an id mismatch is never absence. Clearing it before cutover already handles staleness.
3. **Promotion freezes (Tesla; Carnot names the policy gap).** One global `last_red_at` is moved by the nightly cutover window itself (the sampler hits the 600 s outage), by the night syd and melb disagree during rollout, and by any single probe flake. So no release's first-seen ever postdates the last red. **Introduced by v3's 7-day change.** → Reds are per digest, counted only while an island is SERVING that digest, and a sample taken during a planned cutover lease isn't a sample.
4. **Watcher health omits `aiko_connected`, and the lease isn't conditional (Tesla, Maxwell).** A bus-deaf release on the canary records current, renews the lease, and never pages. A release that dies after "current" also keeps the lease fed. → The watcher's healthy predicate includes `aiko_connected`, and the lease is pinged only when that predicate passes, on every run.
5. **"Nothing served by construction" overclaims (Carnot, Tesla).** Registrar and chat start on the same `up -d` and can emit off-box effects before the gateway marker exists. → Shrink the claim to "no gateway data-volume write was admitted"; registrar/chat off-box effects are named accepted loss (as on every manual deploy). Don't build a three-service gate.
6. **Smaller (all narrow):** syd shares melb's window, so it can't refuse a digest melb quarantined (stagger syd later, Tesla); name the variables-write credential and add `concurrency:` (Maxwell); probe exclusion from the island mark needs a flag the count honours (Maxwell); the manifest becomes a checked file (Carnot); marker timestamp + per-service diagnostics for the terminal runbook (Carnot).

## The pattern across three rounds (named as a class, not three instances)
Every round found its most severe flaw in the **failure arms of Phase B's prose step list**:
- **Round 1:** the step order contradicted the quarantine rule.
- **Round 2:** the rehearsal relocated a failure, and the 120 s knife misjudged slow migrations.
- **Round 3:** the marker was deleted before the backup, and a container-id mismatch was read as absence.

And the round-3 severe flaws (1–3) were each **created by a round-2 fold**. **The size bar held (v3 is 1.38× v1); the "remove, don't relocate" bar did not**, for three of v3's new pieces. Severity did not decline across rounds, so this is not converging the way a clean final round would.
The mechanism the class calls for is not another prose fix. **Phase B should be written as a state table**: every step × {success, failure} → the next state, plus the invariant that must hold in each state (e.g. "a checked backup exists" before any live-set mutation). A reviewer, or a test, can then check every cell instead of reading a narrative for the one missing arm.

## Disposition
**The round cap (3) is reached, and the core is TEMPERED:** the compat guard (unknown revision ⇒ refuse, no floors) held all three rounds, unanimously, and is **ready to build**. So is record-don't-rewind as a principle, and the `.backup` set restore.
**The watcher and promotion layer is NOT tempered.** Each round's fixes produced the next round's severe flaw. Folding round 3 as prose and shipping would ship a fourth generation nobody struck. **Recommended:** build the compat guard now; rewrite Phase B as a state table (v4), fold flaws 1–6 into it, and strike v4 as a NEW round series once the Qwen seat lands, rather than as round 4 of this one. That's Nick's call: it overrides the cap.

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** v3's spine holds, and its marker placement is checked against the code. But the dead-man lease measures the WATCHER's pulse, not the island's, so the one failure v3 hands to a human, a release that goes bad AFTER it was recorded current, never pages anyone.

**Fatal flaws:**
- **§3 Paging: the lease is pinged by the watcher, so a crash-looping island that the watcher has already called healthy keeps renewing it (missing failure mode, under-counted blast radius).** Step 3 records the digest as current once all three containers are healthy within the budget. If the gateway starts crash-looping ten minutes later (a healthy-but-wrong release that turned into a down release), every later watcher run sees "channel digest == running digest", has nothing to do, and pings the lease. `restart: always` keeps the container technically up between crashes. So the island is down and the page is silent, on exactly the canary (enspyr-melb) whose real use v3 relies on to catch the tail. John McClane: "Welcome to the party, pal." **Fix:** the ping is CONDITIONAL. The watcher pings only if, at that moment, all three containers are healthy and `/health` returns 200. It's the same predicate as step 3, so it adds no new mechanism: just "ping if healthy" instead of "ping on run". Host-failed and post-cutover death then page the same way.
- **§2: the promotion job's memory needs a credential v3 doesn't name (unstated assumption).** "Repo variables" written by a scheduled workflow: the default `GITHUB_TOKEN` can't write Actions variables, so this needs a fine-grained token or an App with variables:write, plus `concurrency:` so two hourly runs can't interleave read-modify-write on the streak. v3 already names the probe password as a credential CI holds, so it should name this one too, or keep the state somewhere `GITHUB_TOKEN` can already write (an orphan-branch JSON file, committed).
- **§2: excluding the probe from the island-mark counts is code, not a sentence.** The activity aggregate has no notion of a probe account today. Either add an explicit, operator-visible flag on the probe user (and the count query honours it), or accept that enspyr-syd's mark shows activity from a robot and say so. Otherwise the k-anonymity ruling's public numbers include CI.

**What holds:**
- **Marker placement, checked against `main.py`:** before `state.bus.start()` (line 250) lifespan only runs `verify_schema()` and the push-reachability read. The channel worker consumes a queue that only bus events fill, and gossip and nudge start after `bus.start()`. So "written immediately before the bus subscribe" really is the first point at which this generation can consume or serve. The port binds after `yield`. Record-don't-gate is now true of the code, not just the design.
- **Deleting the on-host rehearsal** removed a relocated failure instead of guarding it, and v3 shrank (1.38× v1) while closing round 2. That's the opposite of design 16's ratchet.
- **First-seen + last-red promotion** makes "stable" mean something at this release cadence without a reset-on-new-release stall. Not moving `:stable` backwards is correctly argued: a channel downgrade would cross migrations the guard refuses.
- **The terminal runbook forbids the serve-anyway override**, and the diagnostics record the window between the marker and the failure, so a human restore is an informed loss rather than a silent one.

**If RECAST, what to fold back:**
- The lease ping is conditional on the step-3 health predicate, evaluated on every run.
- Name the variables-write credential and add `concurrency:` (or move the streak state to a file `GITHUB_TOKEN` can commit).
- Make the probe-exclusion an explicit flag the activity query honours, or name the robot activity as accepted.


---

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. v3 closes the round-2 wounds honestly in the main line: deleting on-host rehearsal is the right subtraction, one long budget removes the migration knife, the marker is now keyed to the digest/container and placed late enough to mean something, and the watcher-owned env file avoids poisoning SOPS drift. But the proof still has two hot leaks: the `.served` boundary is gateway-only while three services are started, and promotion still treats two health probes plus one synthetic path as enough evidence to advance a whole fleet. Dijkstra: "Simplicity is prerequisite for reliability." The fold-back is small, but it must be explicit before this ships.

**Fatal flaws:**
- The rollback proof says marker absent means "nothing was served or consumed," but only the gateway writes the marker. The watcher starts gateway, registrar, and chat together; registrar and chat mount no volume, but they can still accept connections, subscribe, ack, emit webhooks, register presence, or produce other off-box effects before the gateway marker exists. The doc later says those off-box effects are accepted, but that contradicts the stronger safety claim. This is entropy sneaking through an unmeasured boundary: either all externally serving/consuming processes are held behind the same admission event, or the guarantee must shrink to "no gateway data-volume writes were admitted."
- The app-side marker placement is still underspecified at the exact thermodynamic event horizon. It says "after migration, immediately before the bus subscribe and the port bind," relying on uvicorn binding only after lifespan. That is a fragile coupling to framework startup order and service-specific code paths. If any route, socket, background task, or bus consumer can start outside that ordering, the marker-absent restore can lose acknowledged work. Feynman: "The first principle is that you must not fool yourself." This needs an implementation invariant: no client port bind, no WS accept, no MQTT subscribe/ack, and no externally visible side effect before the marker write succeeds.
- The state manifest is prose, not yet a checked artifact. v3 says every volume in compose must be classified, which is good, but the classifications themselves are semantic assertions: `mosquitto_data` is disposable because gateway uses a clean session; registrar/chat have no volume; worker-guard lock is disposable. That can rot the first time someone adds a retained topic, durable subscription, upload path, generated file, cache that becomes authoritative, or new mount. The design must make this a machine-checked manifest with service/path/classification/reason, and fail preflight on any unclassified writable mount. Otherwise the second law wins quietly: state only increases.
- Promotion still undercounts healthy-but-wrong while owning the mechanism that spreads it. v3 improves v2 by using CI memory, two islands, digest matching, and a synthetic round trip. But `:stable` advances on one scripted gateway login/post/read plus `/health` from the busy canary. That does not cover registrar/chat behavior except container health, Aiko phone-ring semantics, worker paths, auth edge cases, or migrations that damage rare data. The doc names this tradeoff, but because stable auto-promotes fleet-wide, this cannot be merely "out of scope"; it must be a named acceptance criterion owned by the operator: stable means exactly this smoke suite and no more.
- The `last_red_at` rule is conservative but can deadlock promotion for the wrong release. A red sample from a newer `:latest` digest after an older digest has already soaked clean makes the older digest ineligible because its first-green time is not after `last_red_at`. That converts "new bad release" into "no stable promotion of prior good release" until a human hotfix override. This may be acceptable, but it is not named. Hamming: "The purpose of computing is insight, not numbers." The insight needed here is whether stable should promote the newest clean digest before the red, or whether any red freezes the channel by policy.
- The terminal runbook still asks the operator to choose between fix-forward and restoring `pre-<digest>.db` while accepting lost writes since marker time, but the design does not prove the diagnostics contain enough to identify that window across all services. The marker has digest/container, but not necessarily timestamp, schema before/after, service readiness sequence, bus subscription timing, or registrar/chat effects. v3 lists many diagnostics, which is good; make marker time and per-service admission/consume state explicit, or the operator is doing archaeology under pager pressure.

**What holds:**
- The biggest round-2 flaw was actually dissolved: on-host rehearsal is gone. That removes the second engine on the same disk/memory reservoir instead of armoring it with more ritual.
- One `AUTO_UPDATE_BUDGET` covering migration and startup is the right thermodynamic accounting. A running migration is heat in the system, not yet a verdict.
- Unknown Alembic revision refusing by default, with no compat floors, remains the strongest part of the design. It converts irreversible schema advance into an honest stop rather than a pretend rollback.
- The watcher-owned second `--env-file` is the correct low-coupling home for `ISLAND_IMAGE`; it preserves the SOPS drift alarm instead of teaching operators to ignore it.
- Digest plus container id for `.served`, deletion before cutover, and reading only after `compose stop` are materially better than v2's build-ref scar.
- The off-box lease is the right pager shape. A planned outage renews the lease; a dead or host-failed box stops renewing it.
- Default `AUTO_UPDATE=off`, explicit `stable`/`latest`, shared `flock`, pull-before-stop, all-three health, previous pinned by digest/id, and refusing auto-restore to pre-guard images all hold.

**If RECAST, what to fold back:**
- Keep v3, but weaken or complete the core invariant. Preferred fold: all three services must share an admission gate, and no service may bind a client port, accept WS, subscribe/ack MQTT, or emit off-box effects before the gateway marker/admission point. Smaller fold: explicitly reduce the restore guarantee to gateway database state and name registrar/chat pre-marker effects as accepted loss.
- Turn the state manifest into a checked file consumed by preflight: every compose volume and writable mount classified as restored, disposable, externally durable, or forbidden, with future unclassified paths failing Phase A.
- Make promotion semantics explicit: `:stable` means health plus the named synthetic round trip plus real-use canary observation, not general correctness. Add registrar/chat smoke only if those services have user-visible release risk; otherwise state the residual risk in the operator-facing setting.
- Decide and document the `last_red_at` policy: either global freeze on any red is intentional, or promotion may select the newest digest whose own clean window predates a later bad `:latest`. Right now the policy is hidden in the variables.
- Persist marker timestamp and per-service startup/admission diagnostics. In terminal state the operator should know exactly whether any service crossed the irreversible boundary before choosing fix-forward versus data restore.
- Verdict target: RECAST, but narrow. Do not add rehearsal back. Do not build a giant smoke cathedral. Fold the admission boundary across all serving processes, make the manifest executable, and ship the remaining healthy-but-wrong risk as an explicit operator tradeoff.


---

## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** v3 took the second engine off the live box, then coupled every release to one global red that the 3am cutover itself strikes, so `:stable` goes still, and a failed backup after the marker is wiped can delete the only database still singing.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- **§2's promotion predicate contradicts the cadence sentence it was written to save.** The prose says a newer release must not reset the soak, because a reset streak never promotes at this cadence. The machine is one global `last_red_at`: the last red sample from either island, missing field included, fail closed. A sample is green only when both `:latest` islands match the release. `:stable` moves only to a digest whose immutable first-seen is at least `SOAK_DAYS` (7) ago and still after that global red. A newer digest landing makes syd and melb disagree for a night, and that disagreement is red, so `last_red_at` jumps past the previous release's first-seen. That release never qualifies again. The same clock is struck by the ritual §2 and §3 schedule together: the sampler is hourly, the default window is `03:00..05:00`, and cutover stops the stack inside a 600s budget. A sample in that window is a failed `/health`, which is red, which moves the same variable. One flake of the enspyr-syd probe (CI password, one post, one WS read, 168 hourly chances in seven days) does it too. Round 2's rehearsal let one small box freeze the fleet. v3 deleted the rehearsal and gave that authority to a single sample. After promotion, a red only stops further promotion and opens an issue. `:stable` stays on the bad digest. The issue does not conduct. Islands that already migrated cannot take a backward tag, because the guard would refuse them. Islands that have not pulled are the ones a hold could still save, and they still resolve the condemned digest.
- **§3 Phase B step 1 destroys the proof, and the absent-marker arm then destroys the live set.** The order is stop, delete `.served`, then `.backup` into `pre-<digest>.db`, then `integrity_check`. The failure arm is any error after step 1: `compose stop`, read the marker, and if it is absent, delete `aiko.db`, `-wal`, and `-shm` and copy `pre-<digest>.db`. Backup failure, a disk that fills after the Phase A "free space ≥ 2× the db" check (the last two backups already sit on that disk), or a failed `integrity_check` happens with the stack already stopped and the marker already gone. That exit is either undrawn, or it falls into the absent arm and deletes the live database to copy a file that just failed. Phase A said a disk fault retries and does not quarantine. After the stop, Phase B forgets. The container-id conjunction is the same wire. The marker counts only when digest and container id match, and `restart: always` stays armed during the budget. Container C1 can write the marker, bind, ack writes, and die. The watcher, reading after `compose stop`, sees C2. A strict match looks absent, the restore runs, and the acks C1 already gave are gone. Round 1's silent rewind comes back through the identity added to prevent a stale file.
- **The canary's green is a different note from the phones, and the other `:latest` island cannot hear a death.** The core move says a missing marker means nothing was served or consumed, so the restore loses nothing by construction. Registrar and chat start on the same `up -d`, mount no volume, and can emit off-box while the gateway is still migrating and `.served` is still unwritten. A failure then rewinds the database and leaves those effects standing. The watcher's healthy is three running containers plus `/health` 200. Promotion, on enspyr-melb, also demands `aiko_connected`. A bus-deaf release on the island where the phones ring still renews the lease, records current, and does not quarantine. The dead-man stays fed. CI withholds `:stable`, so the fleet is spared and the canary sits in the bad state with no page. Quarantine is local. enspyr-syd follows the same `:latest` tag and the same `03:00..05:00` local window, in the same timezone, so it neither leads melb nor reads melb's quarantine. A digest the canary has already condemned is still the tag the test island pulls that night.

**What holds:**
- Unknown revision refuses, the watcher never sets `MIGRATE_ALLOW_UNKNOWN_REVISION`, and the floor table stayed deleted. That guard has held three rounds and can ship alone.
- Deleting on-host rehearsal is a real subtraction. The live box is no longer a second engine with quarantine authority.
- One 600s budget, a migration still inside it treated as a wait, the marker written at the end of lifespan immediately before the bus subscribe and the bind, cleared before cutover, and read only after `compose stop`. That placement is the right place on the waveform.
- `.backup` plus deleting the `aiko.db` / `-wal` / `-shm` set. Watcher-owned `image.env`, off the SOPS byte-for-byte file. The lease renewed before the stop, with grace longer than the budget plus a rollback. Five ordered bootstrap steps. An unclassified volume fails Phase A. Host-failed stays stopped, with a runbook that forbids the serve-anyway override. `AUTO_UPDATE=off` by default. The island pulls. `flock -n` is shared with `update.sh`.
- Refusing to walk `:stable` backward is the right fear. A downgrade would march every migrated follower into the guard. What is missing is a hold that conducts forward.

**If RECAST, what to fold back:**
- Rewrite the §2 predicate until the cadence sentence is the mechanism. Per digest: first-seen, and reds only while an island is actually serving that digest, probe failure included. A newer digest's rollout, a sample taken while the lease is held across a planned cutover, and "not on it yet" must not move an older candidate's clock. A red pauses that digest's soak. It must not erase first-seen. Keep the ban on a backward tag. Add a digest hold the watcher reads before it pulls, so a post-promotion red stops islands that have not adopted it.
- In §3, a `pre-<digest>.db` that has passed `integrity_check` exists before `.served` is deleted and before any failure arm may touch the live set. Backup failure, short disk, or a bad check starts the previous image on the untouched database, retries next poll, and does not quarantine. Marker-present means a marker file for this digest exists. A restarted container id appends to it. A container-id mismatch is not absence.
- The watcher's healthy on a `:latest` island includes the same `aiko_connected` the promoter already requires, so a bus-deaf canary rolls back or the lease pages. Leave melb as the canary. Give syd a later window so it can refuse a digest melb has quarantined.
- If those folds start growing a smoke suite or a verdict artifact, stop. Ship the compat guard. Leave auto-update parked. This is round 3 of 3.



# v4 series, round 1

**Overall verdict: RECAST, 3/3** (Noether, Carnot, Tesla; zero DISSOLVE). Continue: flaws name MISSING cells, so the series' stop condition is not met.

## Per-family verdicts (v4 r1)
| Family | Verdict | One line |
|---|---|---|
| Noether (DeepSeek) | RECAST | P's identity and the backup's provenance aren't conserved across the retries the table itself schedules; T is written and never read |
| Carnot (GPT) | RECAST | No recovery entry for a watcher that dies mid-transaction; digest identity is never connected to what the app reports |
| Tesla (Grok) | RECAST | No steady-state cell, so every poll is a cutover; the watcher is tuned to a digest while the image only reports a git sha |

## Author's bundle error (recorded first because it is the series' own failure class)
The design branch was based on `d76e983`, BEFORE #392 merged, and the bundle was built from that working tree. Every seat therefore saw the **pre-#392** `migrate.py` / `db.py` (`MIGRATE_SKIP_UNKNOWN_REVISION`, no `schema_status`). Noether struck §1 for contradicting its bundled code, correctly. The [E] cells are unaffected (the harness ran the published 0.17.0 image), but the bundle disagreed with them. Series 1 lacked `db.py`; series 2 bundled a stale one. **Mechanism for v5:** build the bundle from `git show origin/main:<path>`, and assert in the bundler that `def schema_status` is present.

## Fatal flaws (deduped, most severe first)
1. **No steady state (Tesla).** A1 → A2 → B1 with no "running is already N and Healthy" arm, so every poll downs the stack. **Created by v4**: v3's A1 "compares it with the running one" was lost in the rewrite. → A1: running = N ⇒ lease iff Healthy, exit.
2. **Identity: digest vs git sha (all three).** `/health` `build` carries `git_sha`/`ref`, never a digest, so B4's `build = X` can't succeed. The gateway can't write a digest it doesn't know, and CI memory is keyed on a value nothing reports. → Compare by `git_sha`, pull by digest. A2 maps digest → `git_sha` from the pulled image's own metadata; the marker and CI key on `git_sha`.
3. **No durable transaction (Noether, Carnot).** `P` is recomputed from state the failure arms mutated. T is written but never read, so the next poll redeploys the host-failed release. A watcher dying mid-B leaves no recovery entry. A retry overwrites `pre-N.db` with an already-migrated copy. → A journal written before B1 (N, P, `head(P)`, stamp, backup path keyed by transaction, phase), read FIRST at A0: the T latch ⇒ exit without pinging; mid-transaction ⇒ a recovery entry. A2 checks `stamp == head(P)`. Backups are never overwritten.
4. **Local quarantine has no enforcement (Carnot).** A follower restores P and then retries N every poll. → The journal's quarantine set is read in A1.
5. **Marker not fail-closed or durable (Noether, Carnot).** A failed or unsynced append lets the gateway serve while B5 later reads absence and rewinds. → Append + fsync, or abort lifespan before `bus.start()`. An unreadable marker ⇒ T.
6. **B5 present ∧ migrates → T is too eager (Tesla).** The marker lands before `bus.start()`, so a slow broker after it reaches T on a release that may be fine. → One "N again" cell (restart N within a second budget) before T.
7. **The actuator socket (Tesla).** `image.env = N` is ambiguous, `update.sh` doesn't read `image.env`, and the drift preflight takes a tag (`update.sh:246`). → `ISLAND_IMAGE=<repo>@sha256:…`, a full reference. The preflight is fed the `ref` from the image's metadata. [A] until built.
8. **Promotion attribution gaps (Tesla, Noether).** An unreachable-twice red has no identity to hang on, and syd's 2 h lag is a sentence, not a cell. → Unreachable pauses ALL promotion (fail-closed, island-scoped). syd's A1 requires melb to report `git_sha(N)` Healthy before pulling N, so syd follows melb, not the tag.
9. **The lock is unenforced (Noether).** Manual operator commands bypass it. → `update.sh` takes the lock (bootstrap 3). A hand `docker compose` is a named tradeoff, owned by the operator.
10. **[E] cells exercise functions, not containers (Noether, Tesla).** E3/E4 call the migrator and `verify_schema` one-shot, never the real `CMD`. B5a's three-file wording vs WAL-off. → E3′: run the real `entrypoint.sh` container and assert it exits non-zero without binding. B5a: restore one file and delete any stray sidecars.

## The evidence-column check (pre-registered in v4: ≥ 70% of flaws in [A] cells)
17 tagged findings: **12 [A], 4 [E], 1 [C] = 71%.** The prediction held, narrowly. The [E] strikes were all real but of one kind: the harness proves a MECHANISM in one run, not the STATE a retry reaches (Noether B2/B6) or the container boundary (E3). So the tags did steer review, and they also told reviewers exactly where an [E] claim overreached. v5 should execute the retry state, not only label it.

## Disposition
Folds 1–10 are v5. Flaw 1 shows the class is not dead: a table rewrite can still drop an arm the prose had. **v5 check:** diff the v3 prose's arms against the v5 table's cells before striking. Round 2 of the v4 series follows on a bundle built from `origin/main`.
**Struck:** dt-1792406278, 2026-10-19, against v4 `57c08ce`. Panel: Noether + Carnot + Tesla (Maxwell is the author and did not strike). Bundle 108 KB, 13 files, including the code the cells cite.

## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The table conserves the backup *file* but not the backup's *provenance*, and it conserves the running image but not the *identity of P* — so the retry, which the design itself schedules ("retry next poll"), silently breaks the two invariants every failure arm leans on.

**Fatal flaws:**

- **A2 [A] + A0 [A] (missing cell: no state read) — "P" is not a conserved quantity, and T is not absorbing.** A2 says `record P` and never says *from where*; the two available sources are both wrong in exactly the state a rollback is needed. `image.env` is written to **N** by B4 and again by B6; `/health` is unavailable because the stack is down. So after B5's `present ∧ migrates → T`, the next hourly poll records **P = N**, computes `migrates := head(N) ≠ stamp` = **false** (the DB is already at head(N)) — which means the `present ∧ migrates → T` guard cannot fire again — and re-runs B2/B3/B4 on the release T declared host-failed. T's action is "stay down"; nothing enforces it. The table writes a state and no cell reads it. Class: written-but-never-read state + an identity mutated by the failure arms.

- **B2 [E1] / T [A] — the rollback artifact's provenance is not conserved across runs.** The artifact is keyed only by digest (`pre-N.db`), so the retry above overwrites the pre-migration snapshot with a snapshot of the already-migrated DB. The cell's invariant reads "a checked `pre-N.db` exists" — `checked` means `integrity_check` passed, not that it is *pre-N*. T's invariant "nothing deleted after B2" is a per-run claim used as a system-wide one. The E1 arm proves the backup mechanism works; it never proves the artifact survives a second attempt. (And it coexists with `update.sh`'s host-side, timestamped, never-overwritten `backups/aiko.db.preupdate-<ts>` — two artifacts, two locations, two provenance stories, and the runbook names one.)

- **B6 [E5] — the [E] cell does not exercise the state that triggers it: P is never validated against the DB.** `migrates := head(N) ≠ stamp` silently presumes `stamp == head(P)`. Reachable counterexample: B6's own failure arm leaves `image.env = P` while the DB sits at head(N); the next poll's B6 boots P against a schema P refuses, and B5a restores a snapshot that is already at head(N). The harness restores a *genuinely* pre-N db, so "previous image ADMITTED after restore" is a witness for a state the design can no longer guarantee it is in. Missing A2 check: `stamp == head(P)`, else there is no rollback target at all.

- **A0 [A] — the mutual-exclusion invariant is unenforced, by the design's own admission.** The cell's note concedes `update.sh` takes no lock today; the lock arrives only via a one-time manual bootstrap on a box whose `deploy/` does not sync itself (#2301), and the runbook's own fix-forward step ("fix forward with a newer release") is a hand `compose up -d` that never takes it. Failure: B5a's `rm aiko.db* && cp pre-N.db` interleaves with a concurrent operator deploy — the gateway writes into a DB being replaced, on the sole-copy store. The interlock rests on vigilance, the exact class this repo has spent eight cage-match rounds refusing to rest on.

- **§3 marker [C] / B5 [E3] — "Present(N) absent ⇒ no gateway write was admitted" is not conserved by construction.** The append happens after `verify_schema()` and before `bus.start()`, but nothing makes the append *fail-closed*. A full or read-only `/data` (A2's disk check runs before the pull, before the backup, and long before N's run) lets the append fail while the gateway serves; B5 then reads absence, B5a restores, and N's writes are silently rewound. Round 3 fixed one cause of false absence (container-id mismatch) and left the class.

- **§2 [A] + B4 [C] — the quantity that attributes health to a release is not the quantity that identifies the release.** The sampler attributes each sample to "the digest the island reports (`/health` `build`)"; `build` is build provenance written by the Dockerfile (`git_sha`/`ref`/`built_at`/`aiko_*_ref`) — there is no digest in it. B4's Healthy requires `build = X` where X is a resolved channel **digest**. So either B4's success predicate is unsatisfiable, or per-digest memory, `Eligible(R)` and `holds.json` are keyed on a value that cannot distinguish a rebuild of the same commit from the release it replaces. Two identifiers used as one. Related missing cell: the unreachable-sample rule ("counts red only if the next sample is also unreachable") never says *which* digest an island that reports nothing is red against — so a T or a 20-minute cutover holds the wrong release, or none.

- **§1 [E3]/[E4] — the harness does not exercise the boot the cells lean on, and the bundled code contradicts the stated predicate.** E3/E4 run `python -m aiko_gateway.migrate` and `verify_schema` as two *separate* `docker run` one-shots: never `entrypoint.sh`, never uvicorn, never the lifespan. "The previous image REFUSES the forward db" is proven for two functions in isolation, not for "a container of P cannot serve" — which is what B5's "refusal is certain" and B6 rely on. And the bundled `migrate.py` emits `MIGRATE_SKIP_UNKNOWN_REVISION` and **serves** on unknown revisions; the refusal lives in `db._assert_at_head`. §1's "one predicate decides both boot layers" is not in the bundle, and "an older image NEVER accepts a newer schema [E3]" is a universal claim with a one-pair witness riding on an unnamed second mechanism. (A2's drift cell is likewise [A]; the script backing it is header-only here.)

**What holds:**

- The compat guard's *effect*: with `db.py` finally in the bundle, E3 + E4 + E5 and the must-fail arm do establish that a forward-migrated volume cannot be served by 0.16.1 and that restoring the set recovers admission. The must-fail arm is a real positive control, not decoration.
- The state-table frame itself. Every step × {success, failure} with an entry invariant is the right instrument; it is the only reason these flaws are nameable at all. Keep it.
- The round-3 folds that are genuinely conserved: backup before any live-set mutation (B2 before B3), append-don't-match on the marker, per-digest reds, `aiko_connected` in Healthy, lease conditional on Healthy (the lease now measures the island, not the watcher), `holds.json` fail-closed, syd's staggered window, `concurrency:` on the CI writer, the manifest as a checked file.
- The one-shot route's premise is real: the image has no `ENTRYPOINT`, only `CMD ["./entrypoint.sh"]`, so a command override skips migration ([E1] and the Dockerfile agree), and `worker_guard` keeps the single-writer premise true.
- Scoping honesty: `:stable`/`:latest` split, `AUTO_UPDATE=off` default, push-CD rejected, promotion is a human act, healthy-but-wrong named as a tradeoff.

**If RECAST, what to fold back:**

- Make T absorbing: A0 reads the persisted state; `host-failed`/`quarantine` ⇒ exit without touching the stack, the DB, or any artifact. Clearing is an explicit human act. Add the cell.
- Name the artifact by provenance, not by digest: `pre-<P>-<N>-<utc>.db`, never overwritten, or reuse `update.sh`'s host-side timestamped snapshot as the single rollback artifact. State the system-wide invariant as "a snapshot whose stamp is head(P) exists", and check it on entry to B2.
- Add A2's validation: `stamp == head(P)` (read head(P) from the running image's scripts). If it fails, there is no P — go straight to T rather than to a B6 that cannot succeed.
- Make the marker append fail-closed: any error before `bus.start()` aborts the boot. Then absence ⇒ no admitted write is conserved by construction.
- Make the lock real: the watcher refuses to run unless the box's `update.sh` is the version that takes the lock (fail-closed, like the drift guard's "absent ⇒ die"), and route the runbook's fix-forward step through it.
- Name one release identity and put it everywhere: report it at `/health`, key the sampler, `holds.json`, `Eligible(R)` and B4's predicate on that same value.
- Define the no-reported-digest attribution rule.
- Add a container-boot arm to the harness (`entrypoint.sh` + lifespan), and write the real predicate (`db._assert_at_head`) into §1 instead of a `schema_status()` the bundle does not contain.

---

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** The schema evidence holds, but the watcher lacks a recovery protocol for interrupted execution and a usable definition of digest identity. Fold back to a durable deployment transaction with explicit admission and recovery boundaries. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. Here, the wasted work is a growing state table whose state disappears when its shell dies. Dijkstra: "Simplicity is prerequisite for reliability."

**Fatal flaws:**
- [missing cell] [A] — There is no recovery entry for watcher death, reboot, or timeout between cells. After B3, an interruption can leave the stack down; after B4, N may have migrated and served while image.env already names N. The next timer starts at A0, with no specified way to recover the original P, checked backup, or transaction phase. Likewise, nothing on A0–A1 consults the terminal host-failed state before attempting another deployment. A shell lock serializes surviving processes; it does not preserve deployment history. Persist the transaction before destructive transitions and make recovery the first entry point, including an explicit terminal-state latch.
- [B3–B5] [A] — Present(N) treats an absent marker as proof that no gateway write was admitted, but the design specifies APPEND without durability ordering or a failure policy. A marker write that fails must prevent bus.start(); a marker that remains only in filesystem cache can disappear after a host failure while later database commits survive. That makes B5a silently rewind acknowledged writes. The second law does not grant reversibility merely because a log line disappeared. Specify a durable marker before admitting writes, durable clearing before starting N, and treat unreadable or uncertain marker state as terminal. The supplied harness never starts the gateway or exercises this boundary.
- [missing cell] [A] — Registry digest identity is not connected to the identity the application actually reports. Healthy(X), per-digest sampling, and Present(N) require a digest, but the bundled Dockerfile bakes git_sha, ref, build time, and dependency refs; it does not bake a registry digest. The lifespan excerpt also has no proposed marker implementation. Comparing build directly with N therefore has no defined successful outcome. A healthy migrating release could serve, fail the identity test, and enter T. Define the mapping among channel manifest digest, platform image, running container, application provenance, and marker identity. Use Docker's inspected image identity for artifact verification and explicitly map health provenance to it.
- [A1/B6] [A] — QUARANTINE(N) has no specified enforcement path. A1 reads shared holds, but never reads a local quarantine; shared holds record melb rejection and post-promotion reds, rather than every follower's failed installation. A stable follower that restores P successfully can therefore attempt the same N every poll, repeatedly stopping service and restoring its database. The off-box lease can remain satisfied between these planned outages. Persist local rejection before finishing B6 and exclude that digest in A1 until an explicit clearance or a different candidate arrives.

**What holds:**
- E1–E5 substantiate the published-image experiment: command override avoids migration, the new image admits its migrated database, the previous image refuses it, and restoration enables the previous image again. The no-op restore control meaningfully detects a missing restore.
- Backup before marker deletion removes the previous round's direct data-loss path.
- Refusing automatic rewind after a migrating release has admitted gateway writes is sound. The terminal arm honestly acknowledges an irreversible transition.
- Conditional lease renewal and checking aiko_connected address concrete failures omitted by a simple HTTP health check.
- Human promotion until an exercise signal exists is an honest limit on what elapsed soak time proves.

**If RECAST, what to fold back:**
- Keep the compatibility guard and restore primitive. Implement one durable deployment record containing phase, N, P, backup identity, admission status, and local rejection; consult it before resolving another channel candidate.
- Make admission evidence durable and fail closed. Exercise marker failure and interruption before and after admission, checking preservation of actual database writes.
- Resolve artifact identity once through Docker inspection and carry that identity through health validation, markers, sampling, and holds.
- Execute interruption and quarantine scenarios against the resulting watcher. The drift-preflight body is omitted from this bundle, so its digest-to-source binding remains unassessed.


---

The brief was cut before the rest of the state table and the code it cites. I'll read only that remainder, then strike the design.## Tesla, the Arc-Prophet's Design Strike

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Verdict:** RECAST

**Summary:** The table has no rest note, and it tunes the watcher to a digest while the image only sings a git sha, so the second tick of the timer — and every "successful" boot after it — shakes the canary down.

**Fatal flaws:**
- missing cell [A] — The steady state is not in the table. A1 success falls through to A2, and A2 success falls through to B1 `compose down`, with no arm for "the running image is already N and Healthy." The timer's own period is therefore a cutover. Once `AUTO_UPDATE` is on, every poll downs `chat.enspyr.co` (mosquitto, registrar, chat, and the gateway together), waits out `AUTO_UPDATE_BUDGET`, and only then discovers it had nothing to do. The lease renewed at B1 hides the cut from the dead-man for exactly one grace, so the page arrives after the phones have already been quiet. §3's table is the whole mechanism; this arm is the one the table does not contain.
- B4 [A] — Two names are being asked to be one frequency. N is a registry digest (`PROMOTE_NOW=<digest>`, holds keyed by digest, `Eligible(R)` a digest). The only identity the process can actually report is `/health`'s `build`, and the Dockerfile writes that file as `git_sha`, `ref`, `built_at`, and the two aiko refs — no digest — and says an env-fed value would be the host's belief, the #2301 failure. `Healthy(X)` requires `build` = X. A digest never equals that JSON, so B4's success arm is unreachable on the artifacts in this bundle. The marker is told to append `<digest>` from inside the gateway, which does not know its digest unless the watcher injects one. Injected, `.served` repeats the host's belief: B4 then fails the body check, B5 sees `present ∧ migrates`, and the canary goes to **T** while the volume may still be a schema P could serve. Not injected, `Present(N)` is absence, B5a restores a good boot, and every release false-quarantines. E2 only prints `ADMITTED` from `verify_schema()`; the evidence file contains no `/health` body, so the "body observed live" half of B4 was never what the harness exercised.
- B4 / bootstrap 2 [A] — The actuator socket is still a tag. Bootstrap 2 writes `image: ${ISLAND_IMAGE:?}`, which expands through a colon into a tag. A digest pin is a different socket, `repo@sha256:…`. Writing `sha256:…` into the tag slot makes compose ask the registry for a tag of that name. `image.env` is never named as a file compose loads (compose interpolates `.env`, or `--env-file`); `update.sh` today pulls `${ISLAND_VERSION:-edge}` and does not read `image.env`. A2's drift preflight, as `update.sh` calls it, rewrites a numeric version into a `v`-tag and dies on exit 2 when the ref cannot be fetched. A digest is not that shape. The preflight body is header-only in this bundle; the caller visible in `update.sh` is enough. Net: either every candidate fails closed in A2 and never updates, or a "fix" skips the drift check and a migrating image crash-loops into B5 → **T**. The 2026-09-11 outage was this same seam, one line wide.
- B5 [A] — `present ∧ migrates → T` uses E3 for a fact E3 does prove (0.16.1 prints `Refusing` on a db at `0028` and will not admit) and then takes a step E3 does not license. The marker is written after `verify_schema()` and before `bus.start()` (`main.py` lifespan, as specified). So `Present(N)` becomes true while `aiko_connected` is still false, with mosquitto freshly created by a `depends_on` that does not wait for health. Any miss of `Healthy` after that line — bus slow, broker not up, a single probe gap — on a release whose head differed from the stamp at A2, skips B6 and stays down. There is no "start N again" cell. The runbook offers only a newer release, or restore `pre-N.db` and drop writes since the first `.served` line for N. At 3am the operator follows the runbook and lengthens an outage whose schema change already succeeded. E3 shows P will refuse; it does not show N will.
- §2 [A] — Syd's two-hour lag is a sentence, not a cell. Both islands "follow `:latest`" and each watcher resolves the digest on its own tick; `holds.json` is written only once melb has failed to be on R after the window closes, which is after syd has already had a chance to pull the same digest. A release that drives melb into **T** is still eligible on syd's clock for that overlap. Unreachable-twice is declared red and then has no digest to hang on: `/health` never came back, so the "red counts only against the digest the island reports" rule attaches the red to nothing, or — if an implementer hangs it on the last known name — to P, and the known-good digest takes the stain. That is round 3's freeze, transposed up a harmonic.
- B5a [E] — The cell says restore `aiko.db`, `-wal`, and `-shm` from `pre-N.db`. The harness does not do that. `BACKUP` writes one file; `RESTORE` is `rm -f` of the three live paths and `cp` of that one file; `db.py` leaves WAL off on purpose, so the sidecars are usually absent. A literal reading of the cell (`cp` the sidecars, `set -e`) fails after the `rm` has already unlinked the live db, and the failure arm is **T**. E5's must-fail arm proves that a no-op restore leaves the old image refusing; it never proves a three-file restore exists to succeed. The checked backup saves the data. It does not save the night.

**What holds:**
- The shipped guard, as measured, holds for this one hop. E3: 0.16.1 exits on `Refusing` against head `0028`. E4: 0.17.0 exits on `MIGRATE_REFUSE_UNKNOWN_REVISION` for a stamp it has never seen. E5: putting the pre-migration file back makes 0.16.1 print `ADMITTED`. The must-fail arm goes red when restore is a no-op. That consequence — do not boot P onto a db N has migrated — is real, and the bundled `db.py` `_assert_at_head` is the refusal the old "serves anyway" premise missed. (The bundled `migrate.py` still skip-serves under `MIGRATE_SKIP_UNKNOWN_REVISION` and returns; the refuse string lives in the 0.17.0 image, not in that source. Trust the evidence file for the image, not the docstring.)
- B2 before B3 keeps the round-3 data-loss order out of the table: a checked `pre-N.db` exists before `.served` is truncated, and a failed backup returns to P on the untouched set.
- `Present` is "a line for this digest," so a new container id is not absence. E1 shows a CMD override on this image does not migrate, which matches `Dockerfile` (`CMD`, no `ENTRYPOINT`). `compose down` rather than `stop` is the right call under `restart: always`. The lease pinging only while `Healthy`, and going silent in **T**, is the pulse that actually tracks the island.
- Pull, not push. Per-digest reds, aimed at a digest the island is serving, are the right shape — once something on the box can say that digest.

**If RECAST, what to fold back:**
- Add the rest cell, in front of A2: if `docker inspect` says the running island image is already N and `Healthy(N)` holds, renew the lease and exit. B1 is unreachable from that cell. The common hour must not be a cutover.
- Pick one identity and thread it through every socket. Host-side digest from `docker inspect` is the one the registry and the holds already use. Compose becomes `image: ghcr.io/nickmeinhold/aiko-chat-island@sha256:…` (an `@` pin, full ref in the env file compose actually loads). The marker stays a confession the process can make without knowing a digest: after `verify_schema()`, append a nonce the watcher wrote onto the volume before `up`, plus container id and time. `Healthy` compares inspect's digest to N, and the body to `aiko_connected`. Stop asking `build` to be a digest; today it is a git sha, and it should stay the artifact's own name.
- Split B5. `present ∧ migrates ∧ the process admitted ∧ the miss was only the bus` → one retry of N, same image, lease already held, before anyone is allowed to enter **T**. **T** remains the arm where N itself will not stay up and P is known to refuse. The runbook's first line becomes "start N again when the stamp is already N's head"; restore stays the arm that throws away writes.
- Make syd's lag a cell: syd does not resolve a digest until the holds fetch says melb has been serving it for two hours. A double-unreachable sample is attributed to N only while the watcher is inside B4–B6; otherwise it is not a red. A red with no digest is not a red.
- Rewrite the B5a cell to the procedure E5 actually ran: delete `aiko.db`, `-wal`, and `-shm`, then copy the single `pre-N.db` into place. Do not name sidecar files the backup never creates.


---



# v4 series, round 2 (striking v5)

**Overall verdict: RECAST, 3/3** (zero DISSOLVE). Continue: flaws name missing cells (B5 resume, an exit from T). One round left in the series (cap 3).

## Per-family verdicts (v4 r2)
| Family | Verdict | One line |
|---|---|---|
| Noether (DeepSeek) | RECAST | No exit from T (it livelocks back into the same N); the restore point's storage is never named |
| Carnot (GPT) | RECAST | R's "idempotent restore" rewinds writes once P has served; B5 has no resume arm |
| Tesla (Grok) | RECAST | A reboot during the health wait is the ordinary input, and R tunes recovery to the wrong frequency |

## Fatal flaws (deduped, most severe first)
1. **R mutates data after an image may have served (Carnot, Tesla). CREATED BY FOLD 3.** "Redo B5a" from phase B6 overwrites writes P admitted during its health wait, and from a non-migrating B6 it destroys N's preserved writes. B4 resumed via B5 quarantines a release that reached Healthy. B5 has no resume arm. → **v6: R observes, never mutates.** It reads the world: Healthy(N) ⇒ CURRENT, Healthy(P) after phase B6 ⇒ QUARANTINE, anything else ⇒ T with reason `interrupted@<phase>`. Data moves only inside one uninterrupted run. A subtraction: R's dispatch map goes.
2. **No exit from T (Noether, Tesla).** T doesn't quarantine and records no reason. Clearing `host_failed` leaves `txn` and the channel on N, so the next poll replays the same N. → T records its reason. The one clearing command (`auto-update clear`) quarantines N by default (`--retry` to opt out) and clears `txn` with it.
3. **The empty-island hazard is guarded in five cells, not removed (Tesla, Noether, converging independently).** And B5a's failure → T leaves an unlinked db beside the backup, where a hand `docker compose up` serves an empty island. → **A volume sentinel in the image:** the first migration of a fresh volume writes `/data/.initialized`; `migrate.run()` refuses to create a db when the sentinel exists and `aiko.db` is missing. One door, every actuator (watcher, `update.sh`, a 3am hand command). Five checks go.
4. **The restore point's storage is unnamed (Noether, Tesla).** E1/E5 use a host mount (`/bk`); `compose run --rm` keeps nothing; "disk ≥ 2× db" implies the volume. → `pre-<txn>.db` lives on the HOST under `deploy/.auto-update/`, bind-mounted into the one-shot. The harness already does exactly this, so the cell names the path it tested.
5. **The actuator has a writer and no reader (Noether, Tesla).** `update.sh` runs `compose -f docker-compose.yml` with no `--env-file image.env`, and three services interpolate the image key. → One `COMPOSE` invocation, `--env-file .env --env-file image.env`, shared by the watcher and `update.sh` (bootstrap 3). The compose change covers all three services. [A] until built.
6. **The lease deadline is an unstated number (Noether).** Nothing pings while the stack is down. → Grace = 2 × budget + restore + 10 min, stated. B1 renews; a stuck update pages after grace.
7. **E6 overclaimed (Tesla).** `build-info.json` has `git_sha`/`ref`, NO alembic head, yet A2 cited E6 for `head(N)`, the bit that picks rewind vs B4′. → **E7:** read `head(N)` from N's own script directory in a one-shot; add it to the harness.
8. **Rebuild ≠ release (Carnot).** apt/pip resolve at build time, so two digests of one sha can differ. → Install decisions and quarantine key on **digest**. `sha` only checks that `/health` is running the digest the watcher installed.
9. **The marker sits after other lifespan work (Tesla).** `warn_if_unreachable` and the channel worker start between `verify_schema()` and `bus.start()`. → Write the marker immediately after `verify_schema()`.
10. **A GC'd rollback target (Noether).** A2 can't fetch P ⇒ "retry forever", silently. → No rollback target ⇒ refuse to update AND notify. Tie the `ghcr-retention` workflow to keep every digest some island has as `current` or `previous`.

**Not folded:** "the production island is the canary" (Noether) restates Nick's ruling of 2026-10-10, already marked deliberate in the design.

## Trend and the evidence check
- r1's worst flaw: a dropped steady state (every poll a cutover). r2's worst: the recovery cell rewinds served writes, **created by r1's fold 3**. Severity is not falling, but its LOCATION has converged: both rounds' worst live in recovery/re-entry, and r2's two best remedies are subtractions (R observes; a sentinel replaces five guards). That is the shape of convergence the old series never showed.
- [A]-cell share: 13 of 16 tagged findings = **81%** (r1: 71%). All three [E] strikes found an [E] cell citing evidence for something the harness doesn't do (E6 → head; E1/E5 → storage). **Mechanism:** each [E] tag must name the harness LINE that proves it, not just the step.

## Disposition
v6 folds 1–10. It is the last round of this series. If r3 still finds a missing cell, stop, and build in this order: (a) the image-side sentinel and the marker (useful with or without a watcher), (b) the journal and R, then strike as code.
**Struck:** dt-1792407464, 2026-10-19, against v5 `44b2eba`. Bundle 113 KB, code from `origin/main` (bundler asserted `def schema_status`).

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** The new recovery cell can erase admitted writes: rollback is not reversible once either image serves. Three folds remain necessary. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. Dijkstra: "Simplicity is prerequisite for reliability."

**Fatal flaws:**
- [R] [A] — Recovery from B6 unconditionally restores the pre-update backup. But B6 is also reached when N served without migrating, where the normal path deliberately preserves N's writes. A watcher death during B6 therefore converts a preserving rollback into destructive restoration. Even the absent-marker path is unsafe: P can begin serving during B6's health wait; restarting the watcher then restores again and deletes P's newly admitted writes. Idempotent copying is not idempotent recovery once serving resumes.
- [R] [A] — There is no dispatch for txn.phase = B5, although every B cell records its phase on entry. Death after entering B5, including after stopping N but before reading its marker, leaves a durable transaction R cannot resume. Treating this as T turns an ordinary watcher interruption into an operator-required outage. The new recovery fold still omits a reachable state.
- [§2] [A] — 'A rebuild of one commit is one release' assumes artifact equivalence that the supplied Dockerfile contradicts: apt-get and pip resolve external dependencies during rebuilding. Pinning the base digest does not freeze those inputs. Two digests with the same git_sha can behave differently, yet share promotion evidence, quarantine, holds and Healthy identity. A follower can install an untested rebuild under a previously approved SHA, while A1 can also suppress installation of a corrected rebuild. E6 proves metadata extraction for one artifact; it does not prove this equivalence.

**What holds:**
- The supplied evidence supports stopped-stack backup, forward migration, old-image refusal at the real CMD boundary, and restoration for the tested image pair.
- The steady-state arm, enforced update.sh lock, terminal latch and transaction-specific backup address substantial round-one failures.
- Conservative marker presence and refusal to start an older image on an unknown schema are sound safety choices.
- The workspace contains no harness or repository checkout, so I could not perform the required origin/main rerun or independently verify the ledger diff. This strike uses the supplied bundle.

**If RECAST, what to fold back:**
- Split restoration from starting P. Persist a restore-complete state before any P startup, then recover that startup by preserving the live database. Never infer permission to restore merely from phase B6; preserve the distinction between rollback with and without restoration.
- Make R exhaustive over every durable phase, including B5. Exercise watcher death before and after each destructive action and after both N and P begin accepting writes; assert that admitted writes survive every preserving path.
- Use digest as release identity throughout promotion, holds and quarantine. Keep git_sha as provenance. Connect Healthy and marker identity to the selected artifact through container inspection or an immutable artifact identifier, removing the need to pretend rebuilds are equivalent.


---

The strike brief is truncated at the drift preflight. I'll read the remainder of the bundle before I hunt the resonant frequency.## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration." The journal stores the cutover's energy and then tunes recovery to the wrong frequency: a reboot during the health-wait, the one input a timer actually receives, rewinds a good release and, on the rollback arm, copies the backup over a database that has already served.

**Fatal flaws:**
- **R** [A], missing resume for phase **B5** and for a serving sub-phase of **B4** / **B6** — Phase is stamped on entry, and CURRENT / QUARANTINE are written only after the ≤600 s wait (§4 table). Watcher death inside that window is the ordinary vibration (reboot, OOM, the page itself). R maps B4 and B4′ onto B5. On a code-only release the marker is already present and `migrates` is false, so B5 falls into B6 and quarantines a sha that had reached Healthy. R maps B5a and B6 onto "redo B5a, the backup is the source" while P is already serving, so the copy replaces writes admitted after the stack came back. Phase B5 has no arm. B4′ reaches T only on the in-process failure arrow, so a migrating release flutters B5→B4′ on every boot and the latch never sets. Fold 3 built this map; "idempotent" is true only while the phase has not served.
- **A0** [A] / **T** [A] — A0 honours `host_failed` and returns before it looks at `txn`. The §4 runbook clears `host_failed` and leaves `txn` on disk, so the next poll drops into R and plays the same N again. B4′ fails into T with no quarantine row, and A1 therefore has nothing to steady against. The lease is silent for the whole 600 s plus the second budget (§4 Lease); the page is that silence. At 3 am the reflex is to clear the latch, and that blow is what strikes the oscillator again.
- **B5a** [E] / **T** [A] / **missing cell** (entrypoint, beside §1) — B5a unlinks `aiko.db` and then copies. The failure arrow enters T, whose entry invariant says nothing was deleted after B2, and A0's latch then refuses the redo, so the backup sits unused beside a hole. The shipped entrypoint (`entrypoint.sh` then `migrate.py`'s fresh-db path) builds a database on an empty volume and serves it. The harness's setup calls `python -m aiko_gateway.migrate`; E5's must-fail arm prints `RESTORED` and never unlinks. Fold 9 sized a hand `docker compose` as a lock tradeoff. After this unlink, that same command is a silent empty island, and the phones ring there. The refusal has to live in the image: a tombstone written at B2, and `migrate.run()` raises when the tombstone is present and `aiko.db` is missing or fails `integrity_check`.
- **Marker** [A], against **main.py lifespan** [C] — The append is specified after `verify_schema()` and before `bus.start()`. The shipped lifespan already opens `warn_if_unreachable`'s session and starts `_channel_worker` between those two calls (`main.py` excerpt, the block above `state.bus.start()`). A write on that span, or any write a later edit drops there, is admitted while `.served` still reads absent, and B5a treats absence as "restore the backup."
- **B4** [A] / bootstrap (2) [A] — `ISLAND_IMAGE` sits in a second env file. Shipped `update.sh` is the scar of this class: the shell environment beats `.env`, a stray compose file wins, and the 2026-09-11 outage was one line the container never received. Compose auto-loads `.env` and will not see `image.env` unless every actuator passes the same `--env-file` pair. The image key is interpolated on three services (`chat-island`, `registrar`, `chat`); one of them left on `${ISLAND_VERSION:-edge}` splits the mesh from the gateway. Fold 7 named the pin and left the file-set open.
- **A2** [E] / **B2** [E] — A2's evidence cell claims sha, ref, and head via E6. E6 cats `/app/build-info.json`, and the captured file carries `git_sha` and `ref` and no alembic head (`phase-b-evidence.out`). `migrates := head(N) ≠ stamp` is the bit B5 uses to choose rewind or B4′. E1's backup lands on the harness mount `-v "$BK:/bk"`. The design's one-shot is `compose run --rm`, which drops the container layer on exit, and the host path of `pre-<txn>.db` is never named, so R's "backup is the source" has no substrate.

**What holds:**
- §1's single predicate, `schema_status`, shared by both boot layers, with no serve-anyway. E2 admits HEAD, E3 / E3′ / E4 refuse, E5 puts back a database the previous image admits, and the must-fail arm goes red when the copy is a no-op. E3′ runs the real `CMD` and it exits without binding.
- A1's steady arm on sha equality, quarantine, and holds. A poll of the running release stays a poll. Digests pin, shas compare, and E6 binds a real published digest to `build-info.json` without booting the app.
- B1's `compose down` matches `restart: always` in the shipped compose. P is kept off a forward-migrated schema. Unreachable-twice pauses all promotion, and syd's A1 waits until melb reports Healthy on that sha.
- The marker's own rule: a failed append raises, and the process never reaches `bus.start()`.

**If RECAST, what to fold back:**
- R, in the §4 table: the first time Healthy(X) is true, fsync the commit (`current := X`, or the quarantine row) before the rest of the budget. Resume arms: B4/B4′ and Healthy(N) → CURRENT(N); B6 and Healthy(P) → write the quarantine and clear `txn`, leaving the db bytes alone; phase B5 → re-read the marker and branch; a phase whose container never started → B2's failure arm. A crash is a resume, and quarantine stays on the in-process failure arrow.
- T and A0: one journal word, cleared only by a rename that nulls `txn` in the same write. The runbook gains that sentence and loses the bare "clear `host_failed`." Page TTL sits longer than two full budgets, so a healthy wait cannot train the reflex.
- B5a: `pre-<id>.db` lives on the host at `deploy/.auto-update/`, restored by temp file, fsync, rename. New entrypoint cell: tombstone present and `aiko.db` missing or dirty → `migrate.run()` raises and uvicorn is not exec'd. Hand compose stays a named tradeoff only as "refuses to boot."
- Marker sentence: the append is the next statement after `verify_schema()` returns, above `warn_if_unreachable` and the channel worker.
- Bootstrap: one `deploy/compose.sh` that pins `-f docker-compose.yml --env-file .env --env-file image.env`, called by `update.sh`, the watcher, and the runbook. All three service image keys become `${ISLAND_IMAGE:?}`.
- A2: `head(image)` is `ScriptDirectory.get_current_head()` through `docker run --entrypoint`, migrator not invoked, and that string is what the txn stores as `stamp`. Take E6's name off `head`. Check disk before the pull.


---



## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The design conserves two things it never proves — that the *restore point* survives to the moment T needs it, and that there is a *path out of T* — and both laws break on paths the table itself schedules (B5a's delete, and the clear-host_failed re-entry).

**Fatal flaws:**

- **Missing cell: no exit from `host_failed` — T [A] + A0 [A] + T-runbook.** T latches `host_failed` and records *no reason*; A0 on the next run reads it and exits without pinging. Nothing in the table defines the transition out. The runbook's two options both route back into the same transaction: "fix forward with a newer release" silently assumes the channel has already moved (never stated as a precondition), and "restore `pre-<txn>.db` … Then clear `host_failed`" leaves the channel pointing at the *same* N — because T deliberately does **not** quarantine (T's own action column: "no quarantine"). So A0→A1→A2→…→T repeats. That is a livelock, not a recovery: the only escape is a human who knows to edit the channel, which no cell or runbook line tells them to do. The design's own stop-condition ("continue while one names a missing cell") is met here.

- **T's entry invariant "nothing deleted after B2" is false — B5a [E5], T [A].** B5a *deletes* `aiko.db` ("one-shot: delete `aiko.db` and any stray sidecars… copy `pre-<txn>.db`"). On the B5a-failure path to T, the live DB is gone and the only restore point is `pre-<txn>.db`. The design never says *where* that file lives: A2's "disk ≥ 2× db" implies the same disk as the DB (the volume), while the E5 harness restores from `/bk` (a host mount). So E5 proves the restore *mechanism* and not the design's *storage*: if the backup is on `aiko_data`, the very "silently reborn empty" scenario the empty-island invariant exists to prevent also takes the backup. T's "restore `pre-<txn>.db`" is an [A] guarantee resting on an [E]-tested mechanism that does not exercise the state T actually reaches.

- **The lease is not conserved across the down-window — B1 [A], §4 lease, A0 [A].** "ping iff Healthy(current), on every run" means B2–B6 (stack deliberately down, `Healthy` false by construction) never ping. B1's single "renew lease" must cover B4's ≤600 s wait **plus** B4′'s second budget **plus** B5a **plus** B6. No cell fixes the deadline, so the conservation of "alive" depends on an unstated number: too short and every update trips the deadman's switch, too long and a genuinely stuck update never pages. The symmetry "down ⇒ no ping" has no compensating conserved quantity.

- **The actuator has a writer but no specified reader — B4 [A], bootstrap 2–3, update.sh [C].** Fold 7 named the socket (`image.env`: `ISLAND_IMAGE=<repo>@<digest>`, compose `image: ${ISLAND_IMAGE:?}`) but not the wire. The shipped `update.sh` builds `COMPOSE="$DOCKER compose -f docker-compose.yml"` — **no `--env-file image.env`** — and the watcher's own `up -d` is never specified to pass it either. So the two writers that share the A0 flock either die on `:?` (ISLAND_IMAGE unset in shell/.env) or render a *different* image than the one they wrote. The lock conserves mutual exclusion of the writers while the deployed image can diverge from both.

- **The rollback target's availability is assumed — A2 [A].** A2 "re-pull[s] P by digest if pruned", but there is no arm for "the registry no longer serves P's digest" (GHCR GC, deleted tag). A2's failure arm is "retry next poll, never quarantine", so a GC'd rollback target makes the island silently stop updating *forever*. The design conserves "never quarantine a pull failure" but not "always have a rollback target".

- **Secondary — the empty-island coupling is guarded, not removed — B1-fail/B2-fail/B4/B4′/B6 [A].** The entrypoint creates a fresh DB on a missing `aiko.db`; the design guards this in *five* `up -d` cells. A volume sentinel (`.initialized`) that the entrypoint refuses to bypass when `aiko.db` is absent removes the coupling instead of re-checking it five times.

- **Secondary — blast radius: the production island is the canary — header/§3 [A].** "enspyr-melb (where phones ring) is the canary" and "syd follows melb, not the tag". The island that matters takes the release *first*; the test island follows. Either the prose is inverted or the blast radius is under-counted for the one island whose availability is the point.

**What holds:**
- §1's compat guard: one predicate (`schema_status`) shared by both boot layers, no override — the symmetry is real and the refusal is exercised at the container boundary (E3′, E4).
- Identity by `sha`, pull by digest (§2, E6): fold 2 holds; `/health` never needs to report a digest.
- The journal discipline: tmp + fsync + rename, read first at A0, phase rewritten on every B entry — the durable-transaction fold holds, and R's named resumes (B1–B3, B4/B4′, B5a/B6) are idempotent.
- A1's steady-state arm: every poll no longer cuts over (fold 1 holds).
- B5a's restore mechanism itself: the must-fail arm (no-op RESTORE ⇒ E5 red) shows the harness can fail, which is the right property even though it does not exercise the design's storage.

**If RECAST, what to fold back:**
- **Add the missing cell.** A `T`-exit cell: on human clear, A0 must (a) re-read the failing sha/phase from T's record and (b) either quarantine N (if T was reached from B4′/B5-present) or require the channel to have moved. Record the failing sha and phase *in T*, so the runbook's "restore … then clear" cannot re-enter the same N. Change T's invariant from "nothing deleted after B2" to "a verified restore point exists" and state *where* `pre-<txn>.db` lives (host bind mount, not the data volume).
- **Conserve the lease explicitly.** Either state the deadline bound that covers B4+B4′+B5a+B6 and renew in B2/B4/B4′/B6, or drop the pre-renewal fiction and make the page threshold a named constant.
- **Complete the actuator wire.** State `--env-file image.env` on *every* compose invocation in both the watcher and `update.sh`, and make `image.env`'s write atomic (tmp + fsync + rename) like the journal.
- **Add an A2 arm** for "P not obtainable from the registry": fail-closed with a page, not an infinite retry.
- **Remove the empty-island coupling** with a volume sentinel rather than guarding it in five cells.
- **Fix the promotion order** (or the prose): the expendable island goes first, or the header must say why the production island is the canary.

---



# v4 series, round 3 (striking v6, the LAST)

**Overall verdict: RECAST, 3/3** (zero DISSOLVE). **Series CLOSED at its cap.** Per the round policy written into v6 before this strike: a missing cell was named, so **stop designing and build**.

## Per-family verdicts (v4 r3)
| Family | Verdict | One line |
|---|---|---|
| Noether (DeepSeek) | RECAST | Release identity isn't conserved: the sentinel is a birth certificate, R lost its resume, and the channel can move backward |
| Carnot (GPT) | RECAST | The sentinel loses two branches, and observation by sha can't establish digest identity |
| Tesla (Grok) | RECAST | The observer is still tuned to `git_sha`, and the sentinel is silent on every island that already holds a life |

## Converged findings (all three, independently)
1. **Decide by digest, observe by digest.** v6 keys installs on the digest but checks Healthy by `git_sha`. Two builds of one commit share a sha, so R can record CURRENT(N) while `restart: always` has P running. → Healthy and R read the running container's image (`docker inspect` → image id → the pulled digest) for all three services. `/health`'s sha is a cross-check only.
2. **The sentinel misses the volumes that matter.** It's written only when a db is CREATED, so melb and syd never get one. And a check placed after `run()`'s first connect sees a file SQLite just created. (Carnot: B2's `sqlite3.connect('/data/aiko.db')` creates a missing source too; `update.sh` survives only because of its size check, which B2 dropped.) → **Adopt** the sentinel whenever the migrator opens an existing managed db. Check before ANY connect (`os.path.exists`). Open backups `mode=ro`. Older images (P) don't honour the sentinel, so it is a floor: keep an actuator-side "db exists" check until every permitted image carries it.

## Further findings
3. **R lost its in-flight resume (Noether).** With no budget, a reboot mid-health-wait goes to T, whose `compose down` kills a stack that was about to be healthy. → R re-observes the in-flight phase for the remaining budget, still never moving data.
4. **The channel isn't monotonic at the island (Noether).** A re-pushed tag, a mistyped `PROMOTE_NOW`, or a revert used as a "fix forward" gets installed, or burned by quarantine. → A1 refuses N whose head is behind the stamp unless an explicit operator rollback is recorded.
5. **B5's discriminator (Noether).** No marker also means "N never started" (a Docker error). That shouldn't quarantine. B4′'s rationale (a slow bus) doesn't depend on `migrates`.
6. **`clear` leaves the island down (Tesla).** It quarantines, but steady never runs `up`. → `clear` starts `previous`.
7. **Actuator seams (Tesla).** The shell environment beats `--env-file`, so the watcher must `env -u` the drift-preflight seams as `update.sh` does. The `ISLAND_IMAGE` write must be journaled before `up`.
8. **`holds.json` unreadable (Tesla, Noether).** Fail-closed for that run: steady, no install.
9. **[E] overclaims persist, now VISIBLE (Noether, Tesla).** E:73-75 doesn't exercise the 600 s wait; E:76/82 don't exercise B5's branching. Line citations didn't stop overclaiming. They made every overclaim checkable, and reviewers checked.

## What held across the series
`schema_status` as one predicate (shipped, E-proven). The cold one-shot backup doesn't migrate. Backups on the host bind. `compose down`, not `stop`. **R never moves data** (Carnot: "eliminates the write-rewind flaw; keep that boundary"). Digest-keyed quarantine, a single `COMPOSE`, human promotion, a stated lease grace. The two subtractions are right in direction (Noether: "restore their lost branches, don't revert the folds").

## Series trend (v4 → v6)
| Round | Worst flaw | Origin | [A] share |
|---|---|---|---|
| r1 (v4) | every poll a cutover | rewrite dropped a v3 branch | 71% |
| r2 (v5) | R rewinds served writes | created by r1's fold | 81% |
| r3 (v6) | R blesses a release that isn't running; sentinel absent on live volumes | r2's folds **incomplete**, not destructive | ~67% |
Severity fell, from data loss (r2) to an identity lie plus a hazard still open (r3). And all three seats converged on the same two findings, which the old series never produced. The remaining flaws are about what the CODE observes, so prose can't settle them.

## Disposition: build, in this order
**(a) Image-side, useful without any watcher:** the volume sentinel with adoption and a pre-connect check; read-only backup opens; the `.served` marker (append + fsync after `verify_schema()`, fail-closed). One island PR, cage-matched (state-lifecycle). Its tests need the three arms: fresh volume admitted, existing db adopts, sentinel + missing db refused.
**(b) The watcher, as code**, observing container digests, with findings 3-8 as its acceptance tests, struck by `/cage-match`, not `/design-temper`.
**(c) Prerequisite for (b):** ct#5165, `ISLAND_VERSION` out of SOPS.
**Struck:** dt-1792412123, 2026-10-19, against v6 `4725818`. Bundle 113 KB, code from `origin/main`, harness line-numbered.

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** The subtractions are promising, but the sentinel loses two branches and SHA-based observation cannot establish digest identity. Fold back at database creation and artifact observation, then follow the last-round policy: build and strike the implementation. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are.

**Fatal flaws:**
- [B2] [E] — E:71 proves backup of an existing database, not refusal when the source is missing. The exercised BACKUP uses sqlite3.connect('/data/aiko.db'), which creates a missing source. Its empty backup passes integrity_check. With the five existence guards removed, B2 can recreate the missing file before the sentinel checks it; B4 then sees an existing empty database and can initialize an empty island. The sentinel has moved the guard downstream of another creator. Open the backup source with mode=ro, require the expected managed schema, and exercise missing-source refusal through the actual B2→B4 path.
- [§2 sentinel] [A] — The sentinel is written only when creating a fresh database, but both deployed islands already have databases. No adoption step establishes the sentinel on those volumes. Furthermore, rollback image P may predate the sentinel entirely: 0.17.0's bundled migrate.run() creates databases without consulting it. Thus 'every actuator' and the runbook's hand-up protection do not hold during rollout or rollback. Backfill the sentinel durably before enabling automation, and define the minimum rollback-image capability. Until every permitted image enforces it, retain protection at the shared actuator boundary.
- [R / §3 / §4] [A] — Digest identity was adopted for decisions but discarded for observation. Two different digests may share git_sha—the design explicitly admits rebuilds—and therefore Healthy(N) can be true while P is running. If the watcher dies after recording txn but before replacing P, R's first branch can commit CURRENT(N) without ever installing N. The sampler can likewise credit soak to the wrong digest, and syd can follow a melb release that melb never ran. E:91 only reads one image's metadata; it establishes no unique inverse from SHA to digest. Observe the actual container image identities for all three roles, reconcile them with the pinned artifact, and attribute samples only to that verified digest.

**What holds:**
- The published-image harness supports HEAD admission, unknown-revision refusal, and restoration across the tested migration. The no-op restore negative control is useful evidence.
- Removing automatic data restoration from interrupted recovery eliminates the previous write-rewind flaw. Keep that irreversibility boundary.
- Digest-keyed quarantine, host-held backups, explicit terminal clearing, and one Compose invocation are sound directions.
- Dijkstra: "Simplicity is prerequisite for reliability." A shared creation refusal can deliver that simplicity once every database opener and permitted image respects it.

**If RECAST, what to fold back:**
- Close B2's accidental database-creation path with a read-only source open and semantic validation. Prove that a missing source cannot become a valid empty island.
- Build the sentinel with an explicit existing-volume adoption path and tests covering legacy rollback images. Remove the five guards only after their replacement covers those branches.
- Separate release identity from descriptive build metadata: verify running container artifacts against N or P before recovery commits, follower admission, or soak attribution.
- Follow the stated round-three disposition: implement sentinel and marker first, then journal and watcher. Inject interruption around each durable transition. Another prose round would generate entropy without establishing reversibility.


---

The brief is truncated at the drift preflight. I'll read the rest of the bundle and strike from that, nothing else.## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** The observer you pared down to is still tuned to `git_sha`, a harmonic a rebuild does not share with its digest, and at 3am it will consecrate the wrong release while the sentinel you bought by deleting five guards stays silent on every island that already holds a life.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- §3 [E:91], §5 Healthy [A], R [A] — Line 91 cats one digest's `build-info.json` and greps `ref`. One note, one image. §3 then says a rebuild of one commit is a different release because apt/pip resolve anew, and in the next breath lets `/health`'s `git_sha` prove "the island runs what was installed." Fold 8 keyed quarantine on the digest and left the ear on the sha. R, now observe-only, listens to Healthy first: Healthy(N) ⇒ CURRENT(N), and Healthy compares `git_sha`. Two digests of one commit — the CVE rebuild, the ordinary future input, the reason the digest exists — sing the same sha, so Healthy(N) is true while `restart: always` has brought P back. R writes CURRENT(N). The journal's release and the process's bits diverge, and every later steady decision is conducted off that lie. Syd's "melb Healthy on N" and the sampler that attributes a sample to "the digest whose sha" inherit the sibling build's green. B4's success cell still says only "wait ≤ 600 s"; the predicate that would catch this lives in the paragraph above the table, where a builder will not look at 3am.
- §2 [A] — E:69 is `migrate_stamp` on an empty volume printing STAMP: the fresh-volume admission. It is cited as the empty-island hazard. The refusal has no line. The five "db exists?" guards are gone with the ledger. The birth certificate is written only "when it creates a db on an empty volume," so melb and syd — the only volumes that hold a life — never receive one. A missing `aiko.db` there is a fresh create, the empty island, the door those guards used to shut. Shipped `run()` reaches `_table_names()` → `create_async_engine` → connect before any new check, and SQLite creates the file on that connect, so a sentinel test placed in `run()` after the connect watches a file the check just conjured and admits the void. A daemon restart in that window starts the new emptiness under `restart: always`, R hears Healthy, and CURRENT blesses a blank island. The subtraction lost the branch it was invented to close.
- §5 `COMPOSE` [A], R [A], T [A] — Three clocks, and the one that starts the container is the one recovery never reads. The journal says who the release is; `image.env` says who boots; the shell beats both. The bundled `update.sh` records the measurement: an exported variable wins over `--env-file`. Fold 5's shared `COMPOSE` line adds the file and leaves the louder clock in the room. R's "phase ≤ B3 ⇒ `up -d` P" starts whatever `image.env` and the shell resolve. B4 writes `ISLAND_IMAGE := N` as its own commit, and the design never binds that write to the journal rename, so a death in the gap has phase still claiming "db untouched" while the actuator brings up N on a half-applied schema (the migrator docstring: SQLite DDL is not fully transactional). `auto-update clear`, fold 2's exit from T, quarantines N in the journal and drops the latch. It does not republish a digest and it does not start P. A1 then sees the quarantine and sits in steady, and steady does not `up`. The one command leaves the phone island down. The hand `docker compose up` that muscle memory reaches for bypasses the lock, reads `image.env`, and starts the quarantined digest — the sentinel is silent whenever `aiko.db` is present, so the bypass the design calls the operator's tradeoff un-quarantines N.
- A2 [E:94-95, E:91], B5 [E:76, E:82], A1 [E:91] — Lines 91 and 94–95 cat `build-info` and print script heads. They never run the drift preflight, never read an on-disk `version_num`, never meet a pruned P. The cell assigns `stamp = head(P)` and then refuses when `stamp ≠ head(P)`: either a tautology or a live-volume read the harness does not perform. That predicted `migrates` bit is still what B5 dispatches on (present ∧ migrates ⇒ B4′, absent ⇒ B5a). E:76 and E:82 show the old image's real CMD refusing a fully migrated db. A half-applied stamp is a different animal, and the arm that picks restore versus a second budget never observes it. The new caller of `preflight-compose-drift.sh` also skips the `env -u` of `ISLAND_REF_TREE`, `ISLAND_REPO_SLUG`, and `ISLAND_CODELOAD_BASE` that `update.sh` does before the same script. The guard, by that caller's own comment, only announces those seams for every other caller. The watcher is the other caller, and it is the long-running unit where a leaked slug lives: a clean comparison against a stranger's tree, then the digest installs. A1 wears this same [E:91] for channel, quarantine, holds, and melb. A `holds.json` fetch that fails into an empty set is unstated, and line 91 does not go near it. Fail-open there, and a held digest walks onto the canary during a GitHub blip.

**What holds:**
- `schema_status` is one predicate for both boot layers. E:75 admits HEAD, E:82 the previous image's real `CMD` exits without binding, E:87–88 restore then admit. Rollback of a fully migrated db has a known answer, and §1 is already on the islands.
- The cold one-shot backup does not migrate (E:71–72), and `pre-<txn>.db` lives on the host bind the harness already uses. `compose down`, not `stop`, so a daemon restart does not revive a removed container. Inside one uninterrupted run the backup-then-restore arm is the right shape, and R leaves the database bytes where that run put them.
- Promotion stays a human act, the canary ruling is Nick's, the lease grace covers two 600 s budgets plus restore plus 10 minutes, and `:stable` does not walk backward.

**If RECAST, what to fold back:**
- In §5 and R: Healthy(X) is the running container's image digest (`docker inspect` RepoDigests), and `git_sha` is only a witness that this digest's own `build-info.json` matches `/health`. Write that predicate in the B4 and B6 cells. Same ear for syd's melb probe and for soak memory: key the sample on the digest. A shared sha grants nothing.
- In §2: plant `.initialized` on every successful open of a real db, including the upgrade path, so the volumes that exist today receive it in the same image that enforces the refusal. The existence check happens before any engine connect. Tests are a refusal (sentinel present, `aiko.db` absent, file still absent after the refuse) and an admission of a genuinely fresh volume. E:69 stays the admission citation. The refusal gets its own line or stays [A] until it has one.
- One publication of the digest: the journal rename and `image.env` move as one commit. `COMPOSE` runs under `env -u ISLAND_IMAGE -u ISLAND_VERSION` and the three drift seams. R's start-P arm publishes P's digest before `up`. `auto-update clear` publishes P (N only with `--retry`) and the cell states whether the stack is started. Quarantine has to be true of the file the hand `up` will read, or the hand path remains a second actuator.
- Split A2's badge. Identity reads keep [E:94-95, E:91]. Preflight, on-disk stamp, disk, and manifest go back to [A] until a harness line runs them. `stamp` is the on-disk `version_num`; refuse the update when it differs from script `head(P)`; `migrates` is `head(N) ≠` that stamp; B5 re-reads the on-disk stamp after the down and dispatches on the read. The watcher invokes preflight with the same `env -u` strip `update.sh` uses. A1: holds unreadable, melb unreadable, channel unreadable ⇒ the existing failure arm, exit. An empty hold set is a value only a successful read may return.
- These four sentences are missing cells. The sentinel and the marker are worth building once the §2 sentences are in the doc; they are useful without a watcher. The journal and R want to be struck as code only after the digest-ear and the single publication are in the doc. Building R on `git_sha`, or the sentinel as currently sentence'd, ships this round's fold-created holes.


---



## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The conserved quantity here is *release identity* — one digest per install, one stamp per volume, one served-marker per cutover — and v6's two subtractions each fail to conserve it on a path they were added to close: the sentinel is written only where a db is *born*, and R was stripped of its resume as well as its data dispatch.

**Fatal flaws:**

- **§2 volume sentinel [A] — the fold is not a conservation law, it is a birth certificate.** `migrate.run()` writes `/data/.initialized` "when it creates a db on an empty volume." Every volume that already holds a db — *both live islands, enspyr-melb and enspyr-syd* — therefore never gets one, so the guard "refuses to create a db when the sentinel exists and `aiko.db` doesn't" cannot fire there. The five subtracted "db exists?" guards ran on exactly those volumes. Net: the r2 hazard (B5a's interrupted `rm`+`cp` leaves no `aiko.db`; a hand `up` builds a fresh schema, `schema_status` reads HEAD, the island serves empty) is *less* covered after the fold than before it. Compounding: no image published before the sentinel ships reads it, and §1's rollback path explicitly runs older images — so "one door" is a per-image door, i.e. a *floor*, in a design that prides itself on "no floors." Tests named are refusal + genuine-fresh-volume; the missing third arm is *existing db, no sentinel*. **Class: fold-created flaw.**
- **§5 Healthy(X) / §3 [A][C] — the probe keys on `sha` while every decision keys on `digest`.** Fold 8 established that a rebuild of one commit is a different release, so `sha → digest` is one-to-many. Healthy(X) is `git_sha` = X's, so it is satisfied by *any* digest with that sha: R's "Healthy(N) ⇒ CURRENT(N)" can record CURRENT(N) while P is the running image (and clobber `previous`); syd's "melb Healthy on N" gate can be satisfied by melb running a quarantined rebuild; B4's success arm can be satisfied by a container that is not N. The journal's `current` then names a digest the island is not running, and `ghcr-retention` keeps digests on that claim. **Class: proxy for the invariant.** Fix: verify the *running container's* image digest (`docker inspect` the container the txn created — the marker already records a container id, the txn does not), keep `/health`'s sha as a cross-check only.
- **§5 R [A] — the subtraction removed the resume, not just the data dispatch.** R evaluates one probe with no budget. A reboot during B4 or B6 revives the containers via `restart: always`, but the host-side watcher can start first; R then sees no Healthy X at phase ≥ B4 → **T**, whose action is `compose down` — killing the stack that was about to be healthy — and whose exit is not the claimed "one command" (`auto-update clear` clears the journal; A1's steady arm leases and starts nothing; the island stays down). The design already accepts R-resumes for phase ≤ B3; the lost branch is R re-observing the *in-flight* phase (B4's health wait, B5's marker read) for the same budget, never executing B5a/B6's data path. **Class: missing cell, created by the fold.**
- **§5 A1 [A] — the channel's monotonicity is a policy, not a guard, and the blast radius is every island.** Nothing in A1/A2 orders N against `current`. §4's "`:stable` never moves backward" is a promotion-side sentence; the island itself is defenseless. Paths: a re-pushed tag, a fat-fingered `PROMOTE_NOW` (a human is the actuator), and — worst — a *revert*, which is the runbook's own "fix forward." When head(N) ≠ the volume's stamp, §1 correctly refuses, and the table's only response is B5a restore + B6 **quarantine(N)**: the digest that was the fix is burned, and the island stays on the bad release. When head(N) == stamp, it silently downgrades to a different digest with no notification. One registry-side mistake → every island runs a backup/restore cycle. **Class: under-counted blast radius / missing ordering invariant.** Fix: A1 refuses any N whose migration head is not ahead of the island's stamp unless an explicit operator rollback is recorded, and the table names that arm (restore, then install N).
- **§5 B5 [A][E:76,E:82] and B4 [E:73-75] — the discriminator measures the wrong thing, and the [E] tags overclaim.** B5 branches on marker-presence and `migrates`, but the question it must answer is "did N ever run / serve?" So: marker-absent because N *never started* (docker error, disk full) still routes to restore + **quarantine a release that never ran**; marker-present ∧ ¬migrates gets one attempt and quarantines a good release on a slow bus, though B4′'s stated rationale ("slow broker ≠ bad release") is orthogonal to `migrates`. Same class one cell over: A2's `stamp = head(P)` is an image read, while the invariant `migrates` needs is the *volume's* stamp — the quantity E7 actually measures (E:70/74 read it from the db), and the two differ in exactly the rolled-back state §1 calls routine. And the tags: E:76/E:82 prove "the previous image refuses a forward db," not "read the marker and branch"; E:73-75 prove migrate-and-admit, not the 600 s health wait — the harness never starts the app (E6/E7 say so). **Class: wrong discriminator + evidence overclaim (the r2 class, re-armed).**
- **§4 promotion memory [A] — the sampler inherits the same sha/digest hole, and the brake's failure mode is unnamed.** "Attributes each sample to the digest whose `sha` the island reports" cannot identify a digest; a rebuild inherits another digest's 7-day soak, so the canary never tests the binary it promotes. And an unreadable `holds.json` (GitHub blip) has no stated disposition: fail-open silently disengages the operator's emergency brake; fail-closed freezes every island. **Class: unstated failure mode.**

**What holds:**
- The marker's placement — immediately after `verify_schema()`, before `bus.start()` and before uvicorn serves — is the right instrument: it is the one signal that separates "never past the schema check" (restore is lossless) from "may have served" (restore loses writes). B5a's restore being conditional on that is correct.
- §1's single predicate (`schema_status`), shared by both boot layers, with a refusal message naming the working fix, is the strongest thing in the bundle: "the previous image refuses a migrated db" is a fact (E:76, E:82), not a hope.
- Fold 4 (host-side `pre-<txn>.db`) and fold 5 (one `COMPOSE` with `--env-file image.env`, shared by watcher and `update.sh`) close real writer/reader gaps.
- Both subtractions are right in *direction*; they need their lost branches restored, not the folds reverted.

**If RECAST, what to fold back:**
- §2: write the sentinel idempotently on **every** `migrate.run()` where `aiko.db` exists (backfill), and add the third test arm (existing db, no sentinel → sentinel written). Name the sentinel as a floor and have the watcher/`update.sh` refuse to pin an image below it.
- §5: define `Healthy(X)` to include "the running container's image digest == X," and record the created container id in `txn`. `/health`'s sha stays a cross-check.
- §5 R: add a bounded re-observation arm — R re-enters the in-flight phase's *wait* (B4 health, B5 marker read) for the same budget, then applies the phase's arm; it still never executes B5a/B6. State that `clear` leaves the stack down, or make `clear` end with the phase's own `up -d`.
- §5 A1: refuse a resolved N whose migration head is not ahead of the island's stamp unless `auto-update rollback <digest>` is recorded; add the authorized-rollback arm (B5a restore → `up -d N` → CURRENT(N)).
- §5 B5: branch on "did N run/serve" (container exit state + marker), not on `migrates`; retry once regardless of `migrates`; let `migrates` decide only the post-second-failure action. Re-tag B5/B4 honestly ([A] for the marker read and the health wait; keep E:76/E:82 for the refusal premise).
- §4: key the sampler on the digest the island reports (needs the Healthy fix); name the unreadable-`holds.json` disposition (fail-closed to steady + page).
- §4 named tradeoffs: state that A2's drift preflight makes auto-update refuse on every release that touches `compose`/`deploy/`, since nothing syncs the box's tree (#2301) — the automation's steady state there is "refuse + notify," not "update."

---

