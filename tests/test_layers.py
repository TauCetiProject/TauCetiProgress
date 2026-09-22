"""Layer extraction and the coverage block a status body ends with.

Run with `python tests/test_layers.py` from the repo root, or via `tests/run`.
"""

import json
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

ENTRIES = [
    {"id": "Layer 0", "state": "partial", "remaining": "the fundamental identity and Weil reciprocity"},
    {"id": "Layer 1", "state": "done"},
    {"id": "Layer 2.5", "state": "unassessed"},
]
PROSE = "Some prose about the roadmap.\n\nMore prose.\n"
BLOCK = PROSE + "\n```coverage\n" + json.dumps(ENTRIES, indent=2) + "\n```\n"

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def raises(fn, needle=None, label=""):
    try:
        fn()
    except FormatError as exc:
        if needle and needle not in str(exc):
            raise AssertionError(f"{label}: wrong FormatError: expected {needle!r} in {str(exc)!r}") from None
        return
    raise AssertionError(f"{label}: expected a FormatError, none raised")


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


def test_heading_families_have_a_precedence():
    # Worded headings hide short sub-labels beneath them; a short label needs a separator.
    text = "## Part A — Hermite\n### A1: orthogonality\n### A2: the basis\n## Part B — Chebyshev\n### B1: x\n"
    assert [h["id"] for h in layers.headings(text)] == ["Part A", "Part B"]
    assert [h["id"] for h in layers.headings("### K3 surfaces\n### L0: sheaves\n")] == ["L0"]
    # Bold bullets are the fallback; no recognisable headings means no layers.
    text = "## Layers\n\n- **L0 — the engine** (consumes X). Stuff.\n- **L1 — Montel.** More.\n"
    assert [(h["id"], h["line"]) for h in layers.headings(text)] == [("L0", 3), ("L1", 4)]
    assert layers.headings("# Roadmap\n\nprose only\n") == []


def test_readme_sha_is_the_text_hash():
    assert layers.readme_sha("x") == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"


# ----- the block --------------------------------------------------------------------------------


def test_split_block_separates_prose_from_the_decoded_json():
    prose, entries = layers.split_block(BLOCK)
    assert prose == PROSE, repr(prose)
    assert entries == ENTRIES, entries
    assert layers.split_block("prose only\n") == ("prose only\n", None)


def test_split_block_refuses_a_block_it_cannot_take_whole():
    cases = [
        ("followed by prose", BLOCK + "\nmore prose after the block\n", "last thing"),
        ("two blocks", BLOCK + BLOCK, "more than one"),
        ("unclosed", PROSE + "\n```coverage\n[]\n", "not closed"),
        ("not JSON", PROSE + "\n```coverage\nLayer 0: done\n```\n", "not valid JSON"),
    ]
    for label, body, needle in cases:
        raises(lambda: layers.split_block(body), needle, label)


def test_coverage_binds_every_listed_layer_once_and_nothing_else():
    lay = layers.headings(README)
    cov = layers.coverage("Widgets", A, H, lay, ENTRIES)
    assert cov == {"roadmap": "Widgets", "to_sha": A, "readme_sha": H, "layers": ENTRIES}, cov
    # README order, whatever order the model wrote.
    got = layers.coverage("Widgets", A, H, lay, list(reversed(ENTRIES)))
    assert [e["id"] for e in got["layers"]] == ["Layer 0", "Layer 1", "Layer 2.5"]
    cases = [
        ("a layer left out", ENTRIES[:2], "says nothing about"),
        ("a layer the README lacks", ENTRIES + [{"id": "Layer 9", "state": "done"}], "does not have"),
        ("a layer twice", ENTRIES + [ENTRIES[0]], "twice"),
        ("not an array", {"Layer 0": "done"}, "non-empty list"),
        ("an entry that is not an object", ["Layer 0: done"], "must be an object"),
        ("an unknown key", [dict(ENTRIES[0], note="x")] + ENTRIES[1:], "unknown field"),
        ("an illegal state", [dict(ENTRIES[1], state="soon"), ENTRIES[0], ENTRIES[2]], "expected one of"),
        ("a note that closes the comment", [dict(ENTRIES[0], remaining="x --> y")] + ENTRIES[1:], "angle brackets"),
        ("an empty note", [dict(ENTRIES[1], remaining="")] + [ENTRIES[0], ENTRIES[2]], "empty"),
    ]
    for label, entries, needle in cases:
        raises(lambda: layers.coverage("Widgets", A, H, lay, entries), needle, label)
    raises(lambda: layers.coverage("Widgets", "short", H, lay, ENTRIES), "40-character", "bad commit")
    raises(lambda: layers.coverage("Widgets", A, None, lay, ENTRIES), "readme_sha", "no README hash")


def test_the_rendered_status_round_trips_the_coverage():
    lay = layers.headings(README)
    prose, entries = layers.split_block(BLOCK)
    cov = layers.coverage("Widgets", A, H, lay, entries)
    text = files.render_status("Widgets", A, "2026-09-01T00:00:00Z", prose, cov)
    assert files.parse_status(text)["coverage"] == cov
    assert "```coverage" not in text
    lines = text.splitlines()  # the header is the second line, part of the canonical prefix
    assert lines[0].startswith("<!--tauceti-status:v1 ") and lines[1].startswith("<!--tauceti-coverage:v1 "), lines[:2]
    assert lines[2] == "# Status: Widgets"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
