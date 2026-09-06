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
need grep; need sed; need sort; need python3   # stdlib json only — NOT PyYAML (see below)

# --- the recipients this repo REQUIRES, read from committed policy, not hardcoded.
# NOT `mapfile` — that is bash 4+, and macOS ships bash 3.2, which is the shell
# that actually runs deploys here. A bash-4-ism would pass CI (ubuntu) and fail on
# the operator's laptop: green where it does not matter, red where it does.
# NO PyYAML. An earlier draft parsed .sops.yaml with `python3 -c "import yaml"`; CI
# installs only sops, so the gate depended on PyYAML happening to be on the runner
# image (it is, today — undeclared ambient state is not a dependency, it is a
# coincidence with a good track record). It failed CLOSED, but a gate whose failure
# mode is "the runner image changed" is instrumentation you cannot trust. The
# recipient list is a flat `- age1...` sequence; read it with the tools already
# required. Carnot's catch, cage-match round 2.
WANT=()
while IFS= read -r _r; do
  [ -n "$_r" ] && WANT+=("$_r")
done <<EOF_RECIPIENTS
$(sed -n 's/^[[:space:]]*-[[:space:]]*\(age1[a-z0-9]\{20,\}\)[[:space:]]*$/\1/p' "$POLICY")
EOF_RECIPIENTS
[ "${#WANT[@]}" -ge 2 ] || bad "$POLICY declares ${#WANT[@]} recipient(s); at least 2 with uncorrelated failure modes are required (#3976)"
echo "Policy requires ${#WANT[@]} recipients:"
for r in "${WANT[@]}"; do note "${r:0:16}…"; done

# CONTINUITY AGAINST main's POLICY — and read the honest scope before trusting it.
# The checks below read recipients from THIS checkout's .sops.yaml, so a PR that swaps
# both blobs AND rewrites the policy to match is self-consistent and goes green: the tree
# authenticates against itself (Tesla, cage-match round 3). This compares against main's
# recipient set instead.
#
# WHAT IT CANNOT DO, stated so nobody reads more into it: the recipient field is a plain
# STRING. Without a key, nothing here proves the DEK is really wrapped to those keys — a
# determined forger can write main's recipients into a fake envelope. This catches the
# careless and accidental case, not an attacker. Real assurance needs a decrypt, which CI
# has no key for by design.
if git rev-parse --verify -q origin/main >/dev/null 2>&1 && git show origin/main:"$POLICY" >/dev/null 2>&1; then
  MAIN_R=$(git show origin/main:"$POLICY" | sed -n 's/^[[:space:]]*-[[:space:]]*\(age1[a-z0-9]\{20,\}\)[[:space:]]*$/\1/p' | sort)
  HERE_R=$(printf '%s\n' "${WANT[@]}" | sort)
  if [ -n "$MAIN_R" ] && [ "$MAIN_R" != "$HERE_R" ]; then
    if [ "${ALLOW_RECIPIENT_CHANGE:-}" = "1" ]; then
      note "recipient set DIFFERS from origin/main — permitted by ALLOW_RECIPIENT_CHANGE=1"
    else
      bad "recipient set differs from origin/main's $POLICY. Rotation is rare and must be deliberate: re-run with ALLOW_RECIPIENT_CHANGE=1 and say why in the PR body. (This is a continuity check, not authentication — see the note above.)"
      diff <(printf '%s\n' "$MAIN_R") <(printf '%s\n' "$HERE_R") | sed 's/^/      /'
    fi
  else
    note "recipient set matches origin/main"
  fi
else
  note "origin/main not available — skipping the continuity check (local run)"
fi

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
    README.md|.gitignore|MANIFEST.txt) ;;
    *.env.sops) ;;
    *) bad "unexpected artifact in $SECRETS_DIR: $(basename "$entry") — only README.md, MANIFEST.txt, .gitignore and *.env.sops belong here. A misnamed secret would otherwise fall outside the glob below and be silently skipped." ;;
  esac
done

MANIFEST="$SECRETS_DIR/MANIFEST.txt"

