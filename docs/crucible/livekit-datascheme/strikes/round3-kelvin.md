## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** This design builds its house on a glacier, ignoring the measured melt of its own foundation and the ghosts of processes past that will drag it into the crevasse.

**Fatal flaws:**
-   **D1/G1 — The primary leak was misidentified, not eliminated.** The design correctly discards the phantom thread/fd leak but embraces an in-process default while acknowledging a measured, unexplained **~0.4 MB/cycle RSS drift**. This is not a detail for a soak test; it is the original sin of resource leakage returning in a new form. A design that knowingly leaks memory is a design for a system that will slowly freeze to death. This is thermodynamic law, not a test plan.
-   **D2 — The duplex invariant is defended by a mayfly.** A process-local registry is an ephemeral guard for a host-wide invariant. It evaporates on process-crash or restart — the very moments a "restart-storm" begins and the invariant is needed most. Relying on a separate, unbuilt identity allocation scheme (D4) to prevent collisions is not a design, it is a prayer. The ghost of a crashed pipeline's SFU participant will laugh as the new instance collides with it. *Roy Batty: "All those moments will be lost in time, like tears in rain."*
-   **D3/D4 — The security model is a vacuum.** The transport mechanism is measured, but the entire identity and authorization spine (D3, D4, G4, G5) that makes it safe is deferred. It is a detailed blueprint for an engine, bolted to an empty space labeled "chassis." A design whose security half is pure vaporware cannot be called "sound" at any temperature. The asymmetry between the measured transport and the unmeasured identity model is a cold fault waiting to fracture the entire structure.

**What holds:**
-   **The thesis is sound.** `webrtc://` as a first-class, URL-driven aiko transport is proven correct by the spike (`FINDINGS.md`, F6). The core vision survives.
-   **D1's bounded teardown is the right liveness fix.** Abandoning a hung `disconnect()` is the correct response to an unbounded hang. The design correctly chooses a clock over a process to solve a liveness problem.
-   **D5's host selection is a rare point of clarity.** Choosing the pipeline/robot host as the default and scoping island-local out for v1 provides a clear, defensible blast radius.
-   **D6 correctly demotes the sidecar.** It survives as an opt-in isolation mode with a strict, evidence-based contract born from the spike's findings (`FINDINGS.md`, F2, F7).
-   **D7's `fidelity` contract is necessary.** It makes the quality/performance trade-off explicit, a direct and correct lesson from the spike's discovery of the second lossy codec (`FINDINGS.md`, F5).

**If RECAST, what to fold back:**
-   **G1's RSS drift is promoted to a merge-blocker on the in-process default.** The design must be recast to include a plan to investigate and mitigate this memory growth *before* a build, not merely observe it in a soak. If it cannot be eliminated, the in-process path is not viable as a default.
-   **D2's enforcement point must be made crash-survivable.** The process-local registry is slag. The design must specify a robust, host-local mechanism — a lock file with PID checking, a lightweight local coordination service, *something* that outlives a single process — to enforce the `(room, identity)` invariant.
-   **D3/D4's identity contract must be specified.** The design must define the explicit API contract with the island for token minting, refresh, and revocation, including all error states and lifecycle assumptions. The transport can then be built against this contract, even if the island's implementation is initially a mock. The hand-waving must be frozen out.
