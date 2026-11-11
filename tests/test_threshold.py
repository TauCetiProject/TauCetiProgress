"""Tests for `plan --strategy threshold`, `--skip-area` and the label cache.

The rule: N PRs in a roadmap's window, T days since its last report. A roadmap qualifies when N > 0
and N + T > THRESHOLD, or it is archived, or it is not yet assessed; the merge gate's per-roadmap
interval still applies; the unassessed go first, then the largest N + T. Both checkouts are real git
repositories, because "when was it last reported" is read from git as the gate's collector reads it
from the API.
"""

import contextlib
import datetime
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import cli, files, gh, layers, plan, window  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def raises(exc_type, fn, needle=None):
    try:
        fn()
    except exc_type as exc:
        if needle and needle not in str(exc):
            raise AssertionError(f"expected {needle!r} in {str(exc)!r}") from None
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, none raised")


NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
BUILD_PLAN = plan.build_plan
README_WITH_LAYERS = "# Area\n\n## Layer 0: foundations\n\n## Layer 1: the summit\n"


def ago(days=0.0, hours=0.0):
    return NOW - datetime.timedelta(days=days, hours=hours)


# ----- assess: the rule for one roadmap --------------------------------------------------------


def test_no_new_prs_never_qualifies():
    ok, _days, note, when = plan.assess(0, ago(days=40), False, NOW)
    assert not ok and when is None and "no new" in note
    ok, *_ = plan.assess(0, None, True, NOW)
    assert not ok, "an archived roadmap still needs something to report"


def test_n_plus_t_must_exceed_the_threshold():
    ok, days, note, _ = plan.assess(4, ago(days=6.5), False, NOW)
    assert ok and abs(days - 6.5) < 1e-9 and "10.5 > 10" in note, note
    ok, _days, note, when = plan.assess(7, ago(days=3), False, NOW)
    assert not ok and "needs > 10" in note, "N + T = 10 is not > 10"
    assert when == NOW, when


def test_a_busy_roadmap_qualifies_quickly_and_a_quiet_one_after_a_while():
    assert plan.assess(11, ago(hours=7), False, NOW)[0]
    ok, _days, _note, when = plan.assess(1, ago(days=8.5), False, NOW)
    assert not ok and when == NOW + datetime.timedelta(days=0.5), when
    assert plan.assess(1, ago(days=8.5), False, NOW + datetime.timedelta(days=0.51))[0]


def test_an_archived_or_unassessed_roadmap_needs_only_one_pr():
    ok, _days, note, _ = plan.assess(1, ago(days=1), True, NOW)
    assert ok and note == "declared complete"
    ok, _days, note, _ = plan.assess(1, ago(days=1), False, NOW, unassessed="Area's README changed since its report")
    assert ok and note == "not yet assessed: Area's README changed since its report", note
    assert not plan.assess(1, ago(days=1), False, NOW)[0], "the same roadmap, assessed, waits for N + T > 10"


def test_a_never_reported_roadmap_counts_t_from_its_readme():
    ok, days, note, _ = plan.assess(3, None, False, NOW, unassessed="no report yet", added=ago(days=20))
    assert ok and abs(days - 20) < 1e-9 and "no report yet" in note, (ok, days, note)
    ok, days, _note, _ = plan.assess(1, None, False, NOW, unassessed="no report yet")
    assert ok and days is None
    ok, days, _note, when = plan.assess(3, None, False, NOW)
    assert not ok and when is None, "with no date to count from, N alone must exceed the threshold"


