"""No Python source here carries a LITERAL invisible character.

A bidi override, zero-width space, RLM or ZWJ typed straight into a source file is
invisible to the person reading it. A test string with a zero-width space between two
words reads as one with a plain space or none; an rstrip() of a ZWJ reads as an rstrip()
of the empty string. Spell them as escapes (backslash-u-200d), which say what they are.
(This docstring describes those cases in words on purpose: it is scanned too.)
Found 2026-10-28 while building #435: three introduced by an editor that turned escapes
into characters, two older. Five instances is a class, so this is a check, not a
reminder.
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
    offenders, scanned = [], 0
    for top in _SCANNED:
        # A missing directory would make this pass having read nothing: a check that
        # cannot report its own absence (Noether, cage-match #435).
        assert (_ROOT / top).is_dir(), f"{top}/ not found under {_ROOT}"
        for path in sorted((_ROOT / top).rglob("*.py")):
            scanned += 1
            # Walk the RAW text, counting "\n" ourselves. str.splitlines() also splits
            # on (and drops) U+2028, U+2029, form feed and vertical tab, which are
            # exactly some of the characters this looks for (Carnot, cage-match #435).
            line = 1
            for ch in path.read_text(encoding="utf-8"):
                if ch == "\n":
                    line += 1
                elif _invisible(ch):
                    offenders.append(f"{path.relative_to(_ROOT)}:{line}: U+{ord(ch):04X}")
    assert scanned > 100, f"only {scanned} files scanned; is the layout what this expects?"
    assert not offenders, (
        "literal invisible character(s) in source; write them as \\u escapes:\n  "
        + "\n  ".join(offenders))


def test_the_classifier_still_sees_what_it_is_for():
    """The scan's success value is identical to its disabled value: a typo in
    `_invisible` (say a misspelled category) would let every literal through with the
    suite green, because the tree holds none to catch (Noether, cage-match #435 r4).
    So the classifier is pinned on known cases, both ways. Written as code points, not
    escapes: an escape is exactly what an editor can turn into the character itself
    (it happened while writing this test, and the scan above caught it)."""
    invisible = (0x200B, 0x200D, 0x200F, 0x202E, 0x2066, 0xFEFF, 0x2028, 0x2029, 0x00, 0x0C)
    visible = (0x61, 0x5A, 0x30, 0x20, 0x09, 0x0A, 0x0D, 0xE9, 0x1F469)
    for cp in invisible:
        assert _invisible(chr(cp)), f"U+{cp:04X} must count as invisible"
    for cp in visible:
        assert not _invisible(chr(cp)), f"U+{cp:04X} must not count as invisible"
