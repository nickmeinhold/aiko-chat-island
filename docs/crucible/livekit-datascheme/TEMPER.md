# TEMPER.md — `webrtc://` DataScheme (round 2, striking the round-1 re-cast)

**Overall verdict:** **RECAST** — and the recast is an *inversion*: the out-of-process sidecar
must stop being the default.

**Struck:** `dt-1787206431`, 2026-08-20. Families seated: **Maxwell (Claude) + Kelvin (Gemini
3 Pro) + Carnot (GPT/Codex) + Tesla (Grok)** — full 4-way. (Wu/Kimi disabled.)

**Scope stamp (binding):** this is a **DESIGN** temper. The implementation is a **spike**, not
production code, and nothing here is a green light on it. Publish direction is unimplemented;
the duplex collision is untested; the island capability-token model, allowlisted origin and
machine principal are **entirely untouched** (the spike mints from the SFU `--dev` key);
measurements are 640×480, one track, one subscriber, a local SFU, on macOS.

## Why round 2 was not a re-run of round 1

Round 1 overturned this design's mechanism on a claim **nobody had executed**: *"a hung
`disconnect()` orphans the thread/loop/peer-connections and restart storms leak."* Three
families agreed — corroboration between reasoners, not evidence about the system. The spike
(`spike/FINDINGS.md`) then measured it against a real `livekit-server v1.13.5`, and the panel
was struck on the corrected premises with an explicit instruction not to re-confirm round 1.

It didn't. It reversed it.

## Per-family verdicts

| Family | Verdict | One-line |
|--------|---------|----------|
| Maxwell (Claude) | RECAST | Decision 1 breaks decision 2: per-process Rooms make the duplex invariant unenforceable *while satisfying its own wording*. |
| Kelvin (Gemini) | RECAST | "The re-cast bought absolute zero protection against a ghost leak, at the cost of a new, measurable, fatal orphan class." |
| Carnot (GPT) | **DISSOLVE** | The sidecar default "spends entropy on process supervision to solve a liveness defect, then creates a harder orphan class on hard-kill." |
| Tesla (Grok) | RECAST | "The leak was a phantom, the hang is one curse wearing three bodies, and the sidecar is the orphan factory those storms will run at 3am." |

**One DISSOLVE is a strong finding, not a kill** (the bar is ≥2). And Carnot's own fold-back
*keeps* `webrtc://` and reverts the default — so its DISSOLVE is aimed at **decision 1**, not
at the candidate. Read that way, the panel is **unanimous 4/4 on the central question.**

## Fatal flaws (deduped, most-severe first)

1. **Decision 1's justification is measured-false; the default must invert.** — *all four
   families.* The leak premise that carried the sidecar is dead (0.00 threads/cycle,
   0.00 fds/cycle, both arms, clean and hostile). The real defect is **liveness**, and
   `asyncio.wait_for(room.disconnect(), T)` + abandon is measured leak-free — the design's own
   hostile-teardown arm A *is* that fix.
   **DISPOSITION: fold —** in-process Room becomes the DEFAULT; sidecar demoted to an explicit
   opt-in isolation mode.

2. **The re-cast MOVED the leak to a worse trigger, and under-counted its blast radius.** —
   *all four.* aiko installs no SIGINT/SIGTERM handler, so `destroy_sources` runs only on the
   graceful path; a hard-killed pipeline leaves a sidecar **holding a live SFU participant seat
   nothing will reclaim**. Tesla priced the 3am version: crash-loop / OOM-kill / `docker kill` /
   kube preemption ⇒ "morning finds the SFU at max-participants and no robot, no agent, no
   human can sit." This is an authorization + capacity failure, not cleanup debt.
   **DISPOSITION: fold —** if a sidecar exists at all it is illegal without (a) supervisor
   SIGTERM→T→SIGKILL escalation, (b) lifetime bound to the parent (parent-death watchdog /
   `PR_SET_PDEATHSIG` / cgroup), (c) an island-side participant janitor/TTL so a missed reap
   cannot hold a seat.

3. **Decisions 1 and 2 contradict each other, and the wording hides it.** — *Maxwell and Tesla,
   independently.* Decision 2 keys the duplex invariant on `(process, channel_id, identity)`.
   Decision 1 puts every Room in its own process, so that invariant becomes **trivially,
   permanently satisfied while the harm it exists to prevent is unmitigated** — two pipelines,
   or a restarted pipeline racing an unreaped orphan, carry the same identity into the same
   room and LiveKit's last-session-wins silently kills one. In-process a registry can enforce
   the real invariant; out-of-process there is no shared place to hold one.
   **DISPOSITION: fold —** restate as **at most one live `Room.connect` per `(room, identity)`
   host-wide**; drop `process` from the key; name the enforcement point.

