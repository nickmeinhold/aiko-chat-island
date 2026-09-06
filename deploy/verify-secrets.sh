#!/usr/bin/env bash
# Verify the committed encrypted island config — WITHOUT any private key.
#
# WHY THIS EXISTS (Carnot, cage-match on PR#166): every load-bearing proof for that
# PR — plaintext scans, recipient checks, round-trips — was run by hand and written
# into a PR body. "Entropy wins when checks live in memory." A proof that cannot be
# re-run is a claim, and the artifacts it guards sit in a PUBLIC repo permanently.
#
# Everything here reads SOPS's CLEARTEXT METADATA, so it needs no key and runs in CI
# on every push. It cannot prove the plaintext is correct — only a decrypt can, and
# that is `--deep`, opt-in, for someone holding a key.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR=deploy/secrets
POLICY=.sops.yaml
FAIL=0
note() { printf '  %s\n' "$*"; }
bad()  { printf '  FAIL: %s\n' "$*"; FAIL=1; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 2; }; }
# Every tool is checked, including the ones that only ever appear mid-pipe: a tool
# that dies mid-pipeline exits on the LAST command's status, which is success with
# empty output — the exact fail-open that cost this repo a near-miss on
# ISLAND_SIGNING_SEED (PR#163). Fail loudly at the top instead.
need grep; need sed; need sort; need python3

# --- the recipients this repo REQUIRES, read from committed policy, not hardcoded.
# NOT `mapfile` — that is bash 4+, and macOS ships bash 3.2, which is the shell
# that actually runs deploys here. A bash-4-ism would pass CI (ubuntu) and fail on
# the operator's laptop: green where it does not matter, red where it does.
WANT=()
while IFS= read -r _r; do
  [ -n "$_r" ] && WANT+=("$_r")
