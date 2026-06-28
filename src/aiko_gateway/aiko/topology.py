"""Pure parsing of aiko's `channel_list` EC share — the bus topology wire format.

This is the topology sibling of `payload.py`: both decode an aiko-bus wire shape
and depend on NOTHING heavy (no models, no db, no engine). Keeping it here, in
the bus package, lets the bus `client.py` map an EC item name to a channel name
WITHOUT importing the domain layer — which would otherwise drag
`channels_service -> models -> db` and construct the async DB engine at import
just to reach a prefix-stripping string function (#7).

The DB-mutating half of channel reconcile (`upsert_channel` /
`hard_delete_channel`) stays in `domain/channels_service.py`, where the session
and models belong. Existence-on-the-bus (parse) and existence-in-the-DB
(reconcile) are now cleanly separated.
"""
from __future__ import annotations

CHANNEL_LIST_KEY = "channel_list"


def parse_channel_names(channel_list: dict | None) -> set[str]:
    """Extract channel names from the `channel_list` EC share subtree.

    Observed value shape (spike/probe_channel_list.py): each entry is a
    ServiceFilter-shaped tuple ``[['*', name, '*', '*', '*', []], 'None', 'None']``
    and the dict KEY is the name too. Prefer the structured name (robust to a
    channel name containing the EC path separator '.'), fall back to the key.
    """
    names: set[str] = set()
    for key, value in (channel_list or {}).items():
        name: str | None = None
        try:
            candidate = value[0][1]
            if isinstance(candidate, str) and candidate:
                name = candidate
        except (TypeError, IndexError, KeyError):
            name = None
        names.add(name or key)
    return names


def channel_name_from_item(item_name: str | None) -> str | None:
    """Map an EC share `item_name` to a channel name, or None if it is not a
    `channel_list` leaf. ``channel_list.general`` -> ``general``; a bare
    ``channel_list`` (the parent node) or any unrelated key -> None. Only the
    first prefix is stripped, so a channel name containing '.' survives."""
    prefix = f"{CHANNEL_LIST_KEY}."
    if item_name and item_name.startswith(prefix):
        return item_name[len(prefix):] or None
    return None
