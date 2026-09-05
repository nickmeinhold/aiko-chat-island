#!/usr/bin/env bash
# NOTE ON LOCATING THIS FILE: callers must resolve symlinks before dirname'ing
# ${BASH_SOURCE[0]} — bash reports the symlink path, not the target, so a script reached
# through a symlink (a ~/bin shortcut, a packaging step) would look for lib/ beside the
# LINK and die. Each caller does that resolution inline because a shared helper cannot be
# sourced before you have found the thing to source. Found by the author's own round-5
# pass; it fails loudly, but it is a coupling this PR introduced.
# ONE reader for a .env file. Source it; do not copy it.
#
# WHY THIS FILE EXISTS. Four copies of `grep -E "^KEY=" | cut -d= -f2-` existed across
# deploy/ (resolve-gateway-env.sh, resolve-media-env.sh, and twice in standup.sh). Every
# one of them treated A MATCHER MISS AS "NEVER RECORDED" — so any dotenv spelling the
# regex did not cover resolved to the convention default, silently, on a re-run the
# script's own header advertises as safe.
#
# Cage-match round 4 (Tesla) named the structural form: **non-match is not absence.**
# Every hole in the grep is a silent defederation or a silent lockout, and the worst
# instance was not in the script this PR was written for:
#
#   standup.sh read the existing JWT secret with `^JWT_SECRET=`. An `export JWT_SECRET=…`
#   or a leading space read as nothing, so standup MINTED A NEW SECRET — invalidating
#   every live session — under a header promising it never rotates one.
#
# Patching a fourth regex would have left the class open in three other places. So the
# grammar is written ONCE, here, and sourced. Callers get the same answer by construction
# rather than by four people remembering.
#
# THE GRAMMAR, matched and stripped by the same expression so they cannot drift:
#   [ws] [export ws] KEY [ws] = [ws] VALUE [ws] [CR]
# plus one matching pair of surrounding quotes, as python-dotenv strips.
#
# DECLARED DIVERGENCES from python-dotenv, measured and deliberate (see #3592, which is
# the real fix — one parser, not four): inline comments (`KEY=v  # why`) are read
# literally, and backslash escapes inside double quotes are not decoded. Neither shape is
# ever produced by the heredoc that writes these files. Parity is asserted as a PRODUCT
# over the grammar's axes in tests/test_resolve_gateway_env.py — adding an AXIS is how the
# class stays bounded; adding a row is how it stops being.

