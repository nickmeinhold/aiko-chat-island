# Aiko Chat Gateway container image.
#
# Bundles the two from-source aiko packages the gateway needs at runtime
# (aiko_services + aiko_chat — not published on PyPI in a 3.12-pinnable form),
# then installs the gateway on top. Mirrors the matrix/aiko-bridge image so the
# gateway speaks the SAME wire contract as the bridge it shares a bus with.
#
# IMPORTANT — wire-contract version lock: AIKO_CHAT_REF is pinned to the
# nickmeinhold fork commit that puts username+timestamp on the wire (Change B,
# == the contract #34 verified). It MUST match the bridge's AIKO_CHAT_REF
# (matrix/aiko-bridge/Dockerfile). Bump both together, never one alone.

# Base image pinned by DIGEST, not just the floating `3.12-slim` tag (#3). The
# deploy is a manual `docker compose up` that REBUILDS on the host (no registry/CI
# to consume an immutable app artifact — #18), so the one floating upstream input
# left was the base image: a rebuild months apart would otherwise pull a different
# base silently. The digest makes every rebuild byte-reproducible. The tag is kept
# alongside for human readability; the digest is what's resolved.
#
# TRADEOFF: this freezes base-OS security patches until a manual bump. Periodically
# (or on a base CVE) re-resolve and bump BOTH the tag and digest together:
#   docker buildx imagetools inspect python:3.12-slim   # -> Digest: sha256:...
# Resolved 2026-06-29 (manifest-list digest, multi-arch).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ARG AIKO_SERVICES_REF=a66424db76c5bf8f11adfed456cf3a135baf7494
ARG AIKO_CHAT_REPO=https://github.com/nickmeinhold/aiko_chat.git
ARG AIKO_CHAT_REF=3e4e822b65b7e222920642c420661fb0c1e93bb6

# git: fetch the aiko packages. curl: container HEALTHCHECK against /health.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/geekscape/aiko_services.git \
    && cd aiko_services && git checkout "$AIKO_SERVICES_REF" && pip install --no-cache-dir -e . \
    && cd /opt \
    && git clone "$AIKO_CHAT_REPO" aiko_chat \
    && cd aiko_chat && git checkout "$AIKO_CHAT_REF" && pip install --no-cache-dir -e .

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Migration assets (#14). alembic.ini + the alembic/ tree are read at boot by the
# entrypoint (`python -m aiko_gateway.migrate` -> alembic upgrade head). They are
# NOT part of the installed wheel (hatch packages only src/aiko_gateway), so copy
# them explicitly. entrypoint.sh sequences migrate-before-serve.
COPY alembic.ini ./
COPY alembic ./alembic
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Bind on all interfaces inside the container; the compose port publish keeps it
# private (127.0.0.1:8095 on the host). ENVIRONMENT is unset → defaults to
# "production" → the config.py fail-closed JWT guard is armed.
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8095/health || exit 1
# Entrypoint migrates to head (fail-closed), THEN execs uvicorn. The deploy is a
# manual `docker compose up -d` with no host orchestrator (#19), so the
# migrate-before-boot ordering must live in the image.
CMD ["./entrypoint.sh"]
