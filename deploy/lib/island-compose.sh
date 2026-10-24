#!/usr/bin/env bash
# ONE way to run compose for an island. Source it; do not copy it.
#
# WHY THIS FILE EXISTS (claude-tasks#5165, design 18 bootstrap step 1). The image an
# island runs used to be `ghcr.io/…:${ISLAND_VERSION:-edge}`, with ISLAND_VERSION in
# the SOPS-rendered `.env`. Two costs, both measured:
#   - every release changed a SECRETS file for a value that is not a secret, so each
#     one needed a Touch ID re-lift PR (#194, #196, #397), and a reviewer approving an
#     opaque blob for the version line learns to approve opaque blobs;
#   - `:-edge` meant a pin that went missing became "track main", silently. Design 16's
#     empty-pin trap; design 18 r1's fold `${ISLAND_IMAGE:?}`.
#
# So the pin now lives in a box-local `image.env` the operator owns (and design 18's
# watcher later writes), holding ONE full image reference:
#     ISLAND_IMAGE=ghcr.io/nickmeinhold/aiko-chat-island:0.17.1
# A full reference rather than a version, because a digest (`repo@sha256:…`) cannot be
# spelled through a `repo:${TAG}` template, and design 18 pins digests.
#
# THE PRICE, MEASURED on enspyr-syd 2026-10-24: Compose reads `.env` by itself and
# NOTHING else, and passing any `--env-file` stops it reading `.env` at all. So a bare
# `docker compose ps` can no longer see the pin, and with `${ISLAND_IMAGE:?}` it now
# refuses (exit 1, naming this file's wrapper) instead of quietly resolving `edge`.
# Every compose call therefore goes through island_compose below, or deploy/compose.sh
# for a human.
#
# WHAT THE REFUSAL DOES NOT COVER, stated rather than implied (Tesla, PR#401 r2): `:?`
# fires only when ISLAND_IMAGE is UNSET. A bare `docker compose` run with ISLAND_IMAGE
# exported in the shell resolves that value, because the shell outranks every env file.
# That takes a deliberate export of the new name; it is an explicit override, not a
# forgotten pin. Every door in THIS file refuses it (island_require_pin). Closing the bare
# door too would need a base compose file no variable can satisfy, plus a generated
# override (Tesla's proposal), a larger change deferred to design 18's watcher work.
#
# Requires (set by the caller): ISLAND_ROOT (the directory holding docker-compose.yml)
# and DOCKER ("docker" or "sudo -n docker"; island_docker_detect sets it).

ISLAND_REPO="ghcr.io/nickmeinhold/aiko-chat-island"

# The pin file island_compose reads. Set ONLY by island_pin_begin, inside this process.
# Cleared on source so an exported value can never redirect the pin to another file: an
# ambient selector is the same class as an ambient pin (Noether, PR#401 r2).
unset _island_pin_file

# Route every docker call through $DOCKER. Some hosts run the deploy user OUTSIDE the
# docker group (enspyr-melb: user 'ubuntu' needs `sudo -n docker`), others are
# in-group (enspyr-syd). `sudo -n` is non-interactive, so a host needing elevation
# without passwordless sudo fails closed with a clear message. Returns 1 (does not
# exit) so a sourcing script keeps its own die().
island_docker_detect() {
  DOCKER="docker"
  if ! docker ps >/dev/null 2>&1; then
    if sudo -n docker ps >/dev/null 2>&1; then
      DOCKER="sudo -n docker"
    else
      echo "docker needs elevation on this host but 'sudo -n docker' failed — add the deploy user to the 'docker' group, or configure passwordless sudo for docker" >&2
      return 1
    fi
  fi
}

