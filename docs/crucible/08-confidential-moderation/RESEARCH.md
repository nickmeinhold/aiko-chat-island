# Heat / Research — operator-blind confidential moderation

*Movement 2 (Heat). Deep-research pass, 2026-07-30, adversarially verified (5 angles,
23 sources, 25 claims → verified; 0 refuted). Full transcript: workflow wf_d5319f74-31b.*

**Verdict this melt produced: the ore is slag at the locked intersection (solo ×
self-hosted × operator-blind). Candidate INVALIDATED. See VERDICT.md.**

## Findings (all 3-0 verified unless noted)

1. **A TEE cannot protect a key from a motivated host with physical access.** BadRAM
   (CVE-2024-21944, USENIX Security 2025; ~$10, brief physical access, sometimes
   software-only where SPD is unlocked) defeats AMD SEV-SNP confidentiality AND
   integrity and **forges attestation** (replays a valid launch digest so a modified
   VM presents a clean report). TEE.Fail (Oct 2025; sub-$1,000 DDR5 interposer)
   extracts keys — including **secret attestation keys** — from Intel TDX and AMD
   SEV-SNP even with Ciphertext Hiding, and forges attestation. **Both AMD and Intel
   explicitly place physical attacks OUT of their threat models.** Sources:
   badram.eu/badram.pdf, tee.fail, eclypsium, schneier.com.

2. **Threat-model split is structural:** TEEs raise the bar against a REMOTE attacker;
   they fail against the HOST itself. A self-hoster is the host → the exact case the
   hardware does not defend. The operator-blind premise depends on precisely the
   guarantee vendors decline to make.

3. **Credible attestation governance is a large-org capability.** Apple PCC is the only
   demonstrated reference: stateless compute, no privileged runtime access,
   non-targetability, verifiable transparency — backed by a maintained transparency
   log, 90-day binary publication, source on GitHub (apple/security-pcc), a Virtual
   Research Environment, and a funded bounty program. A solo operator IS the physical
   host, controls the build and the log, and cannot provide non-targetability or
   audit themselves. (medium confidence — inference from PCC evidence.)

4. **The detector is independently unreliable and defeatable by its own targets.**
   Perceptual hashing can't find novel content and misfires ~135×/day at WhatsApp
   scale (best FPR ~1e-8); ML classifiers are non-robustly evadable; and **colluding
   sender+receiver layer their own encryption — "any detection scheme capable of
   detecting the encrypted content would be able to break encryption generally."** So
   the bad actors evade trivially while false positives hit innocents. Sources:
   arxiv 2303.03979, 2201.11105.

5. **Prior art is against us.** "Bugs in Our Pockets" (Abelson, Anderson, Bellovin,
   Blaze, Diffie, Rivest, Schneier, Troncoso) judges client-side scanning (the closest
   deployed analog) net-negative for security and privacy. The bot-as-E2EE-member
   pattern is a recognized, unsolved privacy gap (SnoopGuard, USENIX Security 2025):
   the bot necessarily receives ALL group plaintext, and "no platforms successfully
   combine" robust E2EE with effective limits on chatbot access.

## Left genuinely open (not resolved by this pass)

- **Legal crux UNANSWERED** — "chosen inability vs genuine inability" surfaced no case
  law either way. Now *moot* at the sovereign-solo intersection: the technical
  inability isn't real, so there's no chosen-inability to litigate.
- **Nitro Enclaves / ARM CCA unassessed.** The interposer attacks target
  SEV-SNP/TDX/SGX. Nitro's design (root of trust = AWS, not the self-hoster; no
  operator SSH into the enclave) MIGHT resist the physical vector — but only by moving
  trust to AWS, i.e. abandoning self-hosted sovereignty. Not a rescue; a different
  product.
- **"The machine knew" false-negative liability** — posed, unaddressed.
- **Prompt-injection of the moderating LLM specifically** — unaddressed.