4. **The design refuses to choose a deployment host, so it cannot price its own blast radius.**
   — *Maxwell, Tesla; implied by Carnot.* "A robot host, **or** island-local" is an unmade
   load-bearing decision, and the two have **opposite polarity**: on a robot, process death *is*
   the janitor; in the island gateway container — long-lived, also serving chat — a hung
   in-process await can mute chat and an unsupervised sidecar accumulates ghost participants.
   **DISPOSITION: fold —** choose the host, do not OR it; record the blast for each.

5. **The kill-escalation ladder is the entire mechanism and is unwritten.** — *all four.* The
   sidecar hung on SIGTERM 8/8 (all children `rc=-9`). "The OS reaps what Python cannot" is only
   true if someone escalates. **DISPOSITION: fold —** written into the design, not left as an
   implementation detail.

6. **Decisions 3 and 4 survive as contracts but were DISCHARGED BY NOTHING.** — *all four.* The
   spike minted from the SFU dev key; the capability token, allowlisted origin and machine
   principal are untouched. Decision 4 additionally rests on a headless passkey-register →
   refresh path that has **never been exercised**, downgraded from blocker to "provisioning
   step" on reasoning alone — while agent identity (#3096) is PR#136, **open, not merged**.
   **DISPOSITION: fold —** re-open as genuine open dependencies and merge-blockers; the
   machine-principal lifecycle (provision / store / rotate / revoke / recover / transfer) is a
   real sub-design, not a footnote.

7. **The JPEG hop is a sidecar-only tax, and lossiness belongs in the contract.** — *Maxwell,
   Carnot, Tesla.* In-process yields numpy natively and the second lossy codec disappears
   entirely. Two lossy codecs in series is fine for inference and wrong for measurement or
   anything a control loop acts on. **DISPOSITION: fold —** in-process default is raw numpy;
   JPEG documented only on the zmq sidecar path; fidelity becomes an explicit term of the URL
   contract rather than an inherited property.

8. **The ~0.4 MB/cycle in-process RSS drift is the one live reason not to call in-process
   sound.** — *Carnot, Tesla.* Threads and fds are flat, so this is not the claimed leak — but
   it is unexplained and one layer below where anyone looked.
   **DISPOSITION: named gate —** the in-process default is gated on a long soak (thousands of
   create/traffic/destroy cycles with hostile pauses), not declared safe on 16.

## What holds

- **The thesis, and it is now demonstrated rather than argued.** `webrtc://` as a first-class
  aiko transport works at the **URL + registration layer**: aiko's own **unmodified**
  `ImageReadZMQ` reads a LiveKit room after a one-string change, pixels verified. All four
  families say: do not dissolve the scheme.
- **The frame adapter** — numpy ↔ `VideoFrame` is ~1 line each way, proven against a known
  pattern with a positive-controlled comparator; the acceptance bar is a similarity budget, not
  byte equality (WebRTC is lossy by construction).
- **The hang is real and total**, and bounded teardown is mandatory in *whatever* process awaits
  the SFU. Tesla: "Nature does not care about your process tree."
- **Decisions 3 and 4 as directional contracts** — the island must be the token authority, the
  token must be capability-scoped and never a full user session, and a machine principal beats
  reusing the operator's session. Correct, and undischarged.
- **Fold-A / Fold-B** (dead-link ≠ idle, fail-closed on a half-open source) and the parent-death
  watchdog — including its instructive first failure: **state change must precede logging**,
  because the pipe is already dead.

## Disposition

**RECAST.** Fold flaws 1–8 into `DESIGN.md`, with decision 1 **inverted**, then re-strike.
This is round 2 of a ≤3-round budget.

The shape the panel converged on, unanimously:

> **In-process Room is the spine, with a bounded teardown as the liveness invariant.
> The out-of-process sidecar is an explicit opt-in isolation mode, legal only with a written
> supervisor contract, a parent-death reaper, and an island-side participant janitor.**

Not build-ready. `webrtc://` the *idea* is now proven; `webrtc://` the *design* is on its second
recast, and its security and identity half has been discharged by nothing at all.
