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


def section(frm, to, area="ModularForms"):
    import json
    meta = {"roadmap": area, "from_sha": frm, "to_sha": to, "prs": [1]}
    return ("\n<!--tauceti-progress:v1 " + json.dumps(meta, sort_keys=True, separators=(",", ":"))
            + "-->\n## a window\n\nprose\n")


def log(*pairs):
    return "# Progress\n" + "".join(section(a, b) for a, b in pairs)


A = "aaaaaaa" + "0" * 33
B = "bbbbbbb" + "0" * 33
C = "ccccccc" + "0" * 33
D = "ddddddd" + "0" * 33


def pr(number, area="ModularForms", from7="bbbbbbb", to7="ccccccc", owner="TauCetiProject",
       base="main"):
    return {"number": number, "url": f"https://example.invalid/{number}",
            "headRefName": f"progress/{from7}-{to7}/{area}",
            "baseRefName": base,
            "headRepositoryOwner": {"login": owner}}


def evidence(text):
    return reconcile.retirement_evidence(text)


def retire(rows, text=None, area="ModularForms"):
    consumed, live = evidence(text if text is not None else log((A, B), (B, C)))
    return reconcile.retirable_prs(rows, area, consumed, live)


def test_evidence_names_spent_cursors_and_the_live_one():
    consumed, live = evidence(log((A, B), (B, C)))
    assert live == C and consumed == {A, B}


def test_evidence_from_an_empty_or_broken_log_is_nothing():
    assert evidence("") == (set(), "")
    assert evidence("no markers here") == (set(), "")
    assert evidence("<!--tauceti-progress:v1 {not json}-->") == (set(), "")


def test_a_report_at_a_spent_cursor_is_retired():
    rows = retire([pr(1, from7="aaaaaaa")])
    assert [r["number"] for r, _ in rows] == [1]
    assert "already appended at and moved past" in rows[0][1]


def test_a_report_at_the_live_cursor_is_kept():
    assert retire([pr(1, from7="ccccccc")]) == []


def test_a_report_AHEAD_of_the_log_is_kept():
    """The case inequality-with-a-snapshot gets wrong. A report starting at a cursor this log has
    never seen -- because the read was stale, or history moved on -- disagrees with the snapshot
    exactly as loudly as a spent one, and must not be retired for it."""
    assert retire([pr(1, from7="ddddddd")]) == []


def test_a_stale_log_retires_fewer_never_a_live_one():
    """Reading only the first window leaves B live, so the report at B survives; the genuinely spent
    A is still retired. Staleness shrinks the evidence, it never inverts it."""
    rows = retire([pr(1, from7="aaaaaaa"), pr(2, from7="bbbbbbb")], text=log((A, B)))
    assert [r["number"] for r, _ in rows] == [1]


def test_a_prefix_matching_both_spent_and_live_is_kept():
    """Seven hex characters are not a commit."""
    live_twin = "aaaaaaa" + "9" * 33
    rows = retire([pr(1, from7="aaaaaaa")], text=log((A, live_twin)))
    assert rows == []


def test_a_branch_outside_the_gates_grammar_is_never_touched():
    """Someone's ordinary pull request that happens to start with `progress/`. Failing an automated
    gate is not a reason to close a human's work."""
    for ref in ("progress/nothex-whatever/ModularForms", "progress/aaaaaaa/ModularForms",
                "progress/aaaaaaa-bbbbbbb/Modular Forms", "progress/AAAAAAA-bbbbbbb/ModularForms"):
        row = pr(1)
        row["headRefName"] = ref
        assert retire([row]) == [], ref


def test_a_report_targeting_another_base_is_kept():
    assert retire([pr(1, from7="aaaaaaa", base="some-feature-branch")]) == []


def test_another_area_is_untouched():
    assert retire([pr(1, area="Chebotarev", from7="aaaaaaa")]) == []


