# TEMPER.md — `webrtc://` DataScheme, ROUND 3 (final round of the budget)

**Overall verdict:** **RECAST — and the ≤3-round budget is now EXHAUSTED.**
**Disposition: NOT build-ready. The candidate SPLITS.**

**Struck:** `dt3-1787225374`, 2026-08-20. Families seated: **Maxwell (Claude) + Kelvin
(Gemini 3 Pro) + Carnot (GPT/Codex) + Tesla (Grok)** — full 4-way, third consecutive full panel.

**Scope stamp (binding):** DESIGN temper only. The implementation is a **spike**, not production
code. Publish direction unimplemented, duplex collision untested, all island integration
untouched (the spike mints from the SFU `--dev` key).

## Per-family verdicts

| Family | Verdict | One-line |
|--------|---------|----------|
| Maxwell (Claude) | RECAST | D1 inherits a *bounded* copy of the ghost-seat flaw it used to kill the sidecar — and never discloses it. |
| Kelvin (Gemini) | RECAST | "Builds its house on a glacier, ignoring the measured melt of its own foundation." |
| Carnot (GPT) | RECAST | "Bounded teardown is a necessary clock, not yet a sufficient safety proof." |
| Tesla (Grok) | RECAST | "Death was not observed. Death was announced." |

**Nobody voted DISSOLVE, and all four said explicitly: do not flip the spine again.** Tesla:
*"a fourth recast that re-homes the Room to a sidecar to avoid writing a reaper is the orphan
factory volunteering for night shift."* The architecture has converged; what remains is a
**named missing mechanism**, not an open architectural question.

## The finding, reached independently by all four

**An abandoned Room is not a dead Room, and the design says it is.**

D1 races `disconnect()` against a 5s deadline, abandons the await, and *"declares the Room
dead"*. The spike justified that with 0.00 thread and 0.00 fd growth. But threads and fds
cannot see what the abandoned Room still holds:

- an **SFU participant seat** (for an unmeasured `participant_timeout` — nobody has looked it up)
- **ICE/TURN allocations** on a machine we do not own
- **decoder / object graph** — plausibly the home of G1's unexplained ~0.4 MB/cycle RSS drift

Tesla put it best: *"Death was not observed. Death was announced."* Carnot: *"the second law
showing entropy somewhere below the instruments."*

**And it compounds into a self-DoS.** D1's restart-storm policy fails closed for
`(room, identity)` *while a prior bridge is unreaped* — but reaping waits on the same
`disconnect()` the spike left hung at 90s, so "unreaped" means **never**. A hostile teardown
therefore does not degrade the capability, it **denies** it, for a window nobody has measured.
The pipeline survives; the robot cannot rejoin. **That is the flaw: D1 optimised for process
liveness and bought an outage in the user capability.**

## Fatal flaws (deduped, most-severe first)

1. **"Declared dead" is a local lie; the remote seat outlives it.** — *4/4.*
   **FOLD:** strike "declared dead". The Room becomes **LOCALLY ABANDONED / REMOTELY
   MAYBE-LIVE** until either a *measured* `participant_timeout` or an explicit control-plane
   eviction. Measure that timeout and put the number in the design.

2. **Fail-closed against a ghost you just minted is a 3am self-DoS.** — *Maxwell, Carnot,
   Tesla.* **FOLD:** generational identity — key on `(machine principal, connect epoch)`. A
   local abandon must never block epoch N+1; epoch N is not reused until the janitor or a
   measured TTL clears it. Fail-closed survives only as a *same-epoch re-entrancy guard*.

3. **The design needs a SEAT REAPER, and it is the one genuinely new mechanism this round
   produced.** — *Maxwell, Carnot, Tesla, independently.* The moment you refuse to wait on the
   SFU you owe a third door besides "hung `disconnect()`" and "unmeasured TTL": a **LiveKit
   RoomService `RemoveParticipant` call** on the next mint for that pair.
   **PRICE IT, do not assume it:** the island has **never called the LiveKit server API** —
   `mint_room_token` deliberately withholds `roomAdmin`/`roomList`. This is genuinely new
   surface with its own trust question (who may evict whom).

