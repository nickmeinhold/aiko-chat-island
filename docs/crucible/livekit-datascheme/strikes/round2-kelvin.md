## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** The re-cast bought absolute zero protection against a ghost leak, at the cost of introducing a new, measurable, and fatal orphan class.

**Fatal flaws:**
- **[MECHANISM FAILURE] The out-of-process default is a solution to a problem that does not exist.** The entire justification for shelling out to a sidecar was the "restart storms leak" claim. `FINDINGS.md`(F1) proves this claim has zero pressure: thread and fd growth is 0.00. The defect is a `disconnect()` hang — a liveness issue, not a resource leak. The re-cast chose process isolation, the most expensive insulation, to contain a temperature differential of absolute zero.
    > `Roy Batty: "I've seen things you people wouldn't believe... Attack ships on fire off the shoulder of Orion..."`
    And I have seen a design burn a process boundary to fight a phantom.

- **[NEW FAILURE MODE] The out-of-process model introduces a critical leak the in-process model does not have.** `FINDINGS.md`(F7) is the killing blow. A hard kill of the main pipeline orphans the sidecar, leaving a live SFU participant consuming resources indefinitely. The original design, for all its theorized faults, dies cleanly as a single entity. The re-cast did not remove the leak; it moved it to a place where the parent can no longer clean up, creating a zombie. The spike's parent-death watchdog is a patch on a design wound, not a feature of the original re-cast.

- **[ASSUMPTION ON ASSUMPTION] The sidecar's one advantage requires an unstated, un-designed supervisor contract.** `FINDINGS.md`(F2) shows the sidecar also hangs on `SIGTERM` and requires a `SIGKILL` to be reaped. The entire benefit of "the OS can reap what Python can't" is conditional on a supervisor that knows to escalate from TERM to KILL. The `DESIGN.md` re-cast never specifies this; it just assumes its "supervised" helper will be supervised by a mind-reader. A temperature differential requires a boundary; a kill-escalation requires a written contract.

**What holds:**
- **The identity model.** The pivot to a dedicated **MACHINE principal** (`DESIGN.md`, Temper Round 1) correctly identifies and resolves the duplex identity collision. It is the right state.
- **The trust boundary.** Scoping the token via an **`island_url` allowlist** (`DESIGN.md`, Temper Round 1) is the correct security posture, learning the lessons from `scheme_http`'s trial by fire.
- **The duplex invariant.** While untested, the reasoning is sound. Stating the "one Room per (process, channel_id, island identity)" constraint is good design hygiene.

**If RECAST, what to fold back:**
1.  **The default mechanism is REVERTED. In-process is the spine.** The out-of-process model is demoted to a documented, special-purpose alternative for scenarios requiring explicit isolation, not the default path.
2.  **The `disconnect()` hang is a LIVENESS defect, not a resource leak.** It must be solved with a bounded timeout (e.g., `asyncio.wait_for`) around the `await room.disconnect()` call within the in-process bridge. A timeout is a logged `ERROR` state that terminates the bridge thread, ensuring the `destroy_sources` path completes and the primary falsifier (`FINDINGS.md` D) remains loud. Never silent.
3.  The out-of-process helper, if retained as an option, MUST formally adopt the fixes prototyped in the spike as design requirements: a parent-death watchdog and a specified supervisor kill-escalation policy. It cannot be assumed.
4.  The **machine principal** and **island-boundary** decisions from the first re-cast are sound and are carried forward. They are the parts of the correction that were actually correct.
