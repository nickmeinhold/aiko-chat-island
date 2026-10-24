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
# for a human. The refusal is the point: there is no longer a path to the compose file
# that skips the pin.
#
# Requires (set by the caller): ISLAND_ROOT (the directory holding docker-compose.yml)
# and DOCKER ("docker" or "sudo -n docker"; island_docker_detect sets it). Optional:
# ISLAND_IMAGE_ENV, the pin file to read instead of $ISLAND_ROOT/image.env. update.sh
# points it at a private SNAPSHOT so one run reads one value (see island_snapshot_image_env).

ISLAND_REPO="ghcr.io/nickmeinhold/aiko-chat-island"

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

# image.env must exist and be readable. Compose's own error for a missing --env-file
# names the file but not what to put in it; this one says both.
island_require_image_env() {
  local f="${ISLAND_ROOT:?}/image.env"
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
    --env-file "$ISLAND_ROOT/.env" --env-file "${ISLAND_IMAGE_ENV:-$ISLAND_ROOT/image.env}" "$@"
}

# Copy image.env to a private file and point island_compose at it, so every compose call
# in one run (`config --images` for the drift guard, then `pull`, then `up`) resolves the
# SAME pin. Reading the live file three times made them three resolutions: an edit during
# the backup or the confirmation prompt, or design 18's watcher writing it, would have the
# guard certify one release while the script deployed another (Carnot, PR#401 r1). Prints
# the snapshot path; the caller owns removing it.
island_snapshot_image_env() {
  local snap
  snap="$(umask 077; mktemp "${TMPDIR:-/tmp}/aiko-image-env.XXXXXX")" || return 1
  cp "${ISLAND_ROOT:?}/image.env" "$snap" || { rm -f "$snap"; return 1; }
  printf '%s\n' "$snap"
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
  case "$ours" in
    "$ISLAND_REPO@"*)
      echo "image.env pins a digest ($ours); the drift guard compares against a git tag and a digest does not name one" >&2
      return 1 ;;
  esac
  printf '%s\n' "${ours#"$ISLAND_REPO":}"
}
