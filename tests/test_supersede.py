"""Tests for superseding: an area's unmergeable reports are closed once a live one is open.

Two things strand a report, and before this neither closed itself, so they accumulated one per area
per round: a sibling merging moves the cursor and orphans every other open report for that area, and
a repository-wide breakage makes every round open a fresh report that also cannot merge.
"""

import json
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


OLD_CURSOR = "b21862652ed4e0e0cb29351a0e78338370755d0a"
OUR_PRS = [6328, 6334, 6374, 6409]


def body(from_sha=CURSOR, to_sha="787733a", prs=(6328, 6334)):
    meta = {"roadmap": "ModularForms", "from_sha": from_sha, "to_sha": to_sha,
            "prs": sorted(prs), "version": "v1"}
    return apply.BODY_MARKER.replace("{}", json.dumps(meta, sort_keys=True,
                                                      separators=(",", ":"))) + "\nprose\n"


def pr(number, area="ModularForms", from7="37aec57", to7="787733a", owner=OURS, body_text=None,
       prs=(6328, 6334), meta_from=None):
    return {"number": number, "url": f"https://example.invalid/{number}",
            "headRefName": f"progress/{from7}-{to7}/{area}",
            "headRepositoryOwner": {"login": owner},
            "body": body_text if body_text is not None
            else body(from_sha=meta_from or CURSOR, to_sha=to7, prs=prs)}


def branch(from7="37aec57", to7="28e93d9", area="ModularForms"):
    return f"progress/{from7}-{to7}/{area}"


def sweep(rows, live=CURSOR, planned=CURSOR, keep=None, owners=OWNERS, our_prs=OUR_PRS):
    return apply.superseded_prs(rows, "ModularForms", live, planned, keep or branch(), owners,
                                our_prs=our_prs)


def test_an_orphan_whose_window_predates_the_cursor_is_closed():
    rows = sweep([pr(372, from7="b218626", to7="0038168", meta_from=OLD_CURSOR)])
    assert [r["number"] for r, _ in rows] == [372]
    assert "can never append" in rows[0][1]


def test_a_dominated_report_at_the_same_cursor_is_closed():
    rows = sweep([pr(394, prs=(6328, 6334))])
    assert [r["number"] for r, _ in rows] == [394]
    assert "covers 2 of the 4" in rows[0][1]


def test_a_stale_worker_sweeps_nothing():
    """Codex's case. We planned at C0; a sibling landed and the cursor is now C1, so OUR report is
    the dead one. Sweeping on the stale cursor would close the only report that can still merge."""
    live_report = pr(399, from7="37aec57", to7="28e93d9")
    assert sweep([live_report], live=CURSOR, planned=OLD_CURSOR) == []


def test_a_wider_report_is_never_closed_by_a_narrower_one():
    """A worker that planned early and stalled opens a NARROW report late. Creation order would
    close the wider one and lose coverage already written; containment does not."""
    wider = pr(410, prs=(6328, 6334, 6374, 6409, 6444))
    assert sweep([wider], our_prs=[6328, 6334]) == []


def test_two_racing_workers_cannot_close_each_other():
    a = pr(410, to7="aaaaaaa", prs=(6328, 6334))
    b = pr(411, to7="bbbbbbb", prs=(6328, 6334, 6374, 6409))
    a_closes = sweep([b], keep=branch(to7="aaaaaaa"), our_prs=[6328, 6334])
    b_closes = sweep([a], keep=branch(to7="bbbbbbb"), our_prs=[6328, 6334, 6374, 6409])
    assert [r["number"] for r, _ in a_closes] == []
    assert [r["number"] for r, _ in b_closes] == [410]


def test_an_incomparable_window_is_kept():
    """Neither contains the other, so neither supersedes the other."""
    assert sweep([pr(410, prs=(6328, 9999))]) == []


def test_an_identical_window_is_kept():
    """A proper subset, not any subset: an exact duplicate is not dominated, and closing on equality
    would let two workers close each other again."""
    assert sweep([pr(410, prs=OUR_PRS)]) == []


def test_unknown_own_window_closes_no_same_cursor_report():
    assert sweep([pr(394, prs=(6328,))], our_prs=None) == []


def test_unknown_own_window_still_closes_orphans():
    """Being dead is a property of the report alone."""
    rows = sweep([pr(372, from7="b218626", to7="0038168", meta_from=OLD_CURSOR)], our_prs=None)
    assert [r["number"] for r, _ in rows] == [372]


def test_a_report_without_readable_metadata_is_kept():
    assert sweep([pr(394, body_text="no marker here")]) == []
    assert sweep([pr(394, body_text=apply.BODY_MARKER.replace("{}", "not json"))]) == []


def test_metadata_claiming_a_different_cursor_is_kept():
    """The body is mutable, so it is only ever used to prove a report covers LESS."""
    assert sweep([pr(394, meta_from=OLD_CURSOR, prs=(6328,))]) == []


def test_our_own_branch_is_never_closed():
    assert sweep([pr(399, to7="28e93d9")], keep=branch(to7="28e93d9")) == []


def test_another_area_is_untouched():
    assert sweep([pr(396, area="ArithmeticDirichletSeries", from7="8745177")]) == []


def test_a_strangers_report_is_never_closed():
    assert sweep([pr(500, from7="b218626", owner="a-stranger", meta_from=OLD_CURSOR)]) == []


def test_a_malformed_branch_is_ignored():
    bad = {"number": 9, "headRefName": "progress/ModularForms",
           "headRepositoryOwner": {"login": OURS}, "body": body()}
    worse = {"number": 10, "headRefName": "progress/nowindow/ModularForms",
             "headRepositoryOwner": {"login": OURS}, "body": body()}
    assert sweep([bad, worse]) == []


def test_the_whole_modular_forms_pileup_is_swept():
    rows = sweep(
        [pr(372, from7="b218626", to7="0038168", meta_from=OLD_CURSOR),
         pr(381, from7="b218626", to7="0430506", meta_from=OLD_CURSOR),
         pr(394, prs=(6328, 6334)),
         pr(399, to7="28e93d9", prs=OUR_PRS)],
        keep=branch(to7="28e93d9"))
    assert sorted(r["number"] for r, _ in rows) == [372, 381, 394]


def test_report_meta_reads_the_window():
    meta = apply.report_meta(body(prs=(1, 2, 3)))
    assert meta["from_sha"] == CURSOR and meta["prs"] == [1, 2, 3]
    assert apply.report_meta("") is None
    assert apply.report_meta(None) is None


def test_a_failed_close_is_counted_not_swallowed():
    """The caller has to know, so it keeps retrying on the in-flight path."""
    def boom(args, **kw):
        raise apply.gh.GhError("gh exploded")
    orig = apply.gh.gh
    apply.gh.gh = boom
    try:
        assert apply.close_superseded([(pr(372), "because")], "https://example.invalid/399") == 1
    finally:
        apply.gh.gh = orig


def test_a_programming_error_in_a_close_is_not_hidden():
    def boom(args, **kw):
        raise TypeError("a real bug")
    orig = apply.gh.gh
    apply.gh.gh = boom
    try:
        apply.close_superseded([(pr(372), "because")], "url")
    except TypeError:
        pass
    else:
        raise AssertionError("a non-gh error must surface")
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
