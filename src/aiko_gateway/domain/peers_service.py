"""Island directory via peer gossip (#1546; wire taxonomy #1760) — the DECENTRALIZED
discovery layer, no central registry.

Each island advertises its known-island set over ``GET /v1/islands`` (the deprecated
``/v1/gateways`` alias survives the compat window) and converges by anti-entropy: a
background loop periodically pulls each known peer's set and merges it, so
newly-learned islands propagate transitively. No node is an authority — every island
speaks only for itself and what it has learned. The app's server picker calls
``GET /v1/islands`` on whatever island it's pointed at to replace its hardcoded
preset list. An entry is a peer ISLAND (the node); its ``base_url`` is that island's
GATEWAY edge (the protocol front door).

╔══════════════════════════════════════════════════════════════════════════════╗
║ TRUST MODEL — TEST-GRADE, POISONING UNDEFENDED. Read before relying on this. ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ Gossip distributes trust, which distributes the ATTACK: any island we gossip  ║
║ with can inject a peer entry — e.g. a baseURL labelled "Aiko Official"        ║
║ pointing at an attacker's credential-harvesting host. The picker would show   ║
║ it. A central directory would get trust "for free" (trust the one operator);  ║
║ gossip RELOCATES that to "how does a node decide a peer entry is authentic".  ║
║                                                                                ║
║ For the 2-island TEST this is an explicitly NAMED, accepted tradeoff. The     ║
║ only defenses here are SHAPE defenses (https-only, length caps, a hard size   ║
║ cap) — they bound blast radius, they do NOT establish authenticity. Before    ║
║ this is load-bearing in prod it needs an AUTHENTICITY mechanism (signed peer  ║
║ entries / operator-key allowlist / out-of-band verification) and a cage-match ║
║ on the injection family + the app's auth surface. Tracked: claude-tasks #1546.║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Iterable

from . import island_identity
from .island_mode import IslandMode

log = logging.getLogger("aiko_gateway.peers")

# Shape limits — blast-radius bounds, NOT authenticity (see the banner above).
MAX_PEERS = 200          # hard cap on the known set (anti-spam / unbounded growth)
MAX_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_URL_LEN = 255

# An island id is a short slug — lowercased alnum + dash. Constrained so it can't
# carry markup/control chars into the picker UI. \Z (not $) so a trailing newline
# can't satisfy the anchor (Python's $ matches before a final \n).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,%d}\Z" % (MAX_ID_LEN - 1))
# base_url MUST be https — it is a navigation target in the app; http/javascript:/
# data: are rejected outright. Host is a plain DNS name or IPv4, optional :port and
# path. This is the single most security-relevant validation in the file. \Z (not
# $) closes the trailing-newline-before-anchor hole; the host class excludes '@' so
# a userinfo phishing form (https://real@evil) cannot match.
_HTTPS_RE = re.compile(r"^https://[a-zA-Z0-9.-]+(:\d+)?(/[\w./~-]*)?\Z")


@dataclass(frozen=True)
class Island:
    id: str
    display_name: str
    base_url: str
    # The peer's VERIFIED moderation posture (crucible-09 A4), learned ONLY through
    # island_identity.admit_manifest (its signed /v1/island manifest) and written ONLY via
    # IslandDirectory.record_verified_mode. None until verified — the unsigned gossip path
    # (coerce_island/merge) NEVER sets it, so a forged directory entry can advertise a
    # base_url but never a trusted mode. Deliberately NOT emitted by to_public(): re-serving
    # an observed peer mode over our own /v1/islands would re-propagate it as OUR unsigned
    # claim (an authenticity smear); mode stays internal until a signed peer-directory lands.
    # Typed as the closed IslandMode vocabulary at REST (not a free str): the mutator converts
    # + fail-closes on any non-member, so a bad string can never be stored (Carnot + Tesla,
    # PR#112 cage-match: the SoT is IslandMode; the field and writer must honor it, not just
    # the verify path). SEMANTICS: last-verified-mode-within-window — see record_verified_mode.
    mode: IslandMode | None = None

    def to_public(self) -> dict:
        """The wire shape the app picker consumes: snake_case, matching the app's
        reader (base_url/display_name) AND this repo's house style. `id`/`display_name`
        are the island's identity; `base_url` is its gateway edge. The keys are shared
        by both /v1/islands and the deprecated /v1/gateways envelope (only the array
        key differs). (The original #1546 draft emitted camelCase `baseURL`, which the
        app's reader — keys base_url/baseUrl/httpBaseUrl/url — could not match, silently
        dropping every entry. coerce_island stays tolerant of the old keys so a
        mixed-version gossip round still parses.)"""
        return {"id": self.id, "display_name": self.display_name,
                "base_url": self.base_url}


def _normalize_base_url(raw: str) -> str:
    """Strip a single trailing slash so the same gateway isn't stored twice under
    ``…/`` and ``…`` (the gossip GET re-appends the path)."""
    return raw.rstrip("/")


def coerce_island(raw: object) -> Island | None:
    """Validate an untrusted peer entry (dict from a gossip response, or a
    Island) into a Island, or None if it fails any SHAPE check. Never
    raises — a malformed entry from a hostile/buggy peer is dropped, not fatal.

    SHAPE only: a valid-shaped entry is NOT an authentic one (see the banner)."""
    if isinstance(raw, Island):
        gid, name, url = raw.id, raw.display_name, raw.base_url
    elif isinstance(raw, dict):
        # snake_case is the wire contract; accept the legacy camelCase keys too so a
        # mixed-version gossip round (a peer still on the old build) still parses.
        gid = raw.get("id")
        name = raw.get("display_name", raw.get("displayName"))
        url = raw.get("base_url", raw.get("baseURL"))
    else:
        return None
    if not isinstance(gid, str) or not isinstance(name, str) or not isinstance(url, str):
        return None
    gid = gid.strip().lower()
    name = name.strip()
    url = _normalize_base_url(url.strip())
    if not _ID_RE.match(gid):
        return None
    if not name or len(name) > MAX_NAME_LEN:
        return None
    if len(url) > MAX_URL_LEN or not _HTTPS_RE.match(url):
        return None
    return Island(id=gid, display_name=name, base_url=url)


class IslandDirectory:
    """The known-peer set for THIS gateway. Self is always present (and immutable —
    a peer can never overwrite our own entry). Merge is first-write-wins with a
    hard size cap; no conflict resolution (test-grade)."""

    def __init__(self, self_peer: Island | None,
                 bootstrap_urls: Iterable[str] = (),
                 seed_peers: Iterable[object] = ()):
        self._self = self_peer
        self._peers: dict[str, Island] = {}
        if self_peer is not None:
            self._peers[self_peer.id] = self_peer
        # Operator-curated static peers: FULL entries merged with no network fetch.
        # Trusted-by-config (authentic by construction), so they populate the known
        # set directly — the safe alternative to gossip for a handful of islands.
        # merge() still shape-validates and protects self-immutability.
        self.merge(seed_peers)
        # Bootstrap URLs have no id/name until we gossip them, so they live as a
        # separate probe set; gossip_once GETs them and learns their self entry.
        self._bootstrap_urls: set[str] = {
            _normalize_base_url(u) for u in bootstrap_urls
            if isinstance(u, str) and _HTTPS_RE.match(_normalize_base_url(u.strip()))
        }

    @property
    def self_peer(self) -> Island | None:
        return self._self

    def is_self(self, peer: Island) -> bool:
        return self._self is not None and peer.id == self._self.id

    def known(self) -> list[Island]:
        """All known peers (incl. self), sorted by id for a stable response."""
        return sorted(self._peers.values(), key=lambda p: p.id)

    def merge(self, incoming: Iterable[object]) -> int:
        """Merge untrusted peer entries. Returns the count newly added. Drops:
        malformed shapes, our own id OR our own base_url (self is immutable by
        both), already-known ids or base_urls (one entry per gateway URL,
        first-write-wins), and anything past MAX_PEERS."""
        added = 0
        for raw in incoming:
            peer = coerce_island(raw)
            if peer is None:
                continue
            if self._self is not None and peer.id == self._self.id:
                continue  # never let a peer impersonate / overwrite us by id
            if peer.id in self._peers:
                continue  # first-write-wins; no conflict resolution (test-grade)
            # One entry per gateway URL. Self is already in _peers, so this rejects a
            # DIFFERENT-id alias of our own base_url (self-by-URL, not just self-by-id)
            # AND two ids pointing at the same gateway. Without it, id-only immutability
            # let {"id":"other","base_url":<self>} become a second self-referential
            # picker target — and a self-gossip target if gossip is on. (Carnot cage-match.)
            if any(peer.base_url == p.base_url for p in self._peers.values()):
                continue
            if len(self._peers) >= MAX_PEERS:
                log.warning("peer directory at MAX_PEERS=%d; dropping %s",
                            MAX_PEERS, peer.id)
                continue
            self._peers[peer.id] = peer
            added += 1
        return added

    def record_verified_mode(self, peer_id: str, mode: str, *,
                             expected_base_url: str) -> None:
        """Record a peer's moderation posture LEARNED THROUGH THE VERIFIED DOOR
        (island_identity.admit_manifest walked its signed /v1/island manifest). This is
        the ONLY writer of Island.mode — the unsigned gossip path (coerce_island/merge)
        never sets it, so a forged directory entry can advertise a base_url but never a
        trusted mode.

        ENDPOINT-PROVENANCE BINDING (Carnot + Tesla, PR#112 cage-match): the write lands
        ONLY on the peer whose stored ``base_url`` equals ``expected_base_url`` — the URL the
        manifest was actually fetched from. A signed manifest proves (id, base_url, mode)
        under SOME key but NOT that its ``id`` is the peer at that endpoint; without this
        guard a contacted host could sign a manifest naming ANOTHER peer's id and poison that
        peer's mode (every gossip target a mode-oracle against the whole known set). Binding
        by endpoint, not by the manifest's claimed id, closes that.

        ENUM SEAL: ``mode`` is validated against the closed IslandMode vocabulary and stored
        as an IslandMode member; a non-member (e.g. a future caller passing "plaintext-lol")
        is a fail-closed no-op, so single-door discipline is enforced at the writer, not left
        to caller convention.

        No-op if peer_id is unknown, is SELF (this island's own mode is config, not an
        observed posture), the stored peer's base_url != expected_base_url, or mode is not a
        known IslandMode. STICKY SEMANTICS: a recorded mode persists across later
        failed/stale/down rounds until the next successful admit overwrites it — i.e.
        last-verified-within-window, NOT continuous posture. Correct for the A4 declaration
        foundation; a consumer that treats it as live commitment (A5) must add a
        clear-on-failed-admit / re-admit policy first (tracked as an A5 follow-up)."""
        if self._self is not None and peer_id == self._self.id:
            return
        peer = self._peers.get(peer_id)
        if peer is None:
            return
        # Endpoint provenance: only the peer served FROM expected_base_url may have its mode
        # set from a manifest fetched there (base_url is unique per directory, so this pins
        # the write to exactly the contacted peer). Fail closed on mismatch.
        if peer.base_url != expected_base_url:
            return
        # Enum seal: reject anything outside the closed vocabulary rather than storing a raw
        # string. IslandMode(mode) raises ValueError on a non-member → fail-closed no-op.
        try:
            validated = IslandMode(mode)
        except ValueError:
            return
        self._peers[peer_id] = replace(peer, mode=validated)

    def gossip_targets(self) -> list[str]:
        """The base URLs to pull this round: every known non-self peer plus the
        bootstrap contacts (deduped). Bootstrap is how a fresh island converges
        before it knows anyone by id."""
        urls = {p.base_url for p in self._peers.values()
                if not (self._self and p.id == self._self.id)}
        urls |= self._bootstrap_urls
        return sorted(urls)


async def gossip_once(directory: IslandDirectory, client, *, timeout: float = 5.0) -> int:
    """One anti-entropy round: pull each target's island directory and merge.
    Returns the number of newly-learned peers. Per-target failures are swallowed (a
    peer being down must never break the loop). ``client`` is an httpx.AsyncClient.

    COMPAT WINDOW (#1760 wire taxonomy): prefer the canonical ``/v1/islands`` and
    fall back to the deprecated ``/v1/gateways`` so a new node still converges with a
    peer still on the pre-taxonomy build during rollout. Parse either envelope key
    (``islands`` | ``gateways``); merge/coerce_island already tolerate per-entry key
    drift."""
    learned = 0
    for base in directory.gossip_targets():
        # The ENTIRE per-target body — fetch-with-fallback, envelope extraction, AND
        # merge — is under one exception boundary: a peer being down OR returning a
        # hostile/malformed 200 (a JSON list, null, {"islands": null}) must be
        # dropped as a bad target, never abort the whole round (Carnot cage-match:
        # moving .get()/merge() outside the guard re-opened a one-peer gossip DoS).
        try:
            body = None
            for path in ("/v1/islands", "/v1/gateways"):
                url = f"{base}{path}"
                try:
                    resp = await client.get(url, timeout=timeout)
                    resp.raise_for_status()
                    body = resp.json()
                    break  # got a directory; don't try the older path
                except Exception as exc:  # noqa: BLE001 — fetch failed; try next path
                    log.debug("gossip pull failed for %s: %s", url, exc)
            if body is None:
                continue
            # Prefer the canonical `islands` envelope; fall back to the deprecated
            # `gateways` key. `or []` guards an explicit-null value from either key.
            entries = body.get("islands")
            if entries is None:
                entries = body.get("gateways")
            learned += directory.merge(entries or [])
            # A4 (crucible-09): additionally learn this peer's VERIFIED moderation posture
            # from its SIGNED self-manifest (GET /v1/island), walked through the fail-closed
            # verify-then-fresh admission door. The directory fetch above is UNSIGNED
            # discovery (who/where); mode is NEVER trusted from it — the signed manifest is
            # the authenticity channel. A manifest that is missing, forged, stale, or signed
            # for a DIFFERENT base_url binds no mode, and (isolated in its own guard) never
            # disturbs the discovery already merged above. NO filtering — Phase A has one
            # mode; this only records the observed posture (declaration, the A4 foundation).
            try:
                mresp = await client.get(f"{base}/v1/island", timeout=timeout)
                mresp.raise_for_status()
                manifest = mresp.json()
                mode = island_identity.admit_manifest(
                    manifest, now_ms=int(time.time() * 1000))
                # A non-None mode means admit_manifest verified a dict manifest, so manifest
                # is a dict here (no isinstance guard needed — Kelvin, PR#112 cage-match).
                # Bind by ENDPOINT PROVENANCE: the signed base_url must match the URL we
                # actually contacted, AND record_verified_mode additionally pins the write to
                # the peer stored at that base_url. Lowercase the signed id to match
                # coerce_island's id normalization, so an honest peer that signs a mixed-case
                # id (e.g. "Seed" vs stored "seed") is not a silent no-op (Tesla, PR#112).
                if mode is not None and \
                        _normalize_base_url(str(manifest.get("base_url", ""))) == base:
                    directory.record_verified_mode(
                        str(manifest.get("id", "")).strip().lower(), mode,
                        expected_base_url=base)
            except Exception as exc:  # noqa: BLE001 — a bad manifest never breaks discovery
                log.debug("gossip manifest verify skipped for %s: %s", base, exc)
        except Exception as exc:  # noqa: BLE001 — a bad target must never break the loop
            log.debug("gossip round dropped bad target %s: %s", base, exc)
    if learned:
        log.info("gossip: learned %d new peer(s); known=%d",
                 learned, len(directory.known()))
    return learned


def _host_of(base_url: str) -> str:
    """Derive a fallback gateway id from a base URL's host (so a deploy that sets
    gateway_base_url but not island_id still self-identifies). Lowercased, dots →
    dashes, to satisfy _ID_RE."""
    m = re.match(r"^https://([a-zA-Z0-9.-]+)", base_url.strip())
    if not m:
        return ""
    return m.group(1).lower().replace(".", "-")


def build_directory_from_settings(settings) -> IslandDirectory:
    """Construct the process-wide IslandDirectory from config. Self id falls back to
    the base-url host when island_id is unset.

    NAMED DEFERRAL (#1760): the self-identity config keys are still `gateway_*`
    (`GATEWAY_BASE_URL` / `GATEWAY_ID` / `GATEWAY_DISPLAY_NAME` / `GATEWAY_SEED_PEERS`),
    not `island_*`, even though they populate an Island. Renaming them is
    deploy-coupled — the env names are set in the compose files on both island boxes —
    so the rename waits for a coordinated deploy (with a `gateway_*` alias meanwhile),
    NOT this wire PR. Deliberate, not an oversight (Kelvin cage-match)."""
    base = _normalize_base_url(settings.gateway_base_url)
    gid = (settings.island_id or _host_of(base)).strip().lower()
    self_peer = coerce_island(
        {"id": gid, "displayName": settings.island_display_name, "baseURL": base})
    if self_peer is None:
        log.warning("could not build a valid self peer (id=%r base=%r); the "
                    "directory will advertise no self entry", gid, base)
    return IslandDirectory(self_peer, settings.gateway_bootstrap_peers,
                         seed_peers=settings.island_seed_peers)


# Process-wide singleton — built once from settings, shared by the REST route and
# the background gossip loop (started in main.lifespan).
from ..config import settings as _settings  # noqa: E402

directory = build_directory_from_settings(_settings)
