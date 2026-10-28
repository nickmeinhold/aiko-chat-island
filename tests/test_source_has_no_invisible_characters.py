"""No Python source here carries a LITERAL invisible character.

A bidi override, zero-width space, RLM or ZWJ typed straight into a source file is
invisible to the person reading it: a test input that looks like `"zero width"`, a
`rstrip("")` that strips something, a docstring with a hole in it. Spell them as escapes
(`"\\u200d"`), which say what they are. Found 2026-10-28 while building the handle
repertoire (#435): three introduced by an editor that turned escapes into characters,
two older. Five instances is a class, so this is a check, not a reminder.
"""
from __future__ import annotations

import pathlib
import unicodedata

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCANNED = ("src", "tests", "alembic", "scripts")


def _invisible(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category in ("Cf", "Zl", "Zp") or (category == "Cc" and ch not in "\t\n\r")


def test_no_literal_invisible_characters_in_python_sources():
    offenders = []
    for top in _SCANNED:
        for path in sorted((_ROOT / top).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                found = sorted({f"U+{ord(c):04X}" for c in line if _invisible(c)})
                if found:
                    offenders.append(f"{path.relative_to(_ROOT)}:{number}: {', '.join(found)}")
    assert not offenders, (
        "literal invisible character(s) in source; write them as \\u escapes:\n  "
        + "\n  ".join(offenders))
