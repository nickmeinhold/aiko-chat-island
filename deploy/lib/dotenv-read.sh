#!/usr/bin/env bash
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

# dotenv_read <file> <key> — echoes the value, or nothing if absent/unreadable.
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
