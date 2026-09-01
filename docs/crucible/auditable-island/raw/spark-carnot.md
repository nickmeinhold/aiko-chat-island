**Phantom Tenants**

Make every island host a hidden population of synthetic users whose only job is to be indistinguishable from real users.

When someone installs an island, the container ships with a “phantom tenant kit”: thousands of Ed25519 identities, pre-generated social patterns, message timings, call attempts, blocks, accepts, ring paths, and complaint triggers. None are globally registered. None require other observers. Anyone can mint more.

The trick: the island operator never knows which traffic is human and which traffic is audit theatre.

A client can quietly sponsor phantoms. So can another island. So can a random person on a laptop. A phantom behaves like a real user: it has friends, accepts calls, refuses others, rotates keys, disappears for days, sends signed messages, joins rings, misses calls, complains when delivery semantics are violated. Its whole life is locally scripted and cryptographically witnessed by the phantom’s own keys.

Audit stops being “prove your source code is virtuous” and becomes “survive an endless haunted userbase.”

If an island drops messages selectively, delays certain paths, tampers with manifests, deanonymizes rings by active probing, invents call metadata, or treats unknown users differently from favored users, it may be doing that to a phantom. The phantom then releases a compact signed play: “Here was my identity, here were the encrypted envelopes I submitted, here were the expected protocol responses, here is what the island signed or failed to return.”

No public refusal log. No friend graph leak. No central directory. No auditor class. Just actors walking around the system carrying sealed scripts.

The audit artifact is not “this island is good.” It is a theatrical conviction: a reproducible, signed scene showing the island broke a rule while believing it was serving an ordinary user.

The island becomes auditable because every user can bring their own invisible courthouse.

OH OF COURSE: Make abuse detection indistinguishable from normal use, so the operator has to behave for everyone because anyone might be a phantom.
