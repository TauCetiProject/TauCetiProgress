"""Tests for retiring the reports a landing orphans.

The rule is deliberately narrow: a report is retired only when `main` has already moved past the
cursor its window starts at, which makes it unmergeable as a matter of fact. An earlier version
closed reports *before* a replacement landed, by arguing one window contained another; it could
cancel a valid landing, it raced the gate's own state snapshot, and it trusted an editable pull
request body. None of those are reachable from here, and the tests below pin that.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import reconcile  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


CURSOR = "37aec57229a4a5828884027165b25804aac01ac8"
OLD = "b21862652ed4e0e0cb29351a0e78338370755d0a"


def pr(number, area="ModularForms", from7="37aec57", to7="787733a", owner="TauCetiProject"):
    return {"number": number, "url": f"https://example.invalid/{number}",
            "headRefName": f"progress/{from7}-{to7}/{area}",
            "headRepositoryOwner": {"login": owner}}


def sweep(rows, cursor=CURSOR, area="ModularForms"):
    return reconcile.orphaned_prs(rows, area, cursor)


def test_a_report_behind_the_cursor_is_retired():
    rows = sweep([pr(372, from7="b218626", to7="0038168")])
    assert [r["number"] for r, _ in rows] == [372]
    assert "can no longer append" in rows[0][1]


def test_a_report_at_the_cursor_is_kept():
    """Even a narrower one. Nothing is retired on the strength of a window that has not landed."""
    assert sweep([pr(394, to7="787733a")]) == []


def test_a_wider_report_at_the_cursor_is_kept():
    assert sweep([pr(410, to7="ffffff0")]) == []


def test_every_report_at_the_cursor_is_kept_however_many():
    """The old rule would have closed all but one of these before any had proved anything."""
    assert sweep([pr(410, to7="aaaaaaa"), pr(411, to7="bbbbbbb"), pr(412, to7="ccccccc")]) == []


def test_a_strangers_orphan_is_retired_too():
    """Unlike `apply`, ownership is not consulted: the report is unmergeable for its author as much
    as for anyone, and leaving it open marks the area in flight against them."""
    rows = sweep([pr(500, from7="b218626", owner="a-stranger")])
    assert [r["number"] for r, _ in rows] == [500]


def test_another_area_is_untouched():
    assert sweep([pr(396, area="ArithmeticDirichletSeries", from7="8745177")]) == []


def test_a_malformed_branch_is_ignored():
    bad = {"number": 9, "headRefName": "progress/ModularForms"}
    worse = {"number": 10, "headRefName": "progress/nowindow/ModularForms"}
    assert sweep([bad, worse]) == []


def test_an_empty_cursor_retires_nothing():
    """A cursor that could not be read must never be treated as 'matches nothing'."""
    assert sweep([pr(372, from7="b218626")], cursor="") == []
    assert sweep([pr(372, from7="b218626")], cursor=None) == []


def test_the_stale_cursor_inversion_cannot_happen_here():
    """Passing yesterday's cursor would retire the only live report. The caller reads it fresh after
    the landing; this pins what the rule does if that contract is ever broken, so the risk is
    visible rather than implicit."""
    live_report = pr(399, from7="37aec57")
    assert [r["number"] for r, _ in sweep([live_report], cursor=OLD)] == [399]


def test_a_failed_close_is_counted_not_raised():
    """The content is already on main; a cleanup failure must not fail the merge."""
    def boom(args, **kw):
        raise reconcile.gh.GhError("gh exploded")
    orig = reconcile.gh.gh
    reconcile.gh.gh = boom
    try:
        assert reconcile.close_orphans([(pr(372), "because")], "url") == 1
    finally:
        reconcile.gh.gh = orig


def test_the_close_note_says_reopening_will_not_help():
    calls = []
    orig = reconcile.gh.gh
    reconcile.gh.gh = lambda args, **kw: calls.append(args) or ""
    try:
        reconcile.close_orphans([(pr(372), "because")], "https://example.invalid/c")
    finally:
        reconcile.gh.gh = orig
    note = calls[0][calls[0].index("--comment") + 1]
    assert "Reopening will not help" in note and "https://example.invalid/c" in note


def test_a_programming_error_is_not_hidden():
    def boom(args, **kw):
        raise TypeError("a real bug")
    orig = reconcile.gh.gh
    reconcile.gh.gh = boom
    try:
        reconcile.close_orphans([(pr(372), "because")], "url")
    except TypeError:
        pass
    else:
        raise AssertionError("a non-gh error must surface")
    finally:
        reconcile.gh.gh = orig


def test_apply_no_longer_closes_anything():
    """The predict-ahead sweep is gone from the publishing path, not merely unused."""
    from progress import apply
    for gone in ("superseded_prs", "close_superseded", "sweep", "report_meta"):
        assert not hasattr(apply, gone), f"apply.{gone} still exists"



def test_other_parent_maps_both_ways():
    assert reconcile.other_parent("TauCetiRoadmap/X/PROGRESS.md") == "Completed/X/PROGRESS.md"
    assert reconcile.other_parent("Completed/X/PROGRESS.md") == "TauCetiRoadmap/X/PROGRESS.md"
    assert reconcile.other_parent("Elsewhere/X/PROGRESS.md") is None


def test_an_ambiguous_area_retires_nothing():
    """`TauCetiRoadmap/X` and `Completed/X` are different roadmaps with different cursors, and a
    report branch records only the name. Retiring on whichever cursor we hold would discard the
    other roadmap's live reports."""
    seen = []
    orig = reconcile.gh.file_on_default_branch
    reconcile.gh.file_on_default_branch = lambda path, repo=None: (
        seen.append(path) or "<!--tauceti-progress:v1 {\"to_sha\":\"37aec57229a4a5828884027165b25804aac01ac8\"}-->")
    orig_cursor = reconcile.files.cursor
    reconcile.files.cursor = lambda text: "37aec57229a4a5828884027165b25804aac01ac8"
    orig_open = reconcile.gh.open_progress_prs
    reconcile.gh.open_progress_prs = lambda repo=None: [pr(372, from7="b218626")]
    try:
        assert reconcile.sweep_area("X", "TauCetiRoadmap/X/PROGRESS.md") == (0, 0)
        assert "Completed/X/PROGRESS.md" in seen
    finally:
        reconcile.gh.file_on_default_branch = orig
        reconcile.files.cursor = orig_cursor
        reconcile.gh.open_progress_prs = orig_open


def test_an_unambiguous_area_retires_normally():
    orig = reconcile.gh.file_on_default_branch
    reconcile.gh.file_on_default_branch = lambda path, repo=None: (
        "text" if path.startswith("TauCetiRoadmap/") else None)
    orig_cursor = reconcile.files.cursor
    reconcile.files.cursor = lambda text: CURSOR
    orig_open = reconcile.gh.open_progress_prs
    reconcile.gh.open_progress_prs = lambda repo=None: [pr(372, from7="b218626")]
    closed_calls = []
    orig_close = reconcile.close_orphans
    reconcile.close_orphans = lambda rows, url="": closed_calls.append(len(rows)) or 0
    try:
        assert reconcile.sweep_area("ModularForms", "TauCetiRoadmap/ModularForms/PROGRESS.md") == (1, 0)
        assert closed_calls == [1]
    finally:
        reconcile.gh.file_on_default_branch = orig
        reconcile.files.cursor = orig_cursor
        reconcile.gh.open_progress_prs = orig_open
        reconcile.close_orphans = orig_close


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