FILES=("$SECRETS_DIR"/*.env.sops)
if [ "${#FILES[@]}" -eq 0 ]; then
  # SILENCE-READS-AS-SUCCESS, SIBLING CASE. An earlier revision exited 0 here with the
  # lullaby "valid only before the first island is lifted" — so a PR DELETING every
  # secrets file kept CI green and removed the only recovery copy the public repo exists
  # to hold. The rename case was mutation-tested and the class declared closed; deletion
  # of ALL matches was never tested. Confessing one instance is not naming the class.
  #
  # The manifest is the witness: if it names islands, those files MUST exist. Only a
  # genuinely un-lifted repo (no manifest sections) may pass with nothing to verify.
  if [ -f "$MANIFEST" ] && grep -qE '^\[[a-z0-9_-]+\]$' "$MANIFEST"; then
    for sec in $(grep -oE '^\[[a-z0-9_-]+\]$' "$MANIFEST" | tr -d '[]'); do
      bad "$MANIFEST names island [$sec] but $SECRETS_DIR/$sec.env.sops IS GONE — every encrypted island artifact has been removed"
    done
    exit 1
  fi
  echo "No *.env.sops found in $SECRETS_DIR and no island sections in MANIFEST — nothing to verify (a genuinely un-lifted repo)."
  exit "$FAIL"
fi

for f in "${FILES[@]}"; do
  echo; echo "== $f"

  # 1. STRUCTURAL envelope check, one parse. An earlier draft accepted a file merely
  #    CONTAINING the string `lastmodified` — a loose grep standing in for a shape
  #    assertion (Carnot round 2). Assert the fields that must exist for this to be a
  #    decryptable SOPS document at all.
  python3 - "$f" <<'PYCHK' || FAIL=1
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception as e:
    print(f"  FAIL: not parseable JSON — not a SOPS envelope ({e.__class__.__name__})"); sys.exit(1)
m=d.get("sops") or {}
missing=[k for k in ("age","mac","lastmodified","version") if k not in m] + ([] if "data" in d else ["data"])
if missing:
    print(f"  FAIL: SOPS envelope missing required field(s): {', '.join(missing)}"); sys.exit(1)
if not isinstance(m["age"], list) or not m["age"]:
    print("  FAIL: sops.age is empty — no recipient can decrypt this file"); sys.exit(1)
print(f"  valid SOPS envelope (v{m['version']}, {len(m['age'])} age recipient stanzas)")
PYCHK

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
  # Parse sops.age[].recipient from the SCHEMA, not a regex over the whole file. The
  # regex found no false positives today (measured: 0 matches inside the ENC payload),
  # but it could match an age1-shaped substring in base64 — a leaky calorimeter reading
  # the container instead of the contents, when the structured field is right there and
  # already parsed once above. Latent, not live; fixed anyway, because recipient-set
  # exactness is THE cleartext invariant CI can check. Carnot, cage-match round 3.
  FOUND=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for a in d.get('sops',{}).get('age',[]):
    r=a.get('recipient')
    if r: print(r)
" "$f" | sort -u)
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

# 6. MANIFEST coverage (key-free). The binary encoding means a diff cannot show WHICH
#    key changed, so MANIFEST.txt carries the key NAMES in cleartext to buy that review
#    surface back. CI cannot decrypt, so here we can only check the manifest exists and
#    has a section per island — the BINDING check is --deep below. Say that plainly
#    rather than letting a shallow pass read as a verified manifest.
if [ ! -f "$MANIFEST" ]; then
  bad "$MANIFEST missing — the only reviewable record of which keys exist"
else
  for f in "${FILES[@]}"; do
    isl=$(basename "$f" .env.sops)
    grep -q "^\[$isl\]$" "$MANIFEST" || bad "$MANIFEST has no [$isl] section — a secrets file with no manifest entry is unreviewable"
  done
  for sec in $(grep -oE '^\[[a-z0-9_-]+\]$' "$MANIFEST" | tr -d '[]'); do
    [ -f "$SECRETS_DIR/$sec.env.sops" ] || bad "$MANIFEST names [$sec] but $SECRETS_DIR/$sec.env.sops does not exist"
  done
  grep -qE '=' "$MANIFEST" && bad "$MANIFEST contains '=' — it must hold key NAMES only, never values"
  note "manifest covers every island (names only; binding check is --deep)"
fi

# 7. --deep: prove a real decrypt AND that the manifest tells the truth. Needs a key,
#    so it is opt-in and never runs in CI.
if [ "${1:-}" = "--deep" ]; then
  command -v sops >/dev/null 2>&1 || { echo "sops required for --deep" >&2; exit 2; }
  for f in "${FILES[@]}"; do
    isl=$(basename "$f" .env.sops)
    ACTUAL=$(sops -d "$f" | grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
             | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | sort)
    [ -n "$ACTUAL" ] || { bad "$f decrypted to ZERO assignments"; continue; }
    CLAIMED=$(sed -n "/^\[$isl\]$/,/^$/p" "$MANIFEST" | grep -E '^[A-Za-z_][A-Za-z0-9_]*$' | sort)
    if [ "$ACTUAL" = "$CLAIMED" ]; then
      note "$isl: $(printf '%s\n' "$ACTUAL" | grep -c .) keys, manifest matches exactly"
    else
      bad "$isl: MANIFEST DOES NOT MATCH the decrypted file — the reviewable record is lying"
      diff <(printf '%s\n' "$CLAIMED") <(printf '%s\n' "$ACTUAL") | sed 's/^/      /' | head -12
    fi
  done
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "OK — every committed secret carries every required recipient, with no plaintext."; else
  echo "FAILED — see above."; fi
exit "$FAIL"
