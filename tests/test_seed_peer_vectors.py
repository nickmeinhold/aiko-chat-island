"""The gateway's seed-peer rules, pinned against the vectors the Dart tool uses.

`island standup` and `island configure` (deploy/dart) refuse a seed peer the gateway
would drop or rewrite, because the gateway does both silently: a dropped peer is a
green health check and an island missing from the picker. Those rules are written
twice, in two languages. This file and `deploy/dart/test/configure_test.dart` read
the same vectors, so changing either side's rules turns one of them red.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiko_gateway.domain.peers_service import coerce_island

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1]
     / "deploy/dart/test/fixtures/seed_peers.json").read_text()
)["vectors"]


@pytest.mark.parametrize("v", VECTORS, ids=[v["why"] for v in VECTORS])
def test_coerce_island_matches_the_vector(v):
    entry = v["entry"]
    got = coerce_island(entry)
    if v["python"] == "drop":
        assert got is None
        return
    assert got is not None
    as_written = (entry.get("id"), entry.get("display_name"), entry.get("base_url"))
    if v["python"] == "keep":
        assert (got.id, got.display_name, got.base_url) == as_written
    else:
        assert (got.id, got.display_name, got.base_url) != as_written
