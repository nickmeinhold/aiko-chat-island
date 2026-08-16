## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The design is well-built inside a frame it never justified — it inherited "liveness is a fact only the SFU knows" from #3159 and never noticed that for the *dominant* bug (the caller hangs up), **the caller's own client already knows**, and can say so signed, over a door that already exists.

`Roddy Piper: "I came here to chew bubblegum and kick ass — and I'm all outta bubblegum."`

**Fatal flaws:**

- **[WRONG OPTION-FRAME — the illegal move] The signed `call-ended` message was never priced, and it dissolves the headline bug at near-zero cost.** The reported failure is: *caller taps Call, hangs up 3s later, callee still rings and answers into an empty room.* In that scenario the caller's client **knows it hung up**. It can emit a second signed message (`aiko:call/1 ended`, or a `replyTo` the invite) over the existing message door — inheriting auth, ACL, existence-hiding, block-filtering and the live WS for free, exactly as the invite does. No `calls` table, no webhook receiver, no TTL sweeper, no outbound SFU call, no new endpoints. The app tab's line *"the app cannot ask LiveKit anything"* is **true but irrelevant** — the app doesn't need to ask LiveKit about a hangup it performed itself. It only needs the SFU for the *involuntary* cases (crash, network loss, force-quit). The design inherited an SFU-shaped premise from #3159's framing and `CRUCIBLE.md` never re-derived it. This does not necessarily kill the object model — call history and glare are real and messages don't solve them — but **a design that doesn't price the alternative that dissolves its headline use case has not earned its machinery.** Add it to Rejected Alternatives with a real argument, or adopt it as increment 0.

- **[ATTACKER-CONTROLLED INPUT IN A SECURITY CHECK] `call.started_at >= invite.signedAtMs - skew` trusts a timestamp the *caller* chose.** `signedAtMs` is inside the signed envelope, which proves the *signer* picked it — not that it is true. A caller with a skewed or deliberately-future clock signs an invite dated hours ahead; it then satisfies the freshness comparison against **any future call in that channel**, indefinitely. The signature makes it unforgeable-by-others, not honest. This is the design's single load-bearing binding between an invite and a call, and it rests on unvalidated client time. Server-observed receipt time (`messages.created_at`) is the only clock the island can stand on.

- **[BACKEND-FIRST VIOLATION] `POST /v1/channels/{channel_id}/calls` is defined channel-generically, so the island would ship a group-ring primitive the app refuses to use.** The DM-only restriction currently lives in *two client-shaped places* — the app's `admitRing` gate and the island's `video-token` DM-only rule (cage-match #122 rd7). The new POST route has neither. Any member of `#general` could create a call there; whether that rings everyone is then purely an app-side policy decision. `feedback_backend_first_for_trust_boundaries`: the invariant isn't true until the server rejects the bypass. The DM-only gate must be **on the mutator**, in the same door, not inherited by assumption.

- **[MISSING MECHANISM — the third writer has no engine] The design names three writers to `ended_at` (webhook, reconcile, TTL sweep) and never says what *runs* the sweep.** The island is a FastAPI app; nothing in the design introduces a scheduler, and the repo enforces single-worker serving via an exclusive flock (#111 / PR#70). So the TTL is either (a) a background task — new infra, unstated, and it must not double-fire across workers, or (b) evaluated lazily on read — in which case **`ended_at IS NULL` no longer means "live"**, the partial unique index no longer means "one live call per channel", and the glare fix silently weakens. The data model's central claim depends on which, and the design picks neither.

- **[NAME LIES ABOUT THE ACTION] `POST /calls` returning `joined: true` names an action the endpoint did not perform.** Nobody joined anything — a call id was returned. A client author reading `joined` will reasonably believe the participant is in the room. `existing: true` / `created: false` says what happened. Small, but this is the `feedback_borrowed_class_borrowed_behavior` shape: a name carrying a meaning the code doesn't implement, on the highest-privilege path in the app.

**What holds:**

- **The synthesis is genuinely right and is the best thing in the bundle.** "The invite says *a call started here*; the island says *here is the call*" removes the v2-wire-format cost that #3170 assumed was mandatory, and keeps `kCallInviteBody` — a one-way door already in signed history — shut. That reasoning survives my strike intact.
- **The glare kill in `FOLD.md` is correct and well-argued.** "A derived identifier structurally cannot dedupe, because dedupe requires a writer that can see both candidates and pick one" is a real invariant, honestly reached by killing the author's own favourite idea. The bundle is *more* trustworthy for containing it.
- **Webhooks-as-fast-path-never-authority** is the right posture given LiveKit's documented "no guarantees around delivery", and the research pass verified that rather than recalling it.
- **Existence-hiding parity, 503-when-unconfigured, and count-only participants** are all correctly inherited from `video-token` and correctly *not* tie-broken against the app tab.
- **Naming the "which identifier does the client receive" contract table**, on the back of the `dm:` prefix incident, is exactly the right lesson learned in the right place.

**If RECAST, what to fold back:**

- Add **signed `call-ended` message** to Rejected Alternatives with a real argument — or adopt it as **increment 0** and re-scope the table to what messages genuinely cannot do (glare dedupe, call history, involuntary-disconnect liveness).
- Replace `invite.signedAtMs` in the admission check with the **server-observed receipt time** of the invite message; never let client-chosen time gate admission.
- Move the **DM-only gate onto `POST /calls`** (the mutator), not just `video-token`.
- **Decide the TTL engine explicitly** — background task under the single-worker flock, or lazy-on-read — and if lazy, restate what `ended_at IS NULL` means and re-derive whether the partial unique index still enforces one-live-call.
- Rename `joined` → `existing` (or `created`).
- State the **`departureTimeout` value read off the running v1.13.5 config** before the TTL is chosen; it is currently an open variable that a security-relevant timer depends on.
