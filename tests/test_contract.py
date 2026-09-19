"""The producer/consumer contract for `tauceti-coverage:v1`, as an offline fixture.

The producer here and the consumer (TauCeti's `scripts/roadmap_progress.py`, which builds the site's
Progress page) keep their own extraction and format code. The manual cross-check that they agree
is not a regression test, so this one is: `tests/fixtures/coverage-contract/` holds a README, the
model's status body with its coverage block, and `expected.json` -- what `plan` and `apply` emit
from them, and what the consumer at the recorded revision made of that (layer ids and lines,
states, notes, and the reasons it refuses the header against an edited README or another library
commit). The test proves the producer still emits exactly what the consumer was recorded
accepting. Nothing is fetched: when the contract changes, regenerate the fixture against the
consumer and record the revision you checked.

Run with `python tests/test_contract.py` from the repo root, or via `tests/run`.
"""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply as apply_mod, files, layers, plan as plan_mod  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "coverage-contract"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
README = (FIXTURE / "README.md").read_text(encoding="utf-8")
BODY = (FIXTURE / "status-body.md").read_text(encoding="utf-8")
SECTION = " ".join(
    ["The group law landed, with the coordinate formulas and the proof that the operation is associative."] * 4
)

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def make_plan(readme_dir=FIXTURE.parent, rel_dir=FIXTURE.name):
    lay, sha = plan_mod.read_area_layers(readme_dir, rel_dir)
    return {
        "roadmap": EXPECTED["roadmap"], "rel_dir": f"TauCetiRoadmap/{EXPECTED['roadmap']}",
        "from_sha": EXPECTED["from_sha"], "to_sha": EXPECTED["to_sha"], "prs": [1, 2],
        "from_date": "2026-01-01T00:00:00Z", "to_date": "2026-02-01T00:00:00Z",
        "layers": lay, "readme_sha": sha, "bootstrapped": True,
    }


def test_the_plan_reads_the_inventory_the_consumer_reads():
    """Same ids, same order, same README lines (the page links each layer to its heading), and the
    hash is of the README text, which is what the consumer compares against."""
    got = make_plan()
    assert got["layers"] == EXPECTED["producer"]["layers"], got["layers"]
    assert [l["id"] for l in got["layers"]] == EXPECTED["consumer"]["layer_ids"]
    assert [l["line"] for l in got["layers"]] == EXPECTED["consumer"]["layer_lines"]
    assert got["readme_sha"] == EXPECTED["producer"]["readme_sha"]
    assert got["readme_sha"] == hashlib.sha256(README.encode("utf-8")).hexdigest()


def test_the_complete_producer_path_emits_the_recorded_header_byte_for_byte():
    """README + model body -> plan -> apply -> STATUS.md, and the prefix the gate compares is the
    one the consumer was recorded reading."""
    status_text, progress_text, _ = apply_mod.render_update(make_plan(), BODY, SECTION, None, None)
    lines = status_text.splitlines()
    assert lines[:3] == EXPECTED["producer"]["status_prefix_lines"], lines[:3]
    assert lines[1] == EXPECTED["producer"]["header_line"]
    parsed = files.parse_status(status_text)
    assert parsed["coverage"] == EXPECTED["producer"]["coverage"], parsed["coverage"]
    assert "```coverage" not in status_text
    # The gate accepts the pair as a first report.
    header = files.validate_update(EXPECTED["roadmap"], None, status_text, None, progress_text)
    assert header["to_sha"] == EXPECTED["to_sha"]


def test_the_recorded_payload_is_what_the_consumer_turned_into_states_and_notes():
    """The consumer's recorded reading of this exact payload: one state per layer in README order,
    and the `remaining` note only where the model wrote one. If the payload's shape changes, this
    is the assertion that says the consumer must be re-checked."""
    cov = EXPECTED["producer"]["coverage"]
    assert [e["state"] for e in cov["layers"]] == EXPECTED["consumer"]["states"]
    assert {e["id"]: e["remaining"] for e in cov["layers"] if "remaining" in e} == EXPECTED["consumer"]["remaining"]
    assert cov["roadmap"] == EXPECTED["roadmap"] and cov["to_sha"] == EXPECTED["consumer"]["status_header_to_sha"]
    assert len(EXPECTED["consumer"]["revision"]) == 40


def test_an_edited_readme_changes_the_hash_the_consumer_refuses_on():
    """The consumer was recorded refusing this header against a README with an extra requirement
    and against one whose Layer 2 title changed under a stable id. Both come down to `readme_sha`,
    so the producer must hash the whole text, not the layer ids."""
    for edited in (README + "\nA new requirement in Layer 2.\n",
                   README.replace("Hasse's bound", "the Hasse–Weil bound")):
        assert [l["id"] for l in layers.headings(edited)] == EXPECTED["consumer"]["layer_ids"]
        assert layers.readme_sha(edited) != EXPECTED["producer"]["readme_sha"]
    assert "different README" in EXPECTED["consumer"]["refused_edited_readme_body"]
    assert "different README" in EXPECTED["consumer"]["refused_edited_title_same_ids"]
    assert "different library commit" in EXPECTED["consumer"]["refused_other_library_commit"]


def test_a_body_without_a_block_gives_the_report_the_consumer_calls_unassessed():
    prose, _ = layers.split_block(BODY)
    status_text, _, _ = apply_mod.render_update(make_plan(), prose, SECTION, None, None)
    assert files.parse_status(status_text)["coverage"] is None
    assert status_text.splitlines()[1] == "# Status: " + EXPECTED["roadmap"]


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
