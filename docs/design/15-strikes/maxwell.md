## MaxwellMergeSlam's Design Strike

**Verdict:** DISSOLVE

**Summary:** The design never considers making the box a git checkout — which the declarative-deploy record already names as the actual gap — and that one alternative deletes the 654-line drift check, the codeload fetch, and with them the only piece large enough to justify a language boundary at all.

`Roddy Piper: "I have come here to chew bubblegum and kick ass. And I'm all out of bubblegum."` I wrote this casting four hours ago and I'm here to break it, because the option-frame is wrong and I built inside it anyway.

**Fatal flaws:**

- **WRONG OPTION-FRAME (the illegal move, and it's mine).** §1 congratulates itself for finding a third home — and then never asks the obvious fourth question: *why is the box not a git checkout?* The design treats "the box is not a checkout" as a law of nature. It isn't. `docs/crucible/47-declarative-deploy/DESIGN.md` states it as a **defect**, in the target list, in these words: *"Neither box is a git checkout — 'repo-authoritative' is currently a fiction maintained by hand-copying."* That is the recorded diagnosis of this exact surface, and design 15 quotes the same document twice while walking straight past it.

  A shallow checkout of `~/apps/aiko-chat-gateway` pinned to the deployed tag makes `git status --porcelain` **the entire drift check**. One command. It replaces 654 lines. It also replaces the codeload tarball fetch, the tar-extraction path handling, the `cmp -s` walk, the box-only-files warning arm, the subtree-counting arm, and the ISLAND_REF_TREE test seam — all of which exist only because the box has no way to know what the tag's bytes were.

- **THE DISSOLVE CHAINS, AND THAT IS WHY IT IS FATAL RATHER THAN MERELY BETTER.** §3f's own accounting: uncontested movement is 334 lines; with the drift check, 988. If the checkout dissolves the drift check, the language boundary must be justified by **334 lines of three small scripts that already work, are already isolated, and are already tested**. §9's question 5 then answers itself in the negative. The design's best case was always leaning on the contested 654; remove them and there is no case.

- **UNSTATED ASSUMPTION: that adding to the image is free.** §8 proposes shipping Dart inside the gateway image. That image is Python — it bundles `aiko_services` and `aiko_chat` — and every island runs it for the **gateway, registrar, and ChatServer roles**. Adding a Dart AOT stage adds build surface to the artifact that IS the island, to deliver tooling that runs at most a few times a month. The blast radius of a Dart build-stage failure is not "deploy tooling is broken," it is **`release.yml` fails and no island can update at all**. The design prices the box-side risk carefully and the image-side risk not once.

- **THE VERSION HANDSHAKE INHERITS THE FLAW IT WAS DESIGNED TO CLOSE.** §5c proposes a `SHIM_CONTRACT` integer so the image can refuse an outdated shim. But a shim predating the handshake does not know to send one — it invokes the tool and proceeds. So the handshake binds only shims new enough to already be in sync, which is precisely ISL-0003's limit (*"protection starts on the deploy after the operator syncs deploy/ once by hand"*) reproduced one layer down. §2 sets "make the remaining surface's drift detectable" as the success criterion and §5c does not meet it for the case that matters — the stale box, which is the only case there ever was.

- **THE PREMISE MAY ALREADY BE SATISFIED.** ISL-0003's amendment — *"prose functioning as executable governance with no compiler"* — is the design's whole warrant. But the fix for that landed already: `preflight-compose-drift.sh` **is** the compiler, it exists, and it ran clean on both boxes today comparing 6 files. The recorded outage (#4230) was not caused by a guard written in the wrong language; it was caused by **there being no guard**. Design 15 silently upgrades "the guard lives somewhere that doesn't sync" into "the governance still has no compiler," and those are different claims. The first is true and narrow. The second is the one that would justify 2,373 lines of migration, and it is no longer true.

**What holds:**

- **§3b stands and is the document's real contribution.** `standup.sh` pulls the image, so at the moment it runs there is no image to run tooling from. That is chicken-and-egg and no reordering touches it. 781 lines — a third of the surface — is pinned to the box permanently, and #4684 did not know this. This survives the DISSOLVE and should be carried into ISL-0003 regardless of what happens to the rest.
- **§3e's rejection of the docker socket** is correct and decisive. Both boxes are multi-tenant; a socket-mounted container is root-equivalent and by construction not per-app scope. Any future proposal must clear this, not argue around it.
- **§3a is right that `verify-secrets.sh` was miscounted.** It is control-side, guards repo artifacts, and inflates the migration by 252 lines.
- **§7's qualification is honest and the evidence is real** — these scripts' recorded defects are in *decisions*, not reads. That argument survives; it simply is not worth a language boundary at 334 lines. It would be worth **a test suite**, which is cheaper and available today.
- **§6's non-goal** — the shim never initiates — should be preserved verbatim into whatever replaces this, because the pull toward reactive deploy is real and the FATAL is recorded.

**If RECAST, what to fold back:**

I do not think this should be recast, but if the panel disagrees, the design cannot be re-struck without first answering:

- **Price the git-checkout alternative explicitly** and state the falsifier. What would have to be true for a pinned shallow checkout NOT to work? Candidate objections and my own read of each: *git absent on a third-party box* (weak — install it, or fall back to today's script); *`.git` on a multi-tenant box* (weak — it holds public source); *sovereignty* (**weakest** — the box already fetches this repo's bytes from codeload over HTTPS on every single deploy, so the trust relationship exists; a checkout changes the protocol, not the exposure). If none of these hold, the checkout wins and design 15 is slag.
- **Price the Dart stage in `release.yml`** with its failure mode: no island can update.
- **Fix §5c or drop it** — a handshake the stale case cannot participate in is decoration.
- **Re-scope to a test suite for the three decision scripts.** §7's evidence supports guarding those decisions; it does not support relocating them. That is the cheap version of the same win and it needs no new boundary on anyone's deploy path.