done <<EOF_RECIPIENTS
$(python3 -c "
import yaml
d=yaml.safe_load(open('$POLICY'))
for r in d['creation_rules'][0]['key_groups'][0]['age']: print(r)
")
EOF_RECIPIENTS
[ "${#WANT[@]}" -ge 2 ] || bad "$POLICY declares ${#WANT[@]} recipient(s); at least 2 with uncorrelated failure modes are required (#3976)"
echo "Policy requires ${#WANT[@]} recipients:"
for r in "${WANT[@]}"; do note "${r:0:16}…"; done

shopt -s nullglob

# ALLOWLIST THE DIRECTORY BEFORE GLOBBING FOR TARGETS.
# Mutation-proving this script caught it eating its own tail: renaming a secret
# back to the broken `*.enc.env` made the `*.env.sops` glob match NOTHING, so it
# printed "nothing to verify" and exited 0. A checker that goes silent when its
# targets disappear reports success for the state it exists to detect — the same
# silence-reads-as-success class every other arm here is written against.
# So: anything in this directory that is not an expected artifact is a FAILURE,
# whether or not it looks encrypted. A misnamed, moved or half-renamed secret can
# no longer hide by falling outside the glob.
for entry in "$SECRETS_DIR"/* "$SECRETS_DIR"/.[!.]*; do
  [ -e "$entry" ] || continue
  case "$(basename "$entry")" in
    README.md|.gitignore) ;;
    *.env.sops) ;;
    *) bad "unexpected artifact in $SECRETS_DIR: $(basename "$entry") — only README.md, .gitignore and *.env.sops belong here. A misnamed secret would otherwise fall outside the glob below and be silently skipped." ;;
  esac
done

FILES=("$SECRETS_DIR"/*.env.sops)
if [ "${#FILES[@]}" -eq 0 ]; then
  # Legitimately empty before the first island is lifted — but say so loudly, and
  # carry any allowlist failure above into the exit status rather than returning 0.
  echo "No *.env.sops found in $SECRETS_DIR — nothing to verify (valid only before the first island is lifted)."
  exit "$FAIL"
fi

for f in "${FILES[@]}"; do
  echo; echo "== $f"

  # 1. It must actually be a SOPS envelope, not a stray file with the right name.
  grep -q '"sops"' "$f" || grep -q 'lastmodified' "$f" || bad "no SOPS metadata — is this actually encrypted?"

  # 2. EVERY policy recipient must be present. This is the arm that goes red if a
  #    botched `sops updatekeys` silently drops the offline recovery key — which
  #    would be invisible until the day it is the only key left.
  for r in "${WANT[@]}"; do
    grep -q "$r" "$f" || bad "recipient ${r:0:16}… MISSING — this file cannot be decrypted by a required key"
  done
  note "all ${#WANT[@]} required recipients present"

  # 2b. AND NO OTHERS. Tesla's catch, cage-match PR#166: the recipient list is the
  #     ONE cleartext invariant left in a binary-encrypted file — the single line a
  #     reviewer can check without decrypting — and checking only that the required
  #     keys are PRESENT is blind to an ADDED one. An extra `age1…` is a silent third
  #     wrap (a work laptop, a CI runner, a stranger who already holds a key), it
  #     grants full plaintext, and in a binary blob it is invisible to every diff.
  #     Pin the set EXACTLY: present-and-only.
  FOUND=$(grep -oE 'age1[a-z0-9]{20,}' "$f" | sort -u)
  for got in $FOUND; do
    ok=0
    for r in "${WANT[@]}"; do [ "$got" = "$r" ] && ok=1; done
    [ "$ok" -eq 1 ] || bad "UNAUTHORISED RECIPIENT ${got:0:20}… — this key can decrypt the file and is not in $POLICY. A silent third wrap grants full plaintext and is invisible in a binary diff."
  done
  note "no recipients beyond policy ($(printf '%s\n' $FOUND | grep -c . ) found, ${#WANT[@]} allowed)"

  # 3. No plaintext secret markers. Cheap, and it is the failure that cannot be
  #    undone once pushed to a public repo.
  for pat in 'BEGIN PRIVATE KEY' 'BEGIN EC PRIVATE KEY' 'BEGIN RSA PRIVATE KEY' 'AGE-SECRET-KEY'; do
    ! grep -q "$pat" "$f" || bad "PLAINTEXT MARKER '$pat' present in ciphertext"
  done
  note "no plaintext key markers"

  # 4. Values must be encrypted, not merely wrapped. A SOPS file whose values are
  #    cleartext still carries valid metadata — presence of the envelope is not
  #    proof of encryption.
  python3 - "$f" <<'PY' || FAIL=1
import json,sys
d=json.load(open(sys.argv[1]))
blob=d.get("data","")
if not blob.startswith("ENC["):
    print(f"  FAIL: data is not an ENC[...] payload — values may be cleartext"); sys.exit(1)
print("  payload is ENC[...] encrypted")
PY
done

# 5. The rotation path must actually run. `sops updatekeys` reads the extension to
#    pick a parser, so a file named *.enc.env was parsed as dotenv and this command
#    FAILED — silently breaking the documented way to add or remove a recipient.
#    Nobody would have discovered that until the day they needed it most.
if command -v sops >/dev/null 2>&1; then
  for f in "${FILES[@]}"; do
    if ! printf 'n\n' | sops updatekeys "$f" >/dev/null 2>&1; then
      # `n` declines any change; a parse error still fails here, which is the point.
      bad "sops updatekeys cannot read $f — the documented rotation path is broken"
    fi
  done
  note "rotation path (sops updatekeys) parses every file"
else
  note "sops not installed — skipping the rotation-path check (metadata checks above still ran)"
fi

# 6. --deep: prove a real decrypt. Needs a key, so it is opt-in and never runs in CI.
if [ "${1:-}" = "--deep" ]; then
  command -v sops >/dev/null 2>&1 || { echo "sops required for --deep" >&2; exit 2; }
  for f in "${FILES[@]}"; do
    n=$(sops -d "$f" | grep -cE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' || true)
    [ "$n" -gt 0 ] && note "$f decrypts to $n assignments" || bad "$f decrypted to ZERO assignments"
  done
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "OK — every committed secret carries every required recipient, with no plaintext."; else
  echo "FAILED — see above."; fi
exit "$FAIL"
