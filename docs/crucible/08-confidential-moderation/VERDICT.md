# Verdict: CANDIDATE INVALIDATED at Heat (honest negative result)

*The crucible stopped at movement 2. No Cast / Fold / Temper / Blade — the melt
proved the ore is slag at the locked intersection, and polishing slag into a plan is
the exact failure this engine exists to prevent.*

## What was invalidated

The **operator-blind** confidential-moderation candidate — a TEE-hosted LLM moderator
on a **solo, self-hosted** island, such that **the operator themselves cannot read
content** — is not buildable as specified. The falsifier fired, and harder than the
one written in CRUCIBLE.md (which was the *legal* "chosen inability" question). The
kill is *technical* and upstream of the legal question.

## The three independent kills (any one near-fatal; all three at the locked intersection)

1. **Hardware:** a TEE does not protect a key from a motivated host with physical
   access. BadRAM (~$10) and TEE.Fail (<$1,000 interposer) extract keys and forge
   attestation on SEV-SNP/TDX; AMD and Intel place physical attacks out of scope. The
   operator owns the box → the operator is that attacker → "operator cannot read" is
   not a guarantee the hardware makes.
2. **Governance:** credible attestation is a large-org capability (Apple PCC), and
   self-hosting collapses non-targetability + independent audit (the operator is both
   the attested party and the attacker).
3. **Detection:** the oracle is independently unreliable AND colluding parties defeat
   it by layering their own encryption — "any detection scheme capable of detecting
   the encrypted content would break encryption generally." The targets evade; the
   false positives hit innocents.

## The transferable finding (folds into note 07 open question #6)

**"Content both confidential-from-a-motivated-adversary AND readable-by-a-moderator"
may be close to information-theoretically impossible, not merely hard.** Any moderator
that can read content is a break in the encryption the adversary can route around. The
enclave does not escape the plaintext-visibility tension in note 07 — it relocates
where plaintext briefly lives, and against a motivated operator it doesn't even do
that. So note 07's axis (plaintext-moderatable vs E2EE-shielded) is likely a *true*
dichotomy, not a line with a magic midpoint.

## What this does NOT claim (scope discipline)

- NOT "TEEs are useless" — they meaningfully defend against REMOTE attackers. The kill
  is specific to protection-from-the-host.
- NOT that a *cloud*-rooted variant (Nitro, where trust = AWS not the self-hoster) is
  impossible — but that variant abandons the sovereignty that made the candidate worth
  building. It's a different product, and it re-introduces the trusted third party the
  whole architecture exists to avoid.
- The escape routes each exit one locked constraint (self-hosted → cloud;
  operator-blind → plaintext-gateway; solo → large-org). None survives at
  solo × self-hosted × operator-blind.

## Honest process note

Author-instance forecast before Heat: "split — user-privacy win, legal unsettled."
Reality was a clean technical kill the forecast underweighted. The fire ran hotter
than predicted; the engine disposed of the heat, as designed. Cost of the negative
result: one research pass, zero build. That is the crucible earning its keep.
