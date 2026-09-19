"""Tests for layer extraction and the coverage block a status body ends with.

Run with `python tests/test_layers.py` from the repo root, or via `tests/run`.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import files, layers  # noqa: E402
from progress.files import FormatError  # noqa: E402

A = "a" * 40
H = "0" * 64

README = """# Roadmap: widgets

## What Mathlib already has

## The build, in layers

### Layer 0: the widget (Bourbaki I.2)
text
### Layer 1: gadgets
### Layer 2.5: gizmos — and more
## Worked examples
"""

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def raises(fn, needle=None):
    try:
        fn()
    except FormatError as exc:
        if needle and needle not in str(exc):
            raise AssertionError(f"wrong FormatError: expected {needle!r} in {str(exc)!r}") from None
        return
    raise AssertionError("expected a FormatError, none raised")


# ----- headings ---------------------------------------------------------------------------------


def test_headings_keep_order_lines_and_ids_and_drop_parentheticals():
    got = layers.headings(README)
    assert got == [
        {"id": "Layer 0", "title": "Layer 0: the widget", "line": 7},
        {"id": "Layer 1", "title": "Layer 1: gadgets", "line": 9},
        {"id": "Layer 2.5", "title": "Layer 2.5: gizmos — and more", "line": 10},
    ], got


def test_ids_stop_at_the_first_separator():
    assert layers.layer_id("Layer A, line bundles and divisors") == "Layer A"
    assert layers.layer_id("Lane G: grid homology") == "Lane G"
    assert layers.layer_id("L0A — sheaves of modules") == "L0A"
    assert layers.layer_id("S1: the twenty-six sporadic presentations") == "S1"


def test_worded_headings_hide_short_sub_labels_and_short_labels_need_a_separator():
    text = "## Part A — Hermite\n### A1: orthogonality\n### A2: the basis\n## Part B — Chebyshev\n### B1: x\n"
    assert [h["id"] for h in layers.headings(text)] == ["Part A", "Part B"]
    # `K3 surfaces` is a heading about a subject, not a layer label.
    assert [h["id"] for h in layers.headings("### K3 surfaces\n### L0: sheaves\n")] == ["L0"]


def test_bold_bullets_are_the_fallback_and_no_headings_means_no_layers():
    text = "## Layers\n\n- **L0 — the engine** (consumes X). Stuff.\n- **L1 — Montel.** More.\n"
    assert [(h["id"], h["line"]) for h in layers.headings(text)] == [("L0", 3), ("L1", 4)]
    assert layers.headings("# Roadmap\n\nprose only\n") == []


def test_readme_sha_is_the_text_hash():
    assert layers.readme_sha("x") == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"


# ----- the block --------------------------------------------------------------------------------

BLOCK = """Some prose about the roadmap.

More prose.

```coverage
Layer 0: partial — the fundamental identity and Weil reciprocity
Layer 1: DONE
Layer 2.5: unassessed
```
"""


def test_split_block_separates_prose_from_entries():
    prose, entries = layers.split_block(BLOCK)
    assert prose == "Some prose about the roadmap.\n\nMore prose.\n", repr(prose)
    assert entries == [
        {"id": "Layer 0", "state": "partial", "remaining": "the fundamental identity and Weil reciprocity"},
        {"id": "Layer 1", "state": "done", "remaining": ""},
        {"id": "Layer 2.5", "state": "unassessed", "remaining": ""},
    ], entries
    assert layers.split_block("prose only\n") == ("prose only\n", None)


def test_split_block_accepts_the_ascii_dashes_too():
    _, entries = layers.split_block("p\n\n```coverage\nLayer 0: done -- x\nLayer 1: done - y\n```\n")
    assert [e["remaining"] for e in entries] == ["x", "y"]


def test_split_block_refuses_a_buried_second_or_unclosed_block():
    raises(lambda: layers.split_block(BLOCK + "\nmore prose after the block\n"), "last thing")
    raises(lambda: layers.split_block(BLOCK + BLOCK), "more than one")
    raises(lambda: layers.split_block("p\n\n```coverage\nLayer 0: done\n"), "not closed")
    raises(lambda: layers.split_block("p\n\n```coverage\nLayer 0 done\n```\n"), "is not")


def test_coverage_binds_every_listed_layer_once_and_nothing_else():
    lay = layers.headings(README)
    _, entries = layers.split_block(BLOCK)
    cov = layers.coverage("Widgets", A, H, lay, entries)
    assert cov == {"roadmap": "Widgets", "to_sha": A, "readme_sha": H, "layers": [
        {"id": "Layer 0", "state": "partial", "remaining": "the fundamental identity and Weil reciprocity"},
        {"id": "Layer 1", "state": "done"},
        {"id": "Layer 2.5", "state": "unassessed"},
    ]}, cov
    # README order, whatever order the model wrote.
    reordered = list(reversed(entries))
    assert [e["id"] for e in layers.coverage("Widgets", A, H, lay, reordered)["layers"]] == ["Layer 0", "Layer 1", "Layer 2.5"]
    raises(lambda: layers.coverage("Widgets", A, H, lay, entries[:2]), "says nothing about")
    raises(lambda: layers.coverage("Widgets", A, H, lay, entries + [{"id": "Layer 9", "state": "done", "remaining": ""}]), "does not have")
    raises(lambda: layers.coverage("Widgets", A, H, lay, entries + [entries[0]]), "twice")
    bad = [dict(e) for e in entries]
    bad[1]["state"] = "soon"
    raises(lambda: layers.coverage("Widgets", A, H, lay, bad), "expected one of")
    bad = [dict(e) for e in entries]
    bad[0]["remaining"] = "closes the comment --> and hides the disclaimer"
    raises(lambda: layers.coverage("Widgets", A, H, lay, bad), "angle brackets")
    raises(lambda: layers.coverage("Widgets", "short", H, lay, entries), "40-character")
    raises(lambda: layers.coverage("Widgets", A, "short", lay, entries), "readme_sha")
    raises(lambda: layers.coverage("Widgets", A, None, lay, entries), "readme_sha")


def test_the_rendered_status_round_trips_the_coverage():
    lay = layers.headings(README)
    prose, entries = layers.split_block(BLOCK)
    cov = layers.coverage("Widgets", A, H, lay, entries)
    text = files.render_status("Widgets", A, "2026-09-01T00:00:00Z", prose, cov)
    parsed = files.parse_status(text)
    assert parsed["coverage"] == cov, parsed
    assert "```coverage" not in text
    # The header sits on the second line, part of the canonical prefix.
    lines = text.splitlines()
    assert lines[0].startswith("<!--tauceti-status:v1 ") and lines[1].startswith("<!--tauceti-coverage:v1 "), lines[:2]
    assert lines[2] == "# Status: Widgets"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("all tests passed")
