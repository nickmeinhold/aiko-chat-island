Ripgrep is not available. Falling back to GrepTool.
## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** The design's attempt to avoid a wire-format version bump by using a time-based heuristic for call identification creates a critical ambiguity that allows for call hijacking, and its reliance on a shared secret for a new write-capable webhook surface unacceptably expands the blast radius of a single island compromise.

**Fatal flaws:**
- **CLASS: Identity & Authentication.** The core claim that a callee can identify the correct call without a `call_id` in the invite body is false. The proposed `channel + time` binding (`call.started_at >= invite.signedAtMs - skew`) is a heuristic, not a proof. It fails catastrophically under real-world conditions of network delay or malicious replay, allowing a stale invitation for one call to be resolved to a completely different, live call. This is a failure to bind the authorization (the invite) to the resource (the call). `Frank Poole: "Open the pod bay doors, HAL."` The door to the wrong call is wide open.
- **CLASS: Trust Boundary & Blast Radius.** The introduction of a new, inbound webhook surface, authenticated by a shared HS256 secret, turns a read-only credential into a write-capable one across all tenants. As noted in the project's own research, this is "NOT a cryptographic tenant boundary." Granting a remote "end anyone's call" primitive that crosses this non-boundary makes per-island keys a strict prerequisite, not an optional follow-up. To proceed without them is to build on a foundation of ice over a thermal vent.

**What holds:**
- The server-minted call object model, arbitrated by a `UNIQUE` index to solve the glare problem, is a sound and robust approach to establishing a call as a first-class entity. It correctly earns its place over the rejected derived-name alternative.
- The liveness model is a well-considered defense against the cold realities of best-effort webhook delivery. The three-phase mechanism (fast-path webhook, on-answer reconcile, TTL sweep) provides a practical balance of performance and correctness.
- The rejection of the simpler "occupancy on an eternal room" alternative is correct. The analysis that the object model is *cheaper at the trust boundary* by avoiding a high-frequency outbound dependency is the single strongest argument in the entire forge. `Roy Batty: "I've seen things..."` — and seeing an object model be cheaper than a simple poll is one of them.
- The phased migration path for the `video-token` endpoint and the fail-open strategy for mixed-version compatibility are sound.

**If RECAST, what to fold back:**
- **Dissolve the time-based binding.** The synthesis that "the invite says a call started here; the island says here is the call" is the source of the fatal ambiguity. It must be abandoned. The cost of avoiding a v2 wire format is too high. The design must be recast to include the server-minted `call_id` in a new, versioned invite message. This provides an unbreakable cryptographic binding and is the only way to achieve absolute zero ambiguity. Update the "Problem" and "Shape" sections to reflect that a `v2` invite is the price of correctness.
- **Elevate per-island secrets to a prerequisite.** The "Blast radius & consent spine" section must be updated to state that the webhook receiver cannot be shipped until per-island LiveKit secrets are implemented. The build order must be modified to place this task before the implementation of the webhook receiver. You cannot build a secure system on a principle of shared, writable secrets. That is thermodynamic nonsense.
