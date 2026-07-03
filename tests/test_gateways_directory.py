"""Gateway directory via peer gossip (#1546) — service + endpoint tests.

The security-critical surface is `coerce_peer` (it validates UNTRUSTED entries from
gossip peers) and `PeerDirectory.merge` (self-immutability + the anti-spam cap).
These are SHAPE defenses only — the test-grade trust model (poisoning undefended)
is documented in peers_service; these tests pin the shape guarantees, not
authenticity.
"""
from __future__ import annotations

from aiko_gateway.domain.peers_service import (
    MAX_PEERS, GatewayPeer, PeerDirectory, build_directory_from_settings,
    coerce_peer, gossip_once,
)


# --- coerce_peer: the untrusted-entry validator ---------------------------- #

def test_coerce_accepts_well_formed_https_peer():
    p = coerce_peer({"id": "enspyr", "displayName": "Enspyr", "baseURL": "https://chat.enspyr.co"})
    assert p == GatewayPeer("enspyr", "Enspyr", "https://chat.enspyr.co")


def test_coerce_normalizes_trailing_slash():
    p = coerce_peer({"id": "x", "displayName": "X", "baseURL": "https://x.example/"})
    assert p is not None and p.base_url == "https://x.example"


def test_coerce_rejects_non_https_scheme():
    # The single most important check: base_url is a navigation target in the app.
    for bad in ["http://x.example", "javascript:alert(1)", "data:text/html,x",
                "ftp://x.example", "//x.example"]:
        assert coerce_peer({"id": "x", "displayName": "X", "baseURL": bad}) is None


def test_coerce_rejects_bad_id_and_overlong_fields():
    assert coerce_peer({"id": "Has Space", "displayName": "X", "baseURL": "https://x.example"}) is None
    assert coerce_peer({"id": "x", "displayName": "", "baseURL": "https://x.example"}) is None
    assert coerce_peer({"id": "x", "displayName": "n" * 65, "baseURL": "https://x.example"}) is None
    assert coerce_peer({"id": "-bad", "displayName": "X", "baseURL": "https://x.example"}) is None


def test_coerce_rejects_anchor_and_userinfo_evasions():
    # \Z anchor: an internal newline with an evil second line must not match via
    # the $-before-final-newline quirk.
    assert coerce_peer({"id": "x", "displayName": "X",
                        "baseURL": "https://ok.example\nhttps://evil.example"}) is None
    # userinfo phishing form: the visible host is real, the actual host is evil.
    assert coerce_peer({"id": "x", "displayName": "X",
                        "baseURL": "https://real.example@evil.example"}) is None


def test_coerce_rejects_non_string_and_missing_fields():
    assert coerce_peer("not a dict") is None
    assert coerce_peer({"id": "x", "displayName": "X"}) is None  # missing baseURL
    assert coerce_peer({"id": 1, "displayName": "X", "baseURL": "https://x.example"}) is None


def test_coerce_reads_snake_case_and_tolerates_legacy_camel():
    """Wire contract is snake_case (matches the app reader); camelCase still parses
    so a mixed-version gossip round with an old-build peer isn't dropped."""
    snake = coerce_peer({"id": "a", "display_name": "A", "base_url": "https://a.example"})
    camel = coerce_peer({"id": "a", "displayName": "A", "baseURL": "https://a.example"})
    assert snake == camel == GatewayPeer("a", "A", "https://a.example")


# --- PeerDirectory.merge: self-immutability + cap -------------------------- #

def _self() -> GatewayPeer:
    return GatewayPeer("home", "Home", "https://home.example")


def test_merge_adds_new_and_dedupes_by_id():
    d = PeerDirectory(_self())
    assert d.merge([{"id": "a", "displayName": "A", "baseURL": "https://a.example"}]) == 1
    # same id again → first-write-wins, not re-added (even with a different url).
    assert d.merge([{"id": "a", "displayName": "A2", "baseURL": "https://evil.example"}]) == 0
    ids = [p.id for p in d.known()]
    assert ids == ["a", "home"]  # sorted, self included
    a = next(p for p in d.known() if p.id == "a")
    assert a.base_url == "https://a.example"  # original kept, not the evil overwrite


def test_merge_never_overwrites_self():
    d = PeerDirectory(_self())
    # A hostile peer claims OUR id with its own url — must be ignored.
    assert d.merge([{"id": "home", "displayName": "Pwned", "baseURL": "https://evil.example"}]) == 0
    assert d.self_peer == _self()
    assert [p.base_url for p in d.known()] == ["https://home.example"]


def test_merge_rejects_self_by_url_and_duplicate_urls():
    """Self-immutability is by URL too, not just id (Carnot cage-match): a
    different-id alias of our own base_url must not become a second self-referential
    entry, and two ids pointing at the same gateway collapse to one."""
    d = PeerDirectory(_self())
    # different id, but OUR url → dropped (self-by-URL, not just self-by-id)
    assert d.merge([{"id": "notme", "display_name": "Not Me",
                     "base_url": "https://home.example"}]) == 0
    assert [p.id for p in d.known()] == ["home"]
    # first real peer accepted; a second id pointing at the SAME url is dropped
    assert d.merge([{"id": "a", "display_name": "A", "base_url": "https://a.example"}]) == 1
    assert d.merge([{"id": "a-alias", "display_name": "Alias",
                     "base_url": "https://a.example"}]) == 0
    assert sorted(p.id for p in d.known()) == ["a", "home"]


def test_merge_enforces_max_peers_cap():
    d = PeerDirectory(_self())
    flood = [{"id": f"p{i}", "displayName": f"P{i}", "baseURL": f"https://p{i}.example"}
             for i in range(MAX_PEERS + 50)]
    added = d.merge(flood)
    # self already occupies one slot, so at most MAX_PEERS-1 of the flood land.
    assert added <= MAX_PEERS - 1
    assert len(d.known()) <= MAX_PEERS


