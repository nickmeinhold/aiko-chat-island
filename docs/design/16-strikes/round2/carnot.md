## CarnotCodeCarver's Design Strike

**Verdict:** DISSOLVE

**Summary:** No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. Round 2 fixes the named thermodynamic leaks, but the heat engine is now larger than the work it extracts. The design passes many correctness objections and fails Carnot's economy test: for a problem already guarded by `preflight-compose-drift.sh` on both live boxes as of 2026-09-21, the fold buys `.env` cohort delivery and fewer hand-sync misses at the cost of a privileged multi-command deploy system, secret-bearing release retention, provenance policy, docker-daemon interrogation, denylist governance, dual acknowledgements, adoption semantics, and a second key ceremony. Hamming's question applies: what is the simplest thing that could possibly work? Here it is still one manual `deploy/` sync per box plus the existing preflight, with a direct encrypted `.env` reconciliation procedure. The fold repaired the design by spending the justification. Dijkstra would call this not elegance but bookkeeping for complexity you invited in.

**Did the fold discharge round 1?:**
- 1. Backup inside pruned generation: DISCHARGED. The fold moves backups to `<REMOTE_PATH>/backups`, gates resolution in CI, protects `current` and running digest generations, and shreds plaintext `.env` before prune.
- 2. Shipping every island's ciphertext: DISCHARGED. The closed manifest denies `deploy/secrets/**`, `deploy/islands/**`, control-side verification, and the shipper itself; `caddy/` is explicitly out.
- 3. False dissolution / moved loop / key and checkout dependence: PARTIAL. The prose is now honest, adds a second age recipient, break-glass through generation placement, and provenance pinning. But compromise of the control side still reaches every island, and stale/privileged operator state remains a real operational dependency.
- 4. Atomicity, migration, restore, digest binding: DISCHARGED. The split into `ship-config`, `ship-release`, and digest-bounded `restore` correctly separates file intent from running state and refuses image rollback across schema-risk boundaries.
- 5. Drift predicate names wrong state: DISCHARGED. Baseline, target, and live are now distinct; refusal is only `live != baseline`, while `target - baseline` is intentional change.
- 6. Trusting `GENERATION.txt` and wrong-path adoption: DISCHARGED. Absence of `current` is no longer genesis, adoption is explicit, and docker daemon state plus live `DOMAIN` are queried on every ship.
- 7. Human compiler saturated by wall of diff: PARTIAL. Per-file hunks and separate executable acknowledgement are better, but this remains a human-factor weak point. A second prompt is not a proof of attention.
- 8. Local state nowhere to live: PARTIAL. `local/` plus manifest-hash refusal gives local state a home and catches edits, but it reintroduces a second state surface the design must continually explain and police.
- 9. Missing cutover gate: DISCHARGED. First flip now diffs decrypted SOPS bytes against live `.env` and refuses on mismatch.
- 10. Hygiene omissions: DISCHARGED. Mode-correct creation, collision refusal, local plaintext shredding, and retention hygiene are now specified.

**New or surviving fatal flaws:**
- The economy test fails. The design no longer resembles a small control-side shipper plus restore; it is a deployment subsystem. The added machinery is not accidental polish, it is required to make the subsystem safe enough to exist.
- The marginal gain is too narrow for the new trusted surface. The existing preflight already guards compose plus `deploy/` drift and ran clean on both live boxes on 2026-09-21. The remaining gap is `.env` plus avoiding a manual sync prelude; that does not justify centralizing decrypt authority and deployment authority into this much mechanism.
- The design's own mitigations are evidence against it. Denylist CI, provenance pinning, two acknowledgement classes, daemon interrogation, explicit adoption, `local/`, digest-bound restore, and second-recipient key policy are all reasonable locally, but together they show the solution has crossed from dissolving drift into operating a bespoke release manager.
- The denylist manifest is a governance treadmill. It is closed today only by enumeration; the next ambiguous directory repeats the judgment. Feynman's warning fits: what I cannot create, I do not understand. This design does not understand the set it ships except by continually curating exceptions.
- The two-acknowledgement gate is not strong enough to carry daemon-root executable delivery. It reduces accidental rubber-stamping, but it still asks a tired operator to be the compiler for privileged shell changes. That was barely acceptable for a one-line compose diff; it is not the right foundation for shipping `deploy/**` as executable cargo.

**What holds:**
- The round 1 specification bugs were mostly folded correctly. This is not a sloppy recast; it is an overgrown one.
- The cargo/vehicle insight remains true: push from the control side does kill design 15's box-resident fetcher loop.
- The subtractive secret shape still holds: complete encrypted `.env` per island, no template/render/envsubst layer, no byte-match proof.
- Digest-bound `ship-release` and restore refusal across digest boundaries are good design moves in isolation.
- Fail-closed outcomes, same-filesystem `mv -T`, and no scheduler are correct constraints.

**Fold back:**
- DISSOLVE design 16 as an ongoing deploy subsystem.
- Keep the existing `preflight-compose-drift.sh` as the compiler for public repo artifacts; it already addresses the incident class for compose and `deploy/`.
- Adopt the smallest remaining fix: document and script a manual per-box `deploy/` sync before release until the box has the preflight, plus a direct operator-run `.env` reconciliation from the encrypted per-island file. Do not ship all of `deploy/**` as a privileged cohort.
- If `.env` delivery must be improved, make that a narrow tool: decrypt one island's complete SOPS `.env`, compare to live `.env`, require explicit review, place it mode-correctly, shred temps, and stop. Do not combine it with executable deploy delivery.
- Preserve the useful pieces as constraints for any future attempt: no render layer, no box-resident fetcher, no scheduler, no image rollback, digest-bound releases, and restore must not pretend to undo migrations. But the present assembly should not proceed to build.