# dotenv_read <file> <key> — echoes the value, or nothing if the key is absent.
#
# IT CANNOT TELL YOU WHY IT IS EMPTY, and that is load-bearing for callers. An empty
# result means "no such key" OR "no such file" OR "file not readable" — and Carnot
# (round 5) found the consequence: in standup's JWT path, a PERMISSIONS ERROR silently
# became "mint a new secret", invalidating every live session. That is the same
# non-match-is-absence shape Tesla named in round 4, one layer up: unreadable ≡ absent.
#
# The fix is at the CALLER, deliberately, because only the caller knows whether absence is
# benign. A brand-new island legitimately has no JWT_SECRET; an unreadable .env is never
# benign. So `dotenv_read` stays a pure reader and standup.sh checks readability before
# treating absence as "first run" (see the existing_secret block there).
dotenv_read() {
  local v q _re="^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=[[:space:]]*"
  v=$([ -f "$1" ] && grep -E "$_re" "$1" 2>/dev/null | tail -n1 | sed -E "s/$_re//; s/[[:space:]]*\r?$//" || true)
  for q in '"' "'"; do
    if [ ${#v} -ge 2 ] && [ "${v#"$q"}" != "$v" ] && [ "${v%"$q"}" != "$v" ]; then
      v="${v#"$q"}"; v="${v%"$q"}"; break
    fi
  done
  printf '%s' "$v"
}

# dotenv_keys <file> — echoes every KEY the file assigns, one per line, deduplicated.
#
# ITS JOB IS TO OVER-APPROXIMATE, and that is the opposite of dotenv_read's job. The
# caller (standup.sh, comparing what it is about to WRITE against what it is about to
# DESTROY) has asymmetric costs: a key this misses is a key the comparison reports as
# SAFE TO DESTROY — silently, unrecoverably for a signing seed — while a spurious extra
# name only produces a refusal the operator can read and act on. So the key pattern here
# is deliberately WIDER than the shapes a real .env carries: anything that is not
# whitespace and not `=`, which admits `FOO.BAR` and `FOO-BAR`.
#
# It is NOT total, and the claim must not be made as though it were. A QUOTED key
# containing a space (`"FOO BAR"=x`) stops at the space and is never listed, and
# dotenv_read WOULD fetch it if asked — so that one shape is a silent miss rather than a
# safe over-list. Nothing this repo writes produces it and no live island carries one;
# it is recorded because an invariant stated absolutely is the kind the next editor
# trusts (Tesla, cage-match #159 round 2).
#
# NOT "the same expression as the reader", which an earlier revision of this comment
# claimed (Tesla, cage-match #159 — the prose over-claimed the code). dotenv_read
# INTERPOLATES the caller's key with no charset at all, so it will happily read a name
# this lister would never have to guess. There are two expressions with two purposes and
# opposite error costs; they are edited together, and the reader's is the narrow one only
# because its caller already knows the name it wants.
#
# A leading `#` is excluded so a commented-out `#FOO=bar` is not reported as a key — that
# is parity with python-dotenv rather than laxity, and it is the one place the widening
# stops.
#
# KNOWN LIMIT, stated rather than papered over: this is line-oriented and has no quote
# state, so a MULTILINE quoted value (python-dotenv accepts them; nothing this repo writes
# produces one) would contribute its continuation lines as phantom keys wherever they look
# like an assignment. That direction is safe — a phantom causes a refusal, never a
# deletion — but the refusal's advice must not tell an operator to delete a phantom, since
# that means editing the value body. Quote-aware parsing belongs with the merge design,
# not here.
#
# Absence and unreadability are DISTINGUISHED here, unlike in dotenv_read — a missing file
# is `return 0` (a brand-new island legitimately lists no keys) while an unreadable one
# propagates grep's >= 2. An earlier revision of this comment claimed they were
# deliberately indistinguishable, which was true of the first draft and false the moment
# the status handling landed: prose describing a previous version of its own function
# (Tesla, cage-match #159 round 2 — the second over-claim caught in this file).
dotenv_keys() {
  [ -f "$1" ] || return 0
  local _k='^[[:space:]]*(export[[:space:]]+)?([^=[:space:]#][^=[:space:]]*)[[:space:]]*='
  local _raw _rc
  # grep is run ALONE so its status survives. Down a pipeline the final `sort` returns 0
  # no matter how grep died, so an I/O error would read as "this file assigns nothing" —
  # the non-match-is-absence shape this whole file exists to kill, committed by the
  # function that enforces it. grep's contract: 0 = matched, 1 = no match (a legitimately
  # key-less file), >= 2 = a real error, which is the only one that must propagate.
  # -a: TREAT AS TEXT. Without it a single NUL anywhere in the file makes grep declare
  # "Binary file … matches" and emit NO LINES — so this function returns one nonsense
  # entry, or on a grep that instead exits 1, the EMPTY SET with a success status. The
  # caller then computes "nothing would be dropped" and rewrites the file. A NUL is not
  # exotic: a value pasted from a UTF-16 editor carries them. That is non-match-is-absence
  # at the one input that is not an ASCII line — the ghost this file was written to bury,
  # at the exact place it would cost a signing seed (Tesla, cage-match #159 round 2).
  #
  # LC_ALL=C: `sort` and `comm` must agree on collation, and in a UTF-8 locale a
  # collating element can make sort -u treat two DISTINCT key names as equal — which
  # silently drops one from the comparison, and a key missing from the HAVE side is a key
  # destroyed. Pinning C makes the ordering an ASCII byte order that nothing in the
  # environment can move.
  _raw="$(LC_ALL=C grep -aE "$_k" "$1")"; _rc=$?
  [ "$_rc" -le 1 ] || return "$_rc"
  [ -n "$_raw" ] || return 0
  printf '%s\n' "$_raw" | tr -d '\000' | LC_ALL=C sed -E "s/$_k.*/\2/" | LC_ALL=C sort -u
}