def test_merge_drops_malformed_without_failing():
    d = PeerDirectory(_self())
    added = d.merge([
        {"id": "ok", "displayName": "OK", "baseURL": "https://ok.example"},
        {"id": "bad", "displayName": "B", "baseURL": "http://insecure.example"},  # dropped
        "garbage",                                                                 # dropped
    ])
    assert added == 1
    assert sorted(p.id for p in d.known()) == ["home", "ok"]


# --- gossip_once: anti-entropy pull ---------------------------------------- #

class _FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


class _FakeClient:
    """Maps a URL → payload; raises for any URL not in the map (simulates a peer
    being down) so we also exercise the swallow-errors path."""
    def __init__(self, by_url): self._by_url = by_url
    async def get(self, url, timeout=None):
        if url not in self._by_url:
            raise RuntimeError("connection refused")
        return _FakeResp(self._by_url[url])


async def test_gossip_learns_peers_from_a_target_and_self_converges():
    d = PeerDirectory(_self(), bootstrap_urls=["https://seed.example"])
    # The bootstrap peer reports itself + one further peer → both should be learned.
    client = _FakeClient({
        "https://seed.example/v1/gateways": {"gateways": [
            {"id": "seed", "displayName": "Seed", "baseURL": "https://seed.example"},
            {"id": "far", "displayName": "Far", "baseURL": "https://far.example"},
        ]},
    })
    learned = await gossip_once(d, client)
    assert learned == 2
    assert sorted(p.id for p in d.known()) == ["far", "home", "seed"]


async def test_gossip_swallows_unreachable_peer():
    d = PeerDirectory(_self(), bootstrap_urls=["https://down.example"])
    client = _FakeClient({})  # every GET raises
    learned = await gossip_once(d, client)  # must not raise
    assert learned == 0
    assert [p.id for p in d.known()] == ["home"]  # only self


# --- build_directory_from_settings: self-id fallback ----------------------- #

class _S:
    gateway_base_url = "https://chat.imagineering.cc/"
    gateway_id = ""
    gateway_display_name = "Aiko"
    gateway_bootstrap_peers: list[str] = []
    gateway_seed_peers: list[dict] = []


def test_self_id_falls_back_to_host_when_unset():
    d = build_directory_from_settings(_S())
    assert d.self_peer is not None
    assert d.self_peer.id == "chat-imagineering-cc"  # host, dots→dashes, lowercased
    assert d.self_peer.base_url == "https://chat.imagineering.cc"  # slash normalized


def test_seed_peers_populate_directory_without_fetch():
    """Operator-curated seed peers land in the known set at construction — the safe,
    fetch-free alternative to gossip. A malformed seed is dropped, self is immutable."""
    class S(_S):
        gateway_seed_peers = [
            {"id": "enspyr", "display_name": "Enspyr", "base_url": "https://chat.enspyr.co"},
            {"id": "chat-imagineering-cc", "display_name": "Impersonator",
             "base_url": "https://evil.example"},                 # our own id → dropped
            {"id": "bad", "display_name": "B", "base_url": "http://insecure"},  # non-https → dropped
        ]
    d = build_directory_from_settings(S())
    ids = sorted(p.id for p in d.known())
    assert ids == ["chat-imagineering-cc", "enspyr"]  # self + the one valid seed
    # self entry was NOT overwritten by the impersonating seed
    assert d.self_peer is not None and d.self_peer.base_url == "https://chat.imagineering.cc"


# --- the endpoint contract (snake_case — the app-picker reader shape) ------- #

def test_endpoint_returns_self_and_merged_peers_in_contract_shape():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aiko_gateway.domain import peers_service
    from aiko_gateway.rest import gateways

    # Mutate the shared singleton the router reads (same object, not a rebind) with
    # a uniquely-id'd peer so we don't depend on / disturb other tests.
    peers_service.directory.merge(
        [{"id": "test-peer-xyz", "display_name": "Test Peer", "base_url": "https://tp.example"}])

    app = FastAPI()
    app.include_router(gateways.router)  # no lifespan → no aiko bus import
    body = TestClient(app).get("/v1/gateways").json()

    assert "gateways" in body and isinstance(body["gateways"], list)
    entry = next(g for g in body["gateways"] if g["id"] == "test-peer-xyz")
    # snake_case is the contract: these are the exact keys the app's ServerEntry
    # reader looks for (base_url / display_name). The old camelCase `baseURL` was
    # invisible to that reader and silently dropped every entry — regression guard.
    assert set(entry) == {"id", "display_name", "base_url"}
    assert entry == {"id": "test-peer-xyz", "display_name": "Test Peer",
                     "base_url": "https://tp.example"}
    # self entry is present too (built from settings at import).
    assert any(g["id"] for g in body["gateways"])


def test_endpoint_shape_matches_app_serverentry_reader_keys():
    """Pin the cross-repo contract: the app (aiko_chat_app ServerEntry.tryFromJson)
    reads the URL from base_url/baseUrl/httpBaseUrl/url and the name from
    name/display_name/displayName/label. Our emitted keys MUST hit that set — this
    is the assertion that would have caught the baseURL≠baseUrl break."""
    entry = GatewayPeer("x", "X", "https://x.example").to_public()
    app_url_keys = {"base_url", "baseUrl", "httpBaseUrl", "url"}
    app_name_keys = {"name", "display_name", "displayName", "label"}
    assert app_url_keys & entry.keys(), f"no URL key the app reads in {entry.keys()}"
    assert app_name_keys & entry.keys(), f"no name key the app reads in {entry.keys()}"