def test_a_strangers_spent_report_is_retired_too():
    rows = retire([pr(1, from7="aaaaaaa", owner="a-stranger")])
    assert [r["number"] for r, _ in rows] == [1]


def test_close_writes_to_the_repo_it_read():
    """Reading one repository's numbers and closing another's by the same number is a repository
    mix-up, not a typo."""
    calls = []
    orig = reconcile.gh.gh
    reconcile.gh.gh = lambda args, **kw: calls.append(args) or ""
    try:
        reconcile.close_orphans([(pr(7), "because")], "url", repo="other/roadmap")
    finally:
        reconcile.gh.gh = orig
    assert "other/roadmap" in calls[0] and "TauCetiProject/TauCetiRoadmap" not in calls[0]


def test_a_failed_close_is_counted_not_raised():
    def boom(args, **kw):
        raise reconcile.gh.GhError("gh exploded")
    orig = reconcile.gh.gh
    reconcile.gh.gh = boom
    try:
        assert reconcile.close_orphans([(pr(1), "because")], "url") == 1
    finally:
        reconcile.gh.gh = orig


def test_a_programming_error_is_not_hidden():
    def boom(args, **kw):
        raise TypeError("a real bug")
    orig = reconcile.gh.gh
    reconcile.gh.gh = boom
    try:
        reconcile.close_orphans([(pr(1), "because")], "url")
    except TypeError:
        pass
    else:
        raise AssertionError("a non-gh error must surface")
    finally:
        reconcile.gh.gh = orig


def test_other_parent_maps_both_ways():
    assert reconcile.other_parent("TauCetiRoadmap/X/PROGRESS.md") == "Completed/X/PROGRESS.md"
    assert reconcile.other_parent("Completed/X/PROGRESS.md") == "TauCetiRoadmap/X/PROGRESS.md"
    assert reconcile.other_parent("Elsewhere/X/PROGRESS.md") is None


def _fake_files(text_by_path, open_prs):
    orig = (reconcile.gh.file_on_default_branch, reconcile.gh.open_progress_prs)
    reconcile.gh.file_on_default_branch = lambda path, repo=None: text_by_path.get(path)
    reconcile.gh.open_progress_prs = lambda repo=None: open_prs
    return orig


def test_an_ambiguous_area_retires_nothing():
    orig = _fake_files({"TauCetiRoadmap/X/PROGRESS.md": log((A, B), (B, C)),
                        "Completed/X/PROGRESS.md": log((A, B))}, [pr(1, area="X", from7="aaaaaaa")])
    try:
        assert reconcile.sweep_area("X", "TauCetiRoadmap/X/PROGRESS.md") == (0, 0)
    finally:
        reconcile.gh.file_on_default_branch, reconcile.gh.open_progress_prs = orig


def test_an_unambiguous_area_retires_and_uses_the_landing_parent():
    orig = _fake_files({"Completed/X/PROGRESS.md": log((A, B), (B, C))},
                       [pr(1, area="X", from7="aaaaaaa")])
    closed = []
    orig_close = reconcile.close_orphans
    reconcile.close_orphans = lambda rows, url="", repo=None: closed.append(len(rows)) or 0
    try:
        assert reconcile.sweep_area("X", "Completed/X/PROGRESS.md") == (1, 0)
        assert closed == [1]
    finally:
        reconcile.gh.file_on_default_branch, reconcile.gh.open_progress_prs = orig
        reconcile.close_orphans = orig_close


def test_a_missing_log_retires_nothing():
    orig = _fake_files({}, [pr(1, from7="aaaaaaa")])
    try:
        assert reconcile.sweep_area("X", "TauCetiRoadmap/X/PROGRESS.md") == (0, 0)
    finally:
        reconcile.gh.file_on_default_branch, reconcile.gh.open_progress_prs = orig


def test_apply_no_longer_closes_anything():
    from progress import apply
    for gone in ("superseded_prs", "close_superseded", "sweep", "report_meta"):
        assert not hasattr(apply, gone), f"apply.{gone} still exists"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
