"""Tests for superseding: an area's unmergeable reports are closed once a live one is open.

Two things strand a report, and before this neither closed itself, so they accumulated one per area
per round: a sibling merging moves the cursor and orphans every other open report for that area, and
a repository-wide breakage makes every round open a fresh report that also cannot merge.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


OURS = "TauCetiProject"
OWNERS = {OURS}
CURSOR = "37aec57229a4a5828884027165b25804aac01ac8"


def pr(number, area="ModularForms", from7="37aec57", to7="787733a", owner=OURS):
    return {"number": number, "url": f"https://example.invalid/{number}",
            "headRefName": f"progress/{from7}-{to7}/{area}",
            "headRepositoryOwner": {"login": owner}}


def branch(from7="37aec57", to7="28e93d9", area="ModularForms"):
    return f"progress/{from7}-{to7}/{area}"


def test_an_orphan_whose_window_predates_the_cursor_is_closed():
    rows = apply.superseded_prs([pr(372, from7="b218626", to7="0038168")],
                                "ModularForms", CURSOR, branch(), OWNERS)
    assert [r["number"] for r, _ in rows] == [372]
    assert "can never append" in rows[0][1]


def test_a_narrower_report_at_the_same_cursor_is_closed():
    rows = apply.superseded_prs([pr(394, to7="787733a")],
                                "ModularForms", CURSOR, branch(), OWNERS)
    assert [r["number"] for r, _ in rows] == [394]
    assert "superseded" in rows[0][1]


def test_our_own_branch_is_never_closed():
    keep = branch()
    rows = apply.superseded_prs([pr(399, to7="28e93d9")], "ModularForms", CURSOR, keep, OWNERS)
    assert rows == []


def test_another_area_is_untouched():
    rows = apply.superseded_prs([pr(396, area="ArithmeticDirichletSeries", from7="8745177")],
                                "ModularForms", CURSOR, branch(), OWNERS)
    assert rows == []


def test_a_strangers_report_is_never_closed():
    """Branch names are a pure function of the window, so anyone can create one. Closing a
    stranger's would let this operator silently veto someone else's contribution."""
    rows = apply.superseded_prs([pr(500, from7="b218626", owner="a-stranger")],
                                "ModularForms", CURSOR, branch(), OWNERS)
    assert rows == []


def test_a_malformed_branch_is_ignored():
    bad = {"number": 9, "headRefName": "progress/ModularForms",
           "headRepositoryOwner": {"login": OURS}}
    worse = {"number": 10, "headRefName": "progress/nowindow/ModularForms",
             "headRepositoryOwner": {"login": OURS}}
    assert apply.superseded_prs([bad, worse], "ModularForms", CURSOR, branch(), OWNERS) == []


def test_the_whole_modular_forms_pileup_is_swept():
    """The real case: two orphans from the old cursor plus one narrower report at the current one."""
    rows = apply.superseded_prs(
        [pr(372, from7="b218626", to7="0038168"), pr(381, from7="b218626", to7="0430506"),
         pr(394, to7="787733a"), pr(399, to7="28e93d9")],
        "ModularForms", CURSOR, branch(to7="28e93d9"), OWNERS)
    assert sorted(r["number"] for r, _ in rows) == [372, 381, 394]


def test_closing_reports_the_replacement_and_the_reason():
    calls = []
    orig = apply.gh.gh
    apply.gh.gh = lambda args, **kw: calls.append(args) or ""
    try:
        apply.close_superseded([(pr(372), "because")], "https://example.invalid/399")
    finally:
        apply.gh.gh = orig
    assert calls and calls[0][:3] == ["pr", "close", "372"]
    note = calls[0][calls[0].index("--comment") + 1]
    assert "https://example.invalid/399" in note and "because" in note


def test_a_failed_close_is_not_fatal():
    """A tidy-up failure must never cost a publication: the report is already open by then."""
    def boom(args, **kw):
        raise RuntimeError("gh exploded")
    orig = apply.gh.gh
    apply.gh.gh = boom
    try:
        apply.close_superseded([(pr(372), "because")], "https://example.invalid/399")
    finally:
        apply.gh.gh = orig


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
