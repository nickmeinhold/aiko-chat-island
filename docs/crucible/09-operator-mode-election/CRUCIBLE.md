# Crucible: Operator mode election — E2EE xor Moderator

*Movement 1 (Ore). Candidate pre-selected by Nick 2026-07-30 in conversation, as the
resolution of note 07 open-question #6. Threat model is the INVERSE of the killed
crucible 08: not operator-BLIND (physics killed that), but operator-CHOOSES.*

## The pick

Each **island operator elects its island's mode**, embracing note 07's proven true
dichotomy instead of fighting it:

- **E2EE mode** — channels are client-encrypted; the gateway holds only ciphertext.
  Genuine inability → the encryption legal carve-out. No gateway moderation
  (takedown/retraction can't read the body); safety comes from **client-side reporting
  + metadata**, not server scanning.
- **Moderator mode** — plaintext gateway; the shipped #7 takedown/retraction machinery
  works. On the hook to scan, no shield. Report queue + CSAM runbook active.

## Why it's alive (and why it's the opposite of the last ore)

Crucible 08 (operator-blind enclave) died because it tried to have confidentiality AND
moderation at once — proven ~impossible. **This ore stops trying.** It exposes the two
ends of note 07's axis as a conscious operator choice. Sovereignty made concrete: the
node that carries the legal risk picks its own risk/capability tradeoff and owns it.
That is the aiko ethos ([[concept_sovereignty_scoped_moderation]]) as a config axis.

**Impact:** it's the first moderation story that doesn't fight physics, and it unblocks
the #25 E2EE roadmap item WITHOUT abandoning the shipped moderation — they just become
different islands' choices.

## The lucky asymmetry (why this dodges the wall that killed 08)

08 needed hardware attestation to prove operator-blindness, and BadRAM/TEE.Fail broke
it. **This design needs no attestation:** client-side E2EE is self-verifying (the
client KNOWS it handed the server only ciphertext), and Moderator mode is trivially
honest (operator sees plaintext, nothing to prove). The dichotomy that killed 08 is
what makes 09 cheap.

## Claims to falsify (carry to Cast → Temper)

1. **The middle is genuinely killable.** "Moderator = commitment" — is there a coherent
   way to forbid the plaintext-but-not-moderating resting state, or does it leak back in
   (an operator who elects Moderator but does nothing)?
2. **Partial access doesn't poison the shield.** If per-channel ever lands, does the
   operator being able to decrypt SOME content weaken genuine-inability for the E2EE
   channels? (Per-island sidesteps this; verify.) Lawyer question, binds task #7.
3. **E2EE mode has a REAL safety story, not "no safety".** Client-side reporting with
   cryptographic message-franking (does aiko's signing envelope give franking for free?)
   must actually let a user report a specific message provably — else E2EE mode is a
   lawless haven, not a shielded one.
4. **Mode-aware federation is coherent**, not a combinatorial mess: what happens when a
   Moderator-island and an E2EE-island peer, or a channel spans both?
5. **The mode is legible to users** at the moment of speaking, not buried in server
   config — a mislabeled mode is the worst outcome.

## Prior art to mine in Heat

- **Matrix encrypted vs unencrypted rooms** = the per-room version of this election,
  deployed at scale. What are its KNOWN problems (user confusion over which rooms are
  encrypted, metadata leakage, bridge/bot leakage)?
- **Message franking** (Facebook Messenger's abuse-reporting-in-E2EE scheme) — the
  safety mechanism for E2EE mode.
- **MLS (RFC 9420)** as the group-E2EE substrate — fit + known bot/large-group issues.

## Bindings

Note 07 (resolves open-q #6) · crucible 08 (the inverse, invalidated) ·
[[concept_confidential_xor_moderatable]] (the dichotomy this embraces) ·
[[concept_sovereignty_scoped_moderation]] · [[concept_add_remove_asymmetry_never_hide_a_hide]]
(retraction = the Moderator-mode primitive) · `aiko_chat_app` #11/#25 (E2EE roadmap) ·
task #7 (legal).
