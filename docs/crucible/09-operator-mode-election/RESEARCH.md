# Heat / Research — operator mode election

*Movement 2 (Heat). Deep-research pass, 2026-07-30, adversarially verified (5 angles,
~20 sources, 24 claims verified, 1 refuted). Workflow wf_b3b475a5-4be.*

**Verdict: the ore HOLDS. Buildable on established primitives; Heat sharpened three
design requirements and left the legal axis as the one genuine open question.**

## Findings (all 3-0 verified unless noted)

1. **Matrix is deployed prior art for the election.** Encryption is elected PER ROOM
   (E2EE default for new private rooms, off for public). Its abuse thesis: *"the vast
   majority of abuse in public networks ... is visible from the public unencrypted
   domain"* and is handled by **decentralized reputation/publish-subscribe feeds, not
   by breaking E2EE.** This validates BOTH halves of the operator-mode design — the
   per-scope election AND "moderation via reputation, not decryption" (which is exactly
   note 07's mode-aware allow-list). Sources: matrix.org abuse blog (2020), Element,
   Matrix FAQ.

2. **E2EE mode is NOT a lawless haven — franking gives it a real report path.** Message
   franking (Grubbs/Lu/Ristenpart, CRYPTO 2017; shipped in Messenger Secret
   Conversations) lets a recipient VOLUNTARILY report a specific message and PROVE to
   the server it was genuinely sent, while the server sees nothing otherwise. Three
   guarantees: authenticity, confidentiality-absent-report, third-party deniability.

3. **CRITICAL: franking is a DISTINCT construction, NOT free from the signing envelope.**
   It needs a committing AE — `T_F = HMAC-SHA256(N_F, M)`. Ordinary fast AE (AES-GCM) is
   provably NON-committing and was exploitable in Messenger ("invisible salamanders,"
   Dodis et al. 2019 — a malicious sender crafts an image the recipient can't report).
   **Direct answer to falsifier #3: NO, per-message signing does not give franking for
   free.** (A signature over plaintext gives receiver→third-party attribution but at the
   cost of deniability — related, not identical.) → aiko's E2EE arm must build committing-AE
   franking as a specific mechanism.

4. **Group franking needs newer primitives with a metadata cost.** Asymmetric Message
   Franking (Tyagi et al., CRYPTO 2019) + Message Traceback (CCS 2019) give group-scale
   accountability while preserving deniability — but traceback leaks forwarding-path
   metadata (path/tree). Feasible, but a genuine privacy tradeoff to price in.

5. **MLS (RFC 9420, IETF Proposed Standard, Jul 2023) is the mature E2EE substrate** —
   groups from two to thousands, TreeKEM costs scaling as log(group size). The standard
   pick for E2EE mode.

6. **MLS structurally CONFIRMS the dichotomy.** A compromised Delivery Service still
   cannot read plaintext (no server-side moderation possible in E2EE mode); a moderation
   "bot" can only gain access by joining as a FULL member that sees all future messages —
   there is no partial/moderation-only view (the SnoopGuard bot-as-member gap). So
   E2EE-mode moderation = client-side franking/reporting ONLY, never a server or bot
   reading. (A stronger phrasing was refuted 1-2 as overreach vs the arch draft; the
   RFC-9420-grounded version survived 3-0.)

7. Signal's pairwise-fanout group model is the design contrast to MLS/Sender-Keys
   (O(n) per message vs a shared group key). MLS is the better substrate for scale.

## Open (carry to Temper / lawyer)

- **LEGAL (the one real open axis, 0 verified claims either way — genuinely unsettled):**
  does one operator running BOTH modes weaken the "genuine inability" defense for the
  E2EE islands? Does electing Moderator mode + holding plaintext create an affirmative
  duty making plaintext-but-unmoderated the WORST position? → Matt / task #7.
- **Per-room/per-island mixing footgun** — Matrix's mixed-mode security-UX critique
  (which rooms are safe, bridge/bot plaintext leakage, metadata leakage) was not
  captured in verified claims; a real granularity input.
- **Federated/multi-server MLS is still DRAFT-stage in 2026** — no finalized federation
  architecture. A load-bearing gap: single-island E2EE is buildable now; cross-island
  E2EE channels are blocked on federated-MLS maturity.
- **No prior art for per-OPERATOR (vs per-room) election** — Matrix is per-room; a true
  per-operator posture election may have no direct precedent (novel tail).
