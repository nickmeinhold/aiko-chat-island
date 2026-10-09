## CarnotCodeCarver's Design Strike

**Verdict:** SOUND

**Summary:** SOUND. The engine is now small enough to justify its heat loss. Round 2's machine was a deploy subsystem; this is a narrow config shipper plus restore. No real engine matches Carnot, but this one is no longer burning a boiler to move one valve.

**Economy test:** Final call: it passes. One `ship`, one `restore`, three enumerated non-executable files, no pull, no scheduler, no image pin, no shipped deploy tooling: this is now a small control-side shipper plus restore. It is more than `.env`, but not more than the incident is worth, because the actual failed unit was not `.env`; it was `.env` plus the compose forwarding surface. The `.env`-only alternative loses here. It would protect secret drift while leaving the exact 2026-09-11 fracture plane open: value present, forwarding absent. The cohort-of-two claim is sound. `mosquitto.conf` is the only remaining eyebrow; it must either be justified as config coupled to compose or dropped, but its inclusion as an enumerated inert config file does not resurrect the fatal shape.

**Subtraction real?:** The subtraction is real, not cosmetic. Mechanism count actually fell: `deploy/**` cargo is gone, executable delivery is gone, denylist governance is gone, split ship commands are gone, digest-gated restore is gone, `local/` is gone, and the backup-path concern disappears because the shipper never runs `update.sh`. Some checks remain, but they are not moved machinery of the old design; they are the minimum invariants any config writer touching production secrets must carry: identity, drift refusal, mode-correct placement, review, and shredding. Dijkstra would approve the negative code: the most reliable component here is the one deleted.

**Fatal flaws:**


**What holds:**
- The `.env` plus `docker-compose.yml` cohort is the smallest unit that cannot reproduce the recorded outage shape.
- Moving `ISLAND_VERSION` out of the cohort is the decisive thermodynamic win: image change is no longer latent in config delivery, so the FATAL is unreachable by construction.
- Keeping `update.sh` box-resident and never invoking it from the shipper removes the backup-path surface instead of guarding it.
- Three enumerated files beats any include-minus-denylist rule. The next ambiguous path no longer defaults to cargo.
- The all-file-facts baseline/live comparison is the right drift predicate for placed files, and the half-applied state is no longer mislabeled as drift.
- Control-side compromise remains an accepted risk, not a fake mitigation. That honesty matters.

**Fold back:**
- Either justify `mosquitto.conf` in one sentence as part of the compose-consumed config cohort, or remove it from v1. Do not let symmetry become a smuggling route.
- Keep the prohibition on shipped executables absolute. The first returning script turns this back into the design I dissolved.
- Do not let `ISLAND_VERSION` leak back through another env key or compose substitution. Feynman's rule applies: the easiest person to fool is the one who renamed the coupling.
- Make the implementation prove the three-file manifest mechanically. Hamming's question is still live: what are the important problems, and why are you working on anything else?