# EVERY DOOR'S PRECONDITION: the pin comes from image.env, and from nothing else.
#
# The class, enumerated once (PR#401 r2: three seats found three members of it, one door
# at a time): what can steer the image besides image.env's one line?
#   - ISLAND_IMAGE / ISLAND_VERSION exported in the shell  -> refused (stripped below
#     anyway; a stripped habit that silently does nothing must say so)
#   - an ISLAND_IMAGE line in .env (the secrets file)       -> refused: it would satisfy a
#     bare compose's `:?` and outlive image.env's edits
#   - any OTHER line in image.env                           -> refused: image.env is passed
#     AFTER .env, so a `JWT_SECRET=` there would override the SOPS value (Tesla, r2)
#   - the pin file's path from the environment              -> impossible: see unset above
#   - the live file read more than once per run             -> island_pin_begin
# Returns 1 with the reason on stderr; the caller dies.
# Separate from island_require_pin so a door that WRITES image.env can refuse before it
# writes anything: standup chose `edge`, wrote it, and only then found the export, so the
# re-run kept the pin it should never have written (Tesla, PR#401 r3).
island_refuse_ambient_pin() {
  local v
  for v in ISLAND_IMAGE ISLAND_VERSION; do
    if [ -n "${!v+set}" ]; then
      echo "$v is set in this shell's environment. It is ignored: the image comes only from ${ISLAND_ROOT:?}/image.env. Unset it (or edit that file instead) and re-run." >&2
      return 1
    fi
  done
}

island_require_pin() {
  local f="${ISLAND_ROOT:?}/image.env" bad
  island_refuse_ambient_pin || return 1
  if [ ! -r "$f" ]; then
    {
      echo "no readable $f — the image this island runs is pinned there (claude-tasks#5165)."
      echo "  Create it with the release you mean to run, e.g.:"
      echo "    printf 'ISLAND_IMAGE=$ISLAND_REPO:%s\\n' 0.17.1 > $f"
      if [ -f "$ISLAND_ROOT/.env" ] && LC_ALL=C grep -aqE '^[[:space:]]*(export[[:space:]]+)?ISLAND_VERSION[[:space:]]*=' "$ISLAND_ROOT/.env"; then
        echo "  This box's .env still carries an ISLAND_VERSION line. It is no longer read;"
        echo "  it leaves .env at the next SOPS re-lift."
      fi
    } >&2
    return 1
  fi
  # A crude shape check, not a parser: every non-blank, non-comment line must assign
  # ISLAND_IMAGE, and there must be exactly one. Compose stays the only reader of the VALUE.
  bad="$(LC_ALL=C grep -avE '^[[:space:]]*(#|$)' "$f" | LC_ALL=C grep -avE '^[[:space:]]*(export[[:space:]]+)?ISLAND_IMAGE[[:space:]]*=' || true)"
  if [ -n "$bad" ]; then
    printf '%s may hold ONLY the ISLAND_IMAGE line. It is passed after .env, so any other key here overrides the secrets file. Move these out:\n%s\n' "$f" "$bad" >&2
    return 1
  fi
  if [ "$(LC_ALL=C grep -acE '^[[:space:]]*(export[[:space:]]+)?ISLAND_IMAGE[[:space:]]*=' "$f" || true)" != 1 ]; then
    echo "$f must assign ISLAND_IMAGE exactly once." >&2
    return 1
  fi
  # A literal, never an expression: `…:${RELEASE_TAG}` would be resolved from .env on EVERY
  # compose call, so the snapshot would freeze the expression and not the image (Carnot, r3).
  if LC_ALL=C grep -aq '\$' "$f"; then
    echo "$f must hold a literal image reference; it contains '\$' (interpolation would read .env on every compose call)." >&2
    return 1
  fi
  if [ -f "$ISLAND_ROOT/.env" ] && LC_ALL=C grep -aqE '^[[:space:]]*(export[[:space:]]+)?ISLAND_IMAGE[[:space:]]*=' "$ISLAND_ROOT/.env"; then
    echo "$ISLAND_ROOT/.env carries an ISLAND_IMAGE line. The pin lives in image.env only; a copy in .env would satisfy a bare docker compose and survive edits to image.env. Remove it." >&2
    return 1
  fi
}

# The human door passes its raw arguments, so a leading global option would sit AFTER the
# pinned ones and win: `--env-file other.env` would override image.env and the secrets,
# `-f other.yml` would merge another file over the pinned one (Tesla, PR#401 r3). Global
# options belong to the wrapper; the caller supplies a subcommand first.
island_refuse_global_options() {
  case "${1:-}" in
    --build-overlay) ;;
    -*) echo "deploy/compose.sh takes a compose SUBCOMMAND first (ps, logs, up …); global option '$1' is refused: the wrapper owns -f/--env-file/--project-directory." >&2
        return 1 ;;
  esac
}