def test_the_gate_interval_holds_for_every_roadmap():
    """The merge gate refuses a second report for a roadmap within its interval, so the planner must
    not choose one, however many PRs it has and whichever clause it qualifies under."""
    for kwargs in ({}, {"complete": True}, {"unassessed": "README changed"}):
        complete = kwargs.pop("complete", False)
        ok, _days, note, when = plan.assess(80, ago(hours=2), complete, NOW, **kwargs)
        assert not ok and "merge gate" in note, note
        assert when == ago(hours=2) + datetime.timedelta(hours=plan.MIN_REPORT_INTERVAL_HOURS), when
    assert plan.assess(80, ago(hours=6.01), False, NOW)[0]
    # 2 PRs, reported 1 h ago: the gate allows it at +6 h but the rule only at +8 days.
    ok, _days, _note, when = plan.assess(2, ago(hours=1), False, NOW)
    assert not ok and when == ago(hours=1) + datetime.timedelta(days=8), when


def test_the_threshold_is_a_parameter():
    assert plan.assess(3, ago(days=3), False, NOW, threshold=5)[0]
    assert not plan.assess(3, ago(days=3), False, NOW, threshold=6)[0]


# ----- real checkouts --------------------------------------------------------------------------


class Checkouts:
    """A code checkout whose commits are squash merges `(#n)`, and a roadmap checkout whose commits
    carry chosen dates."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="threshold-")
        root = pathlib.Path(self.tmp.name)
        self.code, self.roadmap = root / "code", root / "roadmap"
        for repo in (self.code, self.roadmap):
            self.git(repo, "init", "-q", "-b", "main", str(repo), cwd=False)
        self.root = self.merge("root")

    def git(self, repo, *args, when=None, cwd=True):
        stamp = (when or ago(days=100)).isoformat()
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T",
                   GIT_COMMITTER_EMAIL="t@e", GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        return subprocess.run(["git", *(["-C", str(repo)] if cwd else []), *args], check=True,
                              capture_output=True, text=True, env=env).stdout.strip()

    def merge(self, subject):
        if isinstance(subject, int):
            subject = f"feat: something (#{subject})"
        self.git(self.code, "commit", "-q", "--allow-empty", "-m", subject)
        return self.git(self.code, "rev-parse", "HEAD")

    def write(self, path, text, when):
        target = self.roadmap / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.git(self.roadmap, "add", "-A")
        self.git(self.roadmap, "commit", "-q", "-m", f"write {path}", when=when)

    def area(self, name, reported=None, cursor=None, parent="TauCetiRoadmap", readme=None, status="a report\n"):
        """A roadmap added 90 days ago and, unless `reported` is None, reported that many days ago
        with its cursor at `cursor`."""
        self.write(f"{parent}/{name}/README.md", readme or f"# {name}\n", ago(days=90))
        if reported is not None:
            (self.roadmap / parent / name / "STATUS.md").write_text(status, encoding="utf-8")
            section = files.render_section(name, self.root, cursor or self.root, [1], "2026-01-01", "prose " * 60)
            self.write(f"{parent}/{name}/PROGRESS.md", files.new_progress_file(name) + section, ago(days=reported))

    def plan(self, labels, open_prs=(), **kwargs):
        tip = self.git(self.code, "rev-parse", "HEAD")
        docs = SimpleNamespace(source_commit=lambda: tip, generation=f"pages/{tip}",
                               require_coverage=lambda paths: None, pin=lambda: {"source_commit": tip})
        with patch.object(gh, "closed_prs_for_areas", lambda areas, **_: {
                a: {n: None for n in labels.get(a, [])} for a in areas}), \
             patch.object(plan, "_own_login", return_value="operator"), \
             patch.object(gh, "gh", side_effect=AssertionError("unexpected network request")):
            return BUILD_PLAN(self.roadmap, self.code, open_prs=list(open_prs), ref="main",
                              now=NOW, docs=docs, **kwargs)


def quiet_and_busy():
    """Quiet: 2 PRs, reported 40 days ago (N + T = 42). Busy: 5 PRs, reported a day ago (6)."""
    c = Checkouts()
    for number in range(1, 8):
        c.merge(number)
    c.area("Quiet", reported=40)
    c.area("Busy", reported=1)
    return c, {"Quiet": [1, 2], "Busy": [3, 4, 5, 6, 7]}


def test_the_default_strategy_is_unchanged_and_threshold_differs_from_it():
    c, labels = quiet_and_busy()
    assert c.plan(labels, commits=[], min_prs=1)["roadmap"] == "Busy"
    recent = [(ago(hours=1).isoformat(), "progress: Busy")]
    raises(plan.NotDue, lambda: c.plan(labels, commits=recent, min_prs=1), "landed 1.0h ago")
    got = c.plan(labels, strategy="threshold", commits=recent)
    assert got["roadmap"] == "Quiet" and got["prs"] == [2, 1], got
    assert "last reported 40.0 days ago (N+T = 42.0 > 10)" in got["reason"], got["reason"]
    assert "Busy" not in [r["area"] for r in got["runner_up"]]


def test_the_table_accounts_for_every_roadmap_and_is_written_when_nothing_qualifies():
    c, labels = quiet_and_busy()
    c.area("Idle", reported=4)
    c.area("Done", reported=30, parent="Completed")
    table_path = pathlib.Path(c.tmp.name) / "state" / "table.json"
    exc = raises(plan.NotDue, lambda: c.plan({"Busy": labels["Busy"]}, strategy="threshold", table_path=table_path),
                 "no roadmap qualifies yet: 1 have new pull requests; closest: Busy")
    assert "needs > 10" in str(exc), exc
    table = json.loads(table_path.read_text())
    rows = {r["area"]: r for r in table["rows"]}
    assert set(rows) == {"Quiet", "Busy", "Idle", "Done"}, rows
    assert rows["Idle"]["note"] == "no merged PRs yet" and rows["Idle"]["prs"] == 0, rows["Idle"]
    assert rows["Idle"]["days"] == 4.0 and rows["Done"]["complete"] is True and not rows["Done"]["qualifies"]
    # Busy: 5 PRs, reported a day ago, so it qualifies once T > 5: four days from now.
    assert rows["Busy"]["qualifies_from"] == (NOW + datetime.timedelta(days=4)).isoformat(timespec="seconds")
    assert rows["Busy"]["score"] == 6.0 and rows["Busy"]["assessed"] is True
    assert table["next_qualifies_at"] == rows["Busy"]["qualifies_from"] and table["chosen"] is None
    assert table["to_sha"] == c.git(c.code, "rev-parse", "HEAD") and table["threshold"] == 10.0

    got = c.plan(labels, strategy="threshold", table_path=table_path)
    table = json.loads(table_path.read_text())
    assert table["chosen"] == got["roadmap"] == "Quiet" and table["rows"][0]["area"] == "Quiet"
    assert table["rows"][0]["qualifies"] is True and table["rows"][0]["qualifies_from"] is None


def test_an_archived_roadmap_with_one_pr_is_chosen():
    c = Checkouts()
    c.merge(1), c.merge(2), c.merge(3)
    c.area("Done", reported=3, parent="Completed")
    c.area("Small", reported=1)
    got = c.plan({"Done": [1], "Small": [2, 3]}, strategy="threshold")
    assert got["roadmap"] == "Done" and "declared complete" in got["reason"], got


def test_a_nested_archive_directory_is_compared_whole():
    c = Checkouts()
    c.merge(1)
    c.area("Done", reported=1, parent="Archive/Completed")
    with patch.object(plan, "COMPLETED_DIR", "Archive/Completed"):
        got = c.plan({"Done": [1]}, strategy="threshold")
        assert got["roadmap"] == "Done" and "declared complete" in got["reason"], got
        assert not plan.is_complete("Archive/Other") and not plan.is_complete("Archive")
    assert not plan.is_complete("Archive/Completed/Done") and plan.is_complete("Completed/Done")


def status_for(readme, roadmap):
    header = {"roadmap": roadmap, "to_sha": "b" * 40, "readme_sha": layers.readme_sha(readme),
              "layers": [{"id": "Layer 0", "state": "done"}, {"id": "Layer 1", "state": "partial"}]}
    return f"<!--tauceti-coverage:v1 {json.dumps(header)}-->\n# Status: {roadmap}\n"


def test_an_unassessed_roadmap_goes_before_any_assessed_one():
    c, labels = quiet_and_busy()
    c.area("Edited", reported=0.5, readme=README_WITH_LAYERS, status=status_for(README_WITH_LAYERS, "Edited"))
    c.area("New")
    c.merge(8), c.merge(9)
    labels = {**labels, "Edited": [8], "New": [9]}
    assert c.plan(labels, strategy="threshold")["roadmap"] == "New", "never reported, added 90 days ago"
    del labels["New"]
    assert c.plan(labels, strategy="threshold")["roadmap"] == "Quiet", "Edited's report assesses its README"
    c.write("TauCetiRoadmap/Edited/README.md", README_WITH_LAYERS + "\nA new item.\n", ago(hours=2))
    got = c.plan(labels, strategy="threshold")
    assert got["roadmap"] == "Edited" and [r["area"] for r in got["runner_up"]] == ["Quiet"], got
    assert "Edited's README changed since its report" in got["reason"], got["reason"]
    assert "1 unassessed among 2 qualifying" in got["reason"], got["reason"]


def test_assessment_gap_reads_the_coverage_headers_against_the_readmes():
    c = Checkouts()
    c.area("Area", readme=README_WITH_LAYERS)
    gap = lambda status: plan.assessment_gap(c.roadmap, "Area", "TauCetiRoadmap/Area", status)  # noqa: E731
    assert gap(None) == "no report yet"
    assert gap("# Status\nprose only\n") == "its report does not assess Area"
    assert gap(status_for(README_WITH_LAYERS, "Area")) is None
    c.write("TauCetiRoadmap/Area/Sub/Suggested.lean", "-- sub\n", ago(days=30))
    c.write("TauCetiRoadmap/Area/Sub/README.md", README_WITH_LAYERS, ago(days=30))
    assert gap(status_for(README_WITH_LAYERS, "Area")) == "its report does not assess Area/Sub"
    both = status_for(README_WITH_LAYERS, "Area") + status_for(README_WITH_LAYERS, "Area/Sub")
    assert gap(both) is None
    c.write("TauCetiRoadmap/Area/README.md", "# Area\n\nNow an index with no layers.\n", ago(days=1))
    assert gap(status_for(README_WITH_LAYERS, "Area/Sub")) is None


def test_report_dates_are_read_at_the_ref_as_the_gate_reads_them():
    c = Checkouts()
    c.area("A", reported=12)
    c.write("TauCetiRoadmap/A/README.md", "# A, edited\n", ago(days=2))
    c.git(c.roadmap, "update-ref", "refs/remotes/origin/main", "HEAD")
    c.write("TauCetiRoadmap/A/PROGRESS.md", "an unpublished report\n", ago(days=1))
    reported, added = plan.report_dates(c.roadmap, "origin/main")
    assert reported == {"TauCetiRoadmap/A/PROGRESS.md": ago(days=12)}, reported
    assert added == {"TauCetiRoadmap/A/README.md": ago(days=90)}, added
    assert plan.report_dates(c.roadmap)[0] == {"TauCetiRoadmap/A/PROGRESS.md": ago(days=1)}
    raises(window.GitError, lambda: plan.report_dates(c.roadmap, "origin/missing"))
    shallow = pathlib.Path(c.tmp.name) / "shallow"
    c.git(c.roadmap, "clone", "-q", "--depth=1", c.roadmap.as_uri(), str(shallow), cwd=False)
    raises(window.GitError, lambda: plan.report_dates(shallow), "complete roadmap history")


def report_pr(number, area, hours_old, owner="stranger"):
    return {"number": number, "headRefName": f"progress/aaaaaaa-bbbbbbb/{area}",
            "createdAt": ago(hours=hours_old).isoformat(), "headRepositoryOwner": {"login": owner}}


def test_under_threshold_anyone_s_fresh_report_holds_its_area():
    c, labels = quiet_and_busy()
    c.area("Other", reported=30)
    c.merge(8)
    labels["Other"] = [8]
    fresh, stale = report_pr(519, "Quiet", 2), report_pr(493, "Other", 96)
    got = c.plan(labels, open_prs=[fresh, stale], strategy="threshold")
    assert got["roadmap"] == "Other", got["roadmap"]
    assert "Quiet: PR #519 is still open" in got["skipped"], got["skipped"]
    assert any("#493" in s and "no longer marks the area in flight" in s for s in got["skipped"])
    gone = {**fresh, "headRepositoryOwner": None}
    assert c.plan(labels, open_prs=[gone], strategy="threshold")["roadmap"] == "Quiet"
    assert c.plan(labels, open_prs=[fresh, stale], commits=[], min_prs=2)["roadmap"] == "Busy"
    assert c.plan({"Quiet": [1, 2]}, open_prs=[fresh], commits=[], min_prs=1)["roadmap"] == "Quiet", \
        "the default strategy still ignores a stranger's report"


def test_the_table_says_when_an_open_report_stops_holding_its_area():
    c, labels = quiet_and_busy()
    table_path = pathlib.Path(c.tmp.name) / "table.json"
    held = [report_pr(1, "Quiet", 71), report_pr(2, "Busy", 71), report_pr(3, "Quiet", 2)]
    raises(plan.NotDue, lambda: c.plan(labels, open_prs=held, strategy="threshold", table_path=table_path))
    table = json.loads(table_path.read_text())
    expiry = (NOW + datetime.timedelta(hours=plan.STALE_PR_HOURS - 71)).isoformat(timespec="seconds")
    rows = {r["area"]: r for r in table["rows"]}
    assert rows["Busy"]["qualifies_from"] == expiry and table["next_qualifies_at"] == expiry, table
    # Quiet's latest report holds it longer; the table follows the report the planner obeys.
    assert rows["Quiet"]["qualifies_from"] > expiry, rows["Quiet"]
    # Two hours on, the same two reports no longer hold anything.
    two_hours_on = [report_pr(1, "Quiet", 73), report_pr(2, "Busy", 73)]
    assert c.plan(labels, open_prs=two_hours_on, strategy="threshold")["roadmap"] == "Quiet"
    no_age = {**held[1], "createdAt": None}
    raises(plan.NotDue, lambda: c.plan(labels, open_prs=[held[0], no_age], strategy="threshold",
                                       table_path=table_path))
    rows = {r["area"]: r for r in json.loads(table_path.read_text())["rows"]}
    assert rows["Busy"]["qualifies_from"] is None, rows["Busy"]


def test_a_skipped_area_is_never_chosen_under_either_strategy():
    c, labels = quiet_and_busy()
    got = c.plan(labels, strategy="threshold", skip_areas={"Quiet"}, threshold=5)
    assert got["roadmap"] == "Busy" and "Quiet: set aside with --skip-area" in got["skipped"], got
    table_path = pathlib.Path(c.tmp.name) / "table.json"
    raises(plan.NotDue, lambda: c.plan(labels, strategy="threshold", skip_areas={"Quiet", "Busy"},
                                       table_path=table_path))
    notes = {r["area"]: r["note"] for r in json.loads(table_path.read_text())["rows"]}
    assert notes == {"Quiet": "set aside with --skip-area", "Busy": "set aside with --skip-area"}, notes
    assert c.plan(labels, commits=[], min_prs=1, skip_areas={"Busy"})["roadmap"] == "Quiet"


def test_the_cli_selects_the_strategy_and_exits_75_with_a_table():
    c, labels = quiet_and_busy()
    table = pathlib.Path(c.tmp.name) / "table.json"
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return c.plan(labels, commits=[], **{k: kwargs[k] for k in (
            "strategy", "threshold", "table_path", "skip_areas", "min_prs")})

    def run(*extra):
        argv = ["plan", "--roadmap-dir", str(c.roadmap), "--code-dir", str(c.code), "--table", str(table), *extra]
        with patch.object(plan, "build_plan", side_effect=build), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            return cli.main(argv), err.getvalue()

    assert run("--strategy", "threshold", "--skip-area", "Quiet", "--skip-area", "Other",
               "--label-cache", "labels.json")[0] == 75
    assert seen["skip_areas"] == {"Quiet", "Other"} and seen["label_cache"] == "labels.json"
    assert seen["threshold"] == plan.THRESHOLD and json.loads(table.read_text())["chosen"] is None
    rc, err = run("--strategy", "threshold", "--threshold", "5", "--skip-area", "Quiet")
    assert rc == 0 and seen["threshold"] == 5.0 and json.loads(table.read_text())["chosen"] == "Busy", err
    assert run("--min-prs", "1")[0] == 0
    assert seen["strategy"] == "busiest" and seen["skip_areas"] == frozenset() and seen["label_cache"] is None
    with contextlib.redirect_stderr(io.StringIO()):
        raises(SystemExit, lambda: cli.build_parser().parse_args(
            ["plan", "--roadmap-dir", "r", "--code-dir", "c", "--strategy", "rotate"]))


# ----- the label cache --------------------------------------------------------------------------


class GitHub:
    """`areas`: {area: {number: native_sha_or_None}}; `closed`: {number: (labels, native_sha_or_None)}."""

    def __init__(self, areas, closed=None):
        self.areas, self.closed = areas, closed or {}
        self.full, self.searches = [], []

    def read(self, path, wanted, now=NOW):
        def for_areas(areas, repo=None):
            self.full.append(list(areas))
            return {area: dict(self.areas.get(area, {})) for area in areas}

        def since(when, repo=None, limit=None):
            self.searches.append(when)
            return dict(self.closed)

        with patch.object(gh, "closed_prs_for_areas", for_areas), patch.object(gh, "closed_prs_since", since):
            return gh.cached_closed_prs_for_areas(path, wanted, now=now)


def cache_file():
    return pathlib.Path(tempfile.mkdtemp(prefix="labels-")) / "state" / "labels.json"


def test_a_new_cache_reads_every_area_in_one_batch_and_searches_nothing():
    path, hub = cache_file(), GitHub({"A": {3: None, 1: "c1"}, "B": {2: None}})
    assert hub.read(path, ["A", "B"]) == {"A": {3: None, 1: "c1"}, "B": {2: None}}
    assert hub.full == [["A", "B"]] and hub.searches == []
    assert json.loads(path.read_text())["areas"] == {"A": {"3": None, "1": "c1"}, "B": {"2": None}}


def test_a_later_run_asks_only_for_what_closed_since():
    path = cache_file()
    GitHub({"A": {3: None, 1: "c1"}, "B": {2: None}}).read(path, ["A", "B"])
    # 7 is a bors merge (closed, no native merge); 3 was closed unmerged, reopened, and merged natively.
    hub = GitHub({"C": {9: None, 5: None}}, closed={7: (["roadmap/A", "bug"], None), 8: (["roadmap/none"], None),
                                                    9: (["roadmap/C"], None), 3: (["roadmap/A"], "c3")})
    later = NOW + datetime.timedelta(hours=3)
    assert hub.read(path, ["A", "B"], now=later) == {"A": {7: None, 3: "c3", 1: "c1"}, "B": {2: None}}
    assert hub.searches == [NOW - gh.LABEL_CACHE_MARGIN] and hub.full == []
    # An area the file has never seen is read whole, not pieced together from the search.
    assert hub.read(path, ["C"], now=later + datetime.timedelta(hours=1)) == {"C": {9: None, 5: None}}
    assert hub.full == [["C"]] and hub.searches[1] == later - gh.LABEL_CACHE_MARGIN


def test_a_full_search_a_day_old_file_and_an_unreadable_one_start_over():
    path = cache_file()
    GitHub({"A": {1: None}}).read(path, ["A"])
    current = {"A": {5: None, 1: None}}
    hub = GitHub(current, closed={n: (["roadmap/A"], None) for n in range(gh.LABEL_CACHE_SEARCH_LIMIT)})
    assert hub.read(path, ["A"], now=NOW + datetime.timedelta(hours=1)) == current and hub.full == [["A"]]
    hub = GitHub(current)
    assert hub.read(path, ["A"], now=NOW + datetime.timedelta(hours=26)) == current
    assert hub.full == [["A"]] and hub.searches == []
    for text in ("{not json", json.dumps({"version": 0, "repo": gh.CODE_REPO, "full_at": NOW.isoformat(),
                                          "synced_at": NOW.isoformat(), "areas": {"A": {"1": None}}})):
        path.write_text(text)
        hub = GitHub(current)
        assert hub.read(path, ["A"]) == current and hub.full == [["A"]] and hub.searches == []


def test_a_github_failure_is_never_an_empty_history():
    path = cache_file()
    GitHub({"A": {1: None}}).read(path, ["A"])
    with patch.object(gh, "gh", side_effect=gh.GhError("search unavailable")):
        raises(gh.GhError, lambda: gh.cached_closed_prs_for_areas(path, ["A"], now=NOW + datetime.timedelta(hours=1)))
    assert json.loads(path.read_text())["synced_at"] == NOW.isoformat(timespec="seconds")


def test_the_planner_reads_through_the_label_cache_and_rereads_its_choice():
    c, labels = quiet_and_busy()
    path, calls, refreshed = pathlib.Path(c.tmp.name) / "labels.json", [], []
    exact = dict(labels)

    def cached(file, areas, now=None):
        calls.append((file, sorted(areas), now))
        return {a: {n: None for n in labels[a]} for a in areas}

    def refresh(file, area):
        refreshed.append((file, area))
        return {n: None for n in exact[area]}

    with patch.object(gh, "cached_closed_prs_for_areas", cached), patch.object(gh, "refresh_cached_area", refresh):
        assert c.plan({}, strategy="threshold", label_cache=path)["roadmap"] == "Quiet"
        assert calls == [(path, ["Busy", "Quiet"], NOW)] and refreshed == [(path, "Quiet")], (calls, refreshed)
        # Quiet's labels were removed after the cache read them: the choice is made again without it.
        exact["Quiet"], refreshed[:] = [], []
        got = c.plan({}, strategy="threshold", label_cache=path, threshold=5)
        assert got["roadmap"] == "Busy" and "Quiet: no merged PRs yet" in got["skipped"], got
        assert refreshed == [(path, "Quiet"), (path, "Busy")], refreshed


def test_a_refreshed_area_replaces_its_entry_in_the_cache():
    path = cache_file()
    GitHub({"A": {1: None, 4: None}, "B": {2: None}}).read(path, ["A", "B"])
    with patch.object(gh, "closed_prs_for_area", lambda area, repo=None: {6: "c6", 1: None}):
        assert gh.refresh_cached_area(path, "A") == {6: "c6", 1: None}
    data = json.loads(path.read_text())
    assert data["areas"] == {"A": {"6": "c6", "1": None}, "B": {"2": None}}
    assert data["full_at"] == data["synced_at"] == NOW.isoformat(timespec="seconds")
    path.write_text("{not json")
    with patch.object(gh, "closed_prs_for_area", lambda area, repo=None: {6: "c6"}):
        assert gh.refresh_cached_area(path, "A") == {6: "c6"}
    assert path.read_text() == "{not json"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
