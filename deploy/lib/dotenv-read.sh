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