# usage: island_compose [--build-overlay] <compose subcommand and args…>
#
# `env -u`: the shell environment beats every env file in Compose's precedence
# (measured, PR#190 round 5), so an exported ISLAND_IMAGE would otherwise silently
# outrank image.env. ISLAND_VERSION is stripped too: nothing interpolates it any more,
# and a stale export should not be able to start meaning something again.
# `-f docker-compose.yml` is pinned for the reason update.sh gives: a bare invocation
# lets compose.yaml, an override file or COMPOSE_FILE choose the file set.
island_compose() {
  local files=(-f "${ISLAND_ROOT:?}/docker-compose.yml")
  if [ "${1:-}" = "--build-overlay" ]; then
    files+=(-f "$ISLAND_ROOT/docker-compose.build.yml")
    shift
  fi
  # shellcheck disable=SC2086  # $DOCKER is "docker" or "sudo -n docker", split on purpose
  env -u ISLAND_IMAGE -u ISLAND_VERSION ${DOCKER:?} compose "${files[@]}" \
    --env-file "$ISLAND_ROOT/.env" --env-file "${_island_pin_file:-$ISLAND_ROOT/image.env}" "$@"
}

# ONE PIN PER RUN. Copy image.env to a private file and point island_compose at it, so
# every compose call in one run (`config --images` for the drift guard, `pull`, `up`)
# resolves the SAME pin. Reading the live file per call made them separate resolutions:
# an edit mid-run, or design 18's watcher writing it, would have the guard certify one
# release while the script deployed another (Carnot, PR#401 r1, and r2 for the standup
# door the r1 fix missed). Every multi-call door uses this. Prints the snapshot path; the
# caller removes it (an EXIT trap).
island_pin_begin() {
  local snap
  snap="$(umask 077; mktemp "${TMPDIR:-/tmp}/aiko-image-env.XXXXXX")" || return 1
  cp "${ISLAND_ROOT:?}/image.env" "$snap" || { rm -f "$snap"; return 1; }
  _island_pin_file="$snap"
}

# Read `config --images` output on stdin; print the ONE release tag the drift guard
# should compare against, or explain on stderr and return 1.
#
# This is why the guard no longer parses the pin itself. It used to resolve the ref
# with its own reader (shell env first, then .env, then the `edge` default) and carry
# a crude-grep cross-check in case that reader disagreed with Compose. All of that was
# a second opinion about what Compose would do. `config --images` IS what Compose will
# do (it exists on both boxes' Compose, 5.1.0 and 2.40.3, measured), so the ref and the
# pull now come from one resolution.
#
# Refuses (1) when none of our images resolve, (2) when the services disagree, (3) on a
# digest pin or another repository: the guard compares against a git TAG, and neither
# names one. Those are design 18 step (b)'s to solve (the ref from the image's own
# metadata), and until then --skip-drift-check is the honest way through.
island_ref_from_images() {
  local ours n
  ours="$(LC_ALL=C grep -E "^${ISLAND_REPO//./\\.}[:@]" | LC_ALL=C sort -u)" || true
  n="$(printf '%s' "$ours" | grep -c . || true)"
  if [ "$n" -eq 0 ]; then
    echo "no service resolves to a $ISLAND_REPO image — image.env names something this guard cannot compare against a tag" >&2
    return 1
  fi
  if [ "$n" -gt 1 ]; then
    printf 'the island'"'"'s services resolve to DIFFERENT images, and they must share one:\n%s\n' "$ours" >&2
    return 1
  fi
  # ANY `@` is a digest: compose accepts both `repo@sha256:…` and `repo:tag@sha256:…`,
  # and the second must not reach the guard as a tag called `0.17.1@sha256:…` (Noether, r2).
  case "$ours" in
    *@*)
      echo "image.env pins a digest ($ours); the drift guard compares against a git tag and a digest does not name one" >&2
      return 1 ;;
  esac
  local tag="${ours#"$ISLAND_REPO":}"
  # An empty tag (`…aiko-chat-island:`) must be named as a bad PIN here, not reach the
  # guard as an empty argument and surface as its usage error (Noether, r3).
  if [ -z "$tag" ]; then
    echo "image.env names $ISLAND_REPO with an EMPTY tag ($ours). Write the release, e.g. ${ISLAND_REPO}:0.17.1" >&2
    return 1
  fi
  printf '%s\n' "$tag"
}
