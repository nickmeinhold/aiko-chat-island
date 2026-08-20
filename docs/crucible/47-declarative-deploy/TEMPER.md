# TEMPER — cross-family strike on the declarative-deploy design (2026-07-28)

**Panel**: Kelvin (Gemini 3 Pro), Carnot (GPT/Codex), Tesla (Grok) — all **REQUEST_CHANGES**.
Wu (Kimi K3) dark-seated: killed by the 2-min shell cap mid-reasoning (not a verdict —
a coverage gap). Maxwell (orchestrator) concurs with the consensus. **3 of 4 adversaries
seated, unanimous — a decisive temper: the v1 casting cracked.**

## Consensus fatal flaws (2+ families, verified real)

| # | Flaw | Families | Disposition |
|---|------|----------|-------------|
| 1 | **Byte-match `.env` gate is NOT proof of a config no-op** — compose interpolation/precedence/defaults `${VAR:-x}`, "modulo ordering" already abandons byte-identity, and it covers only `.env` while first deploy ships a SET and runs `update.sh` (a stack recreate, not delivery-only). | K, C, T (unanimous) | **FOLD → v2**: subtract the gate. Compare the REAL object (decrypted complete `.env` vs live `.env`), and scope "no-op" to CONFIG (say `update.sh` still recreates). |
| 2 | **Multi-file staged `mv` is NOT atomic as a cohort** — `rename(2)` is per-inode; a crash leaves mixed-generation config. Tesla's kicker: **staging must be same-filesystem** — `/tmp`-staging → `mv` becomes copy+unlink, re-opening the truncation class. | K, C, T (unanimous) | **FOLD → v2**: `releases/<ts>/` cohort + ONE atomic `current` symlink flip; staging mandatorily under `REMOTE_PATH`. |
| 3 | **`sops -d` → shell env is an exfil surface** — `/proc/<pid>/environ`, `set -x`, child procs, argv; `trap` doesn't run on SIGKILL. | K, C, T | **FOLD → v2**: decrypt to a `umask 077` temp FILE only, never `export` into the shell; `set +x`. |
| 4 | **`source <island>.conf` is code execution** on the age-key host — a fat-fingered/hostile conf evals arbitrary shell. | C, T | **FOLD → v2**: parse as DATA — strict `KEY=VALUE` allowlist, validate `REMOTE_PATH`/`SSH_ALIAS`. |
| 5 | **Rollback is cohort-incomplete + health-blind** — independent `.bak`s recombine wrong generations; `update.sh` may itself be in the bad generation; `/health` passes on wrong-but-alive config (bad `PASSKEY_RP_ID`). | K, C, T | **FOLD → v2**: generation-addressed `restore <ts>` (symlink flip back); name what auto-rollback can't catch (silent misconfig, coupled image/migration). |
| 6 | **Shared-tenant blast > `REMOTE_PATH`** — confining WRITES ≠ confining EFFECTS; `docker compose` under `sudo -n docker` is daemon/root authority; no preflight that `REMOTE_PATH` is the intended island. | C, T | **FOLD → v2**: tenant preflight (verify island identity before overwrite); compose = privilege boundary (fixed project `aiko`, no prune/host-restart/external-net); cap `.bak`/releases retention (plaintext-secret-bearing). |
| 7 | **THE FALSIFIER partially bites — bad option-frame** — Fold's "full toolchain vs 3-line note" dichotomy missed the **subtractive middle**: store the *complete* encrypted `.env` per island (no template/render/envsubst/missing-var-map). "The artifact in git IS the artifact on the box." | C, T | **FOLD → v2 (structural)**: delete the render/template layer entirely. This is the biggest change and the best catch. |

## Verdict

v1 did **not** survive the strike. Re-cast to **v2** (below / in DESIGN.md) — the subtractive
shape the adversaries converged on. The ore is NOT slag: all three families affirmed the
core (truncation-class diagnosis correct, laptop-only age key correct, `update.sh` reuse
correct, whole-file same-FS replace is the right joint). What cracked was the *render layer*
and the *byte-match proof* — both now subtracted, not patched.

## Honest scope (post-temper-recast rule)

v2 is **adversary-originated** (Tesla designed the subtractive shape; Kelvin/Carnot converged
on the same fixes) — so it is not author-laundered. BUT the specific v2 assembly has not itself
been struck as a whole. Before build: either a confirming cross-family strike on v2, OR Nick's
review + the standing `/cage-match` on the built PR (trust-boundary-by-law). Do not mark v2
"battle-tested" on the strength of v1's strike.