4. **D2's process-local registry cannot own a host-wide invariant, and "host-wide" is the wrong
   boundary anyway.** — *4/4.* Kelvin: *"defended by a mayfly"* — the registry evaporates on
   crash, which is precisely when a restart storm begins. Carnot and Tesla both sharpened it:
   **the collision domain is the LiveKit room across all clients**, not one host — two hosts,
   a cloned disk, or a recovered principal all defeat a host-local map.
   **FOLD:** demote the registry to an intra-process re-entrancy guard; name the SFU/room as
   the real mutex; stamp *"duplex is process-local until the machine-principal lifecycle
   exists"*; promote **G3 from untested to load-bearing**.

5. **The correctness property now depends on the least-built half.** — *4/4.* Move enforcement
   to the mint (as flaw 4 requires) and the duplex invariant becomes a property of D3/D4 — the
   half the spike discharged *nothing* of, whose headless credential path has never run, and
   which is blocked behind #3096/PR#136 (**open**). The build order schedules auth at step 3,
   i.e. **after the thing it protects.** Carnot: *"merge readiness cannot sequence security
   after bidirectional media and still call the design coherent."*
   **FOLD:** move G4/G5 earlier; transport-only stays a spike branch, not the candidate.

6. **G1's RSS drift is promoted from soak-item to merge blocker on the in-process default.** —
   *Kelvin explicitly; Carnot and Tesla concur.* D1 was justified with the meters that cannot
   see it. Acceptance must measure RSS, the abandoned object graph, SFU participant count,
   subscriptions and TURN/ICE indicators — and prove successor creation after a timed-out
   teardown. Kelvin: if the drift cannot be eliminated, **in-process is not viable as a default.**

7. **D7 is a knob where a contract belongs, and "lossless" is a false word.** — *Maxwell,
   Carnot, Tesla.* Under D1 there is no serialization hop, so `fidelity` selects nothing on the
   default path. Tesla is right that "lossless" is wrong regardless: the SFU track is *already*
   a lossy codec, so the promise is "no **second** codec", not conservation of photons.
   **FOLD:** rename to `decode=` (`ndarray` default | `jpeg`), scope it to what the scheme
   *emits into the pipeline*, and make a nonsensical combination fail closed rather than be
   silently ignored.

8. **D5 scoped out the janitor along with island-local media.** — *Tesla.* Keeping media off
   the island is right; scoping out the control-plane eviction and *then* failing closed on the
   un-evicted seat was the over-correction. **FOLD:** island-local *media* stays out of v1;
   control-plane kick of an abandoned identity comes **in**.

## What holds

- **The spine. Do not flip it again — 4/4, explicitly.** In-process is right for the measured
  reason; the sidecar-as-default stays burned.
- **The liveness invariant itself:** no pipeline-lifecycle operation may wait indefinitely on
  SFU liveness. Correct, and it generalises past this scheme.
- **D3's trust boundary** — unchallenged across three rounds and four families.
- **D6's four conditions**, each individually evidenced by the spike.
- **D5's host choice** — pipeline/robot host over the gateway/chat failure domain.
- **The `webrtc://` / `livekit-local://` split**, and the thesis itself: first-class-ness at the
  URL layer is *demonstrated* (F6), not argued.
- **G1–G5 as honest named gates** rather than prose-covered constants.

## Disposition — the budget is spent, so this is the honest read

Three rounds, three full panels. The design **flipped in round 1** (on an unrun claim),
**flipped back in round 2** (on measurement), and **held in round 3** — all four families
declined to move the spine and instead named one missing mechanism. That is convergence, not
thrash. But convergence is not a pass, and the budget is exhausted:

> **NOT BUILD-READY. `UN-TEMPERED` for build purposes.**

**The candidate splits, because it is two designs at very different maturities and one verdict
cannot honestly cover both:**

- **The transport half** (D1 + D6 + D7 + the new seat-reaper and epoch folds) has instruments,
  measurements, and a working spike. It needs the reaper written and G1 answered — then it is a
  narrowed candidate that could go to Blade.
- **The identity half** (D2 + D3 + D4) is genuinely still at **Cast** stage: nothing about it
  has been exercised, and flaw 5 shows the transport half's *correctness* now leans on it. It
  should be its own design with its own forge, not a section in a transport doc.

Recommended next move is **not** a fourth recast of this document. It is: measure the
participant timeout, write the reaper, answer G1's soak — then re-open the transport half as a
narrowed candidate, and forge the identity half separately.
