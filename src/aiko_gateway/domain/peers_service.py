"""Island/gateway directory via peer gossip (#1546) — the DECENTRALIZED discovery
layer, no central registry.

Each gateway advertises a known-peer set over ``GET /v1/gateways`` and converges by
anti-entropy: a background loop periodically pulls each known peer's set and merges
it, so newly-learned peers propagate transitively. No node is an authority — every
gateway speaks only for itself and what it has learned. The app's server picker
calls ``GET /v1/gateways`` on whatever gateway it's pointed at to replace its
hardcoded preset list.

╔══════════════════════════════════════════════════════════════════════════════╗
║ TRUST MODEL — TEST-GRADE, POISONING UNDEFENDED. Read before relying on this. ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ Gossip distributes trust, which distributes the ATTACK: any gateway we gossip ║
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
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("aiko_gateway.peers")

# Shape limits — blast-radius bounds, NOT authenticity (see the banner above).
MAX_PEERS = 200          # hard cap on the known set (anti-spam / unbounded growth)
MAX_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_URL_LEN = 255

# A gateway id is a short slug — lowercased alnum + dash. Constrained so it can't
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
class GatewayPeer:
    id: str
    display_name: str
    base_url: str

    def to_public(self) -> dict:
        """The wire shape the app picker consumes: snake_case, matching the app's
        ServerEntry reader (base_url/display_name) AND this gateway's house style
        everywhere else. (The original #1546 draft emitted camelCase `baseURL`,
        which the app's reader — keys base_url/baseUrl/httpBaseUrl/url — could not
        match, silently dropping every entry. coerce_peer stays tolerant of the old
        keys so a mixed-version gossip round still parses.)"""
        return {"id": self.id, "display_name": self.display_name,
                "base_url": self.base_url}


def _normalize_base_url(raw: str) -> str:
    """Strip a single trailing slash so the same gateway isn't stored twice under
    ``…/`` and ``…`` (the gossip GET re-appends the path)."""
    return raw.rstrip("/")


def coerce_peer(raw: object) -> GatewayPeer | None:
    """Validate an untrusted peer entry (dict from a gossip response, or a
    GatewayPeer) into a GatewayPeer, or None if it fails any SHAPE check. Never
    raises — a malformed entry from a hostile/buggy peer is dropped, not fatal.

    SHAPE only: a valid-shaped entry is NOT an authentic one (see the banner)."""
    if isinstance(raw, GatewayPeer):
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
    return GatewayPeer(id=gid, display_name=name, base_url=url)


class PeerDirectory:
    """The known-peer set for THIS gateway. Self is always present (and immutable —
    a peer can never overwrite our own entry). Merge is first-write-wins with a
    hard size cap; no conflict resolution (test-grade)."""

    def __init__(self, self_peer: GatewayPeer | None,
                 bootstrap_urls: Iterable[str] = (),
                 seed_peers: Iterable[object] = ()):
        self._self = self_peer
        self._peers: dict[str, GatewayPeer] = {}
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
    def self_peer(self) -> GatewayPeer | None:
        return self._self

    def is_self(self, peer: GatewayPeer) -> bool:
        return self._self is not None and peer.id == self._self.id

    def known(self) -> list[GatewayPeer]:
        """All known peers (incl. self), sorted by id for a stable response."""
        return sorted(self._peers.values(), key=lambda p: p.id)

    def merge(self, incoming: Iterable[object]) -> int:
        """Merge untrusted peer entries. Returns the count newly added. Drops:
        malformed shapes, our own id (self is immutable), already-known ids
        (first-write-wins), and anything past MAX_PEERS."""
        added = 0
        for raw in incoming:
            peer = coerce_peer(raw)
            if peer is None:
                continue
            if self._self is not None and peer.id == self._self.id:
                continue  # never let a peer impersonate / overwrite us
            if peer.id in self._peers:
                continue  # first-write-wins; no conflict resolution (test-grade)
            if len(self._peers) >= MAX_PEERS:
                log.warning("peer directory at MAX_PEERS=%d; dropping %s",
                            MAX_PEERS, peer.id)
                continue
            self._peers[peer.id] = peer
            added += 1
        return added

    def gossip_targets(self) -> list[str]:
        """The base URLs to pull this round: every known non-self peer plus the
        bootstrap contacts (deduped). Bootstrap is how a fresh island converges
        before it knows anyone by id."""
        urls = {p.base_url for p in self._peers.values()
                if not (self._self and p.id == self._self.id)}
        urls |= self._bootstrap_urls
        return sorted(urls)


async def gossip_once(directory: PeerDirectory, client, *, timeout: float = 5.0) -> int:
    """One anti-entropy round: pull each target's /v1/gateways and merge. Returns
    the number of newly-learned peers. Per-target failures are swallowed (a peer
    being down must never break the loop). ``client`` is an httpx.AsyncClient."""
    learned = 0
    for base in directory.gossip_targets():
        url = f"{base}/v1/gateways"
        try:
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            learned += directory.merge(body.get("gateways", []))
        except Exception as exc:  # noqa: BLE001 — best-effort gossip, never fatal
            log.debug("gossip pull failed for %s: %s", url, exc)
    if learned:
        log.info("gossip: learned %d new peer(s); known=%d",
                 learned, len(directory.known()))
    return learned


def _host_of(base_url: str) -> str:
    """Derive a fallback gateway id from a base URL's host (so a deploy that sets
    gateway_base_url but not gateway_id still self-identifies). Lowercased, dots →
    dashes, to satisfy _ID_RE."""
    m = re.match(r"^https://([a-zA-Z0-9.-]+)", base_url.strip())
    if not m:
        return ""
    return m.group(1).lower().replace(".", "-")


def build_directory_from_settings(settings) -> PeerDirectory:
    """Construct the process-wide PeerDirectory from config. Self id falls back to
    the base-url host when gateway_id is unset."""
    base = _normalize_base_url(settings.gateway_base_url)
    gid = (settings.gateway_id or _host_of(base)).strip().lower()
    self_peer = coerce_peer(
        {"id": gid, "displayName": settings.gateway_display_name, "baseURL": base})
    if self_peer is None:
        log.warning("could not build a valid self peer (id=%r base=%r); the "
                    "directory will advertise no self entry", gid, base)
    return PeerDirectory(self_peer, settings.gateway_bootstrap_peers,
                         seed_peers=settings.gateway_seed_peers)


# Process-wide singleton — built once from settings, shared by the REST route and
# the background gossip loop (started in main.lifespan).
from ..config import settings as _settings  # noqa: E402

directory = build_directory_from_settings(_settings)
