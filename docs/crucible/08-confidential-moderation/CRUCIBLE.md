# Crucible: Operator-blind confidential moderation (enclave LLM moderator)

*Movement 1 (Ore) — the enthusiasm case. Written post-consent. Candidate pre-selected
by Nick in conversation 2026-07-30; threat model locked. Heat/Cast/Fold/Temper/Blade
follow.*

## The pick

An **operator-blind LLM moderator**: a moderation model running inside a
hardware-attested TEE (enclave — AWS Nitro Enclaves / AMD SEV-SNP confidential VM /
Intel SGX). A per-channel key is born **inside** the enclave and never leaves. The
enclave joins an E2EE channel **as a member** (the "bot is a member with a keypair"
framing from `aiko_chat_app` design #11), receives plaintext **only inside the cage**,
runs the moderation policy, and emits **only signed verdicts** — `retract` / `allow` /
`report` (to NCMEC / AFP). Never the plaintext. Never the key. **Remote attestation**
proves to every client that the only code that will ever touch their plaintext is the
specific audited moderation code.

## The locked premise (do not re-litigate — build on it)

**Threat model: protect content FROM THE OPERATOR THEMSELVES.** The operator
deliberately builds a system in which they cannot read user messages (the Apple
Private Cloud Compute stance). This is the radical, load-bearing claim. It is the
selling point (users get confidentiality even from the person running their island)
and possibly the legal lever (an operator who *genuinely cannot* produce plaintext to
a warrant).

## Why this glows AND matters (heat + impact, kept separate from evidence)

Design note 07 drew the moderation axis as **binary**:

- **plaintext gateway** → moderatable (takedown/retraction work) + *scannable* → on the
  hook to scan, no legal carve-out.
- **E2EE** → *unmoderatable* (gateway can't read bodies) + legally shielded.

E2EE killed reporting; plaintext killed confidentiality. **The enclave might be the
only point on the axis that keeps both**: content confidential from every human
including the operator, AND automated lawful detection + reporting. If that holds, it
dissolves note 07's deepest open question (#6) rather than picking a side of it.

**What it would change (impact):** it lets a *sovereign, solo* island offer
strong-confidentiality chat WITHOUT becoming an unmoderated haven — the exact tension
that makes decentralized social either surveilled or lawless. That is a genuinely
different thing a user (and Nick) could have.

**Why it's alive (the recombination, named):** it is three things in Nick's world
snapping together into one object —
- [[concept_caged_decider_sealed_sender]] (split JUDGMENT from CAPABILITY), lifted from
  *process*-enforcement into *hardware*-enforcement;
- the **Veilid bot-as-member** (`aiko_chat_app` #11);
- **Apple PCC** confidential computing.

The bot-member and the caged-decider are the same object; the enclave is the cage made
of silicon instead of a Python boundary.

## The falsifier (the one thing that, if true, proves this is slag)

**If "chosen inability" (an enclave the operator deploys and controls) is legally and
ethically equivalent to "access"** — i.e. a court/regulator treats an
operator-deployed enclave identically to a plaintext gateway (same possession, same
scanning duty) — **then the operator sits in the same legal position as the plaintext
gateway, just more expensively, and the entire "third point on the axis" claim
collapses.** The enclave would buy a real *user-privacy* win (no human reads messages)
but ZERO *legal* movement, and note 07's fork stays a fork.

Secondary falsifier (independent): **if a solo operator cannot run credible
attestation governance** (reproducible builds + published audit + client-side
attestation verification — the thing that took Apple a large team), then even a
perfect enclave is untrustworthy in practice, because clients have no basis to believe
the attested code is honest. Attestation proves *which* code ran, never that it is
*good*.

## Claims to falsify (carried to Cast → Temper)

1. An operator-deployed TEE meaningfully changes the operator's legal posture vs a
   plaintext gateway (the primary falsifier, inverted).
2. "Keys can never leak" — really it's *economic, not absolute*; TEEs break. The claim
   worth defending is "cost-to-extract exceeds the value even for a CSAM-key target,"
   which must be argued against 2026 TEE state-of-the-art, not assumed.
3. A solo operator can bootstrap enough attestation governance that strangers trust the
   enclave with plaintext.
4. An LLM is a good-enough CSAM oracle that a false negative doesn't create *worse*
   ("the machine knew") liability than not scanning at all.
5. The enclave's E2EE-membership doesn't just relocate the plaintext-visibility problem
   to a new trust boundary ([[concept_lossy_trust_boundaries]] — is the enclave a real
   fix or a stand-in that leaks elsewhere?).

## Bindings

Design note 07 (open question #6 — this IS a candidate resolution) ·
[[concept_caged_decider_sealed_sender]] · [[concept_lossy_trust_boundaries]] ·
[[concept_sovereignty_scoped_moderation]] · `aiko_chat_app` design #11 (Veilid) ·
task #7 (legal thread, Matt Craven — the primary falsifier is a lawyer question).
