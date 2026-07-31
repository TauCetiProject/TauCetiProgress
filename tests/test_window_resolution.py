"""Tests for resolving a reported window against real TauCeti history.

Anyone may open a progress pull request, so this is what keeps that bounded: `to_sha` must name a
commit the project actually published, strictly after `from_sha`. Without it a chain of reports could
walk the cursor to arbitrary values, announcing every step.
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("collect", ROOT / ".github" / "scripts" / "collect.py")
collect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect)

from progress import files  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


AREA = "PDE"
FROM = "a" * 40
TO = "b" * 40
TIP = "d" * 40  # what `docgen` resolves to; pinned once per run, never re-read
PROSE = ("Harnack's inequality landed for a nonnegative harmonic function on a planar disc, in both "
         "the two-sided comparison with the centre value and the pairwise form on a closed subdisc "
         "with the sharp constant. The supporting mean-value machinery was extracted along the way.")


def progress_with(from_sha=FROM, to_sha=TO):
    return files.new_progress_file(AREA) + files.render_section(
        AREA, from_sha, to_sha, [1, 2, 3], "w", PROSE)


def with_statuses(mapping, tip=TIP):
    """Stub the two network calls from a `{(base, head): status}` map; anything absent is a 404."""
    orig_cmp, orig_rev = collect.compare_status, collect.rev_parse
    collect.compare_status = lambda repo, base, head: mapping.get((base, head))
    collect.rev_parse = lambda repo, ref: tip
    try:
        return collect.resolve_window(progress_with())
    finally:
        collect.compare_status, collect.rev_parse = orig_cmp, orig_rev


def test_a_real_forward_window_resolves():
    w = with_statuses({(TO, TIP): "ahead", (FROM, TO): "ahead"})
    assert w["to_reachable"] is True and w["advances"] is True
    assert w["from_sha"] == FROM and w["to_sha"] == TO


def test_the_documentation_tip_itself_is_reachable():
    """`identical` means to_sha IS the tip, which is the common case for a fresh report."""
    w = with_statuses({(TO, TIP): "identical", (FROM, TO): "ahead"})
    assert w["to_reachable"] is True and w["advances"] is True


def test_a_fabricated_to_sha_is_not_reachable():
    """A 404 from compare is exactly what an invented commit looks like."""
    w = with_statuses({(FROM, TO): "ahead"})
    assert w["to_reachable"] is False


def test_a_to_sha_off_the_documentation_branch_is_not_reachable():
    for status in ("behind", "diverged"):
        w = with_statuses({(TO, TIP): status, (FROM, TO): "ahead"})
        assert w["to_reachable"] is False, status


def test_a_backwards_window_does_not_advance():
    for status in ("behind", "diverged", "identical"):
        w = with_statuses({(TO, TIP): "ahead", (FROM, TO): status})
        assert w["advances"] is False, status


def test_an_empty_window_does_not_advance():
    """from_sha == to_sha compares as `identical`, and an empty window reports nothing."""
    w = with_statuses({(TO, TIP): "ahead", (FROM, TO): "identical"})
    assert w["advances"] is False


def test_reachability_is_checked_before_advancement():
    """No point asking whether an invented commit moves forward, and it saves a request."""
    seen = []
    orig_cmp, orig_rev = collect.compare_status, collect.rev_parse
    collect.compare_status = lambda repo, base, head: seen.append((base, head)) or None
    collect.rev_parse = lambda repo, ref: TIP
    try:
        w = collect.resolve_window(progress_with())
    finally:
        collect.compare_status, collect.rev_parse = orig_cmp, orig_rev
    assert seen == [(TO, TIP)], seen
    assert w["advances"] is None


def test_the_branch_is_pinned_to_one_snapshot():
    """`docgen` is mutable. Resolving it once and comparing against that SHA closes the gap in which
    it could move between the question and the answer, and records what was consulted."""
    w = with_statuses({(TO, TIP): "ahead", (FROM, TO): "ahead"})
    assert w["ref_sha"] == TIP


def test_an_unreadable_branch_refuses_rather_than_passing():
    orig_cmp, orig_rev = collect.compare_status, collect.rev_parse
    collect.compare_status = lambda repo, base, head: "ahead"
    collect.rev_parse = lambda repo, ref: None
    try:
        w = collect.resolve_window(progress_with())
    finally:
        collect.compare_status, collect.rev_parse = orig_cmp, orig_rev
    assert w["to_reachable"] is False and w["ref_sha"] is None


def test_an_unparseable_log_resolves_to_nothing():
    """The content checks report a malformed log properly; this must not mask them."""
    assert collect.resolve_window("not a progress file") is None
    assert collect.resolve_window("") is None
    assert collect.resolve_window(None) is None


def test_the_newest_section_is_the_one_checked():
    """A pull request appends one section; an older section's window is already history."""
    text = progress_with() + files.render_section(AREA, TO, "c" * 40, [4], "w", PROSE)
    orig_cmp, orig_rev = collect.compare_status, collect.rev_parse
    collect.compare_status = lambda repo, base, head: "ahead"
    collect.rev_parse = lambda repo, ref: TIP
    try:
        w = collect.resolve_window(text)
    finally:
        collect.compare_status, collect.rev_parse = orig_cmp, orig_rev
    assert w["from_sha"] == TO and w["to_sha"] == "c" * 40



# ----- the bootstrap cursor ---------------------------------------------------------------------


def _bootstrap(number="45", merge_sha="m" * 40, parent="p" * 40, statuses=None):
    """Drive `bootstrap_cursor` against canned responses."""
    statuses = statuses or {}
    orig_run, orig_cmp = collect.subprocess.run, collect.compare_status

    class P:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    def fake_run(args, **kw):
        joined = " ".join(args)
        if args[:2] == ["gh", "pr"]:
            # Deliberately NOT in number order, and the lowest number is not the earliest merge:
            # the selection under test is by `mergedAt`, which is the bug this guards.
            if not number:
                return P("[]")
            return P(json.dumps([
                {"number": int(number) + 3, "mergedAt": "2026-01-01T00:00:00Z"},
                {"number": int(number), "mergedAt": "2026-02-01T00:00:00Z"},
            ]))
        if "/pulls/" in joined:
            return P(merge_sha + "\n")
        if "/commits/" in joined:
            return P(parent + "\n")
        return P("")

    collect.subprocess.run = fake_run
    collect.compare_status = lambda repo, base, head: statuses.get((base, head))
    try:
        return collect.bootstrap_cursor("PDE")
    finally:
        collect.subprocess.run, collect.compare_status = orig_run, orig_cmp


M, PARENT = "m" * 40, "p" * 40


def test_a_verified_bootstrap_cursor_is_returned():
    assert _bootstrap(statuses={(M, "docgen"): "ahead", (PARENT, "docgen"): "ahead",
                                (PARENT, M): "ahead"}) == PARENT


def test_the_earliest_merged_pull_request_is_the_one_used():
    """Not the lowest-numbered: the stub returns a lower number that merged LATER."""
    seen = []
    orig_run, orig_cmp = collect.subprocess.run, collect.compare_status

    class P:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""

    def fake_run(args, **kw):
        joined = " ".join(args)
        if args[:2] == ["gh", "pr"]:
            return P(json.dumps([{"number": 100, "mergedAt": "2026-02-01T00:00:00Z"},
                                 {"number": 101, "mergedAt": "2026-01-01T00:00:00Z"}]))
        if "/pulls/" in joined:
            seen.append(joined)
            return P(M + "\n")
        return P(PARENT + "\n")

    collect.subprocess.run = fake_run
    collect.compare_status = lambda repo, base, head: "ahead"
    try:
        collect.bootstrap_cursor("PDE")
    finally:
        collect.subprocess.run, collect.compare_status = orig_run, orig_cmp
    assert any("/pulls/101" in s for s in seen), seen
    assert not any("/pulls/100" in s for s in seen), seen


def test_a_merge_commit_off_the_documented_history_is_refused():
    """`merge_commit_sha` means different things per merge method, and a pull request merged
    elsewhere need not touch this history at all, so it is checked rather than trusted."""
    for status in (None, "behind", "diverged"):
        assert _bootstrap(statuses={(M, "docgen"): status, (PARENT, "docgen"): "ahead",
                                    (PARENT, M): "ahead"}) is None, status


def test_a_cursor_off_the_documented_history_is_refused():
    assert _bootstrap(statuses={(M, "docgen"): "ahead", (PARENT, "docgen"): None,
                                (PARENT, M): "ahead"}) is None


def test_a_cursor_not_before_its_merge_is_refused():
    assert _bootstrap(statuses={(M, "docgen"): "ahead", (PARENT, "docgen"): "ahead",
                                (PARENT, M): "identical"}) is None


def test_a_root_commit_with_no_parent_is_refused_rather_than_guessed():
    assert _bootstrap(parent="", statuses={(M, "docgen"): "ahead"}) is None


def test_no_labelled_pull_requests_means_no_cursor():
    assert _bootstrap(number="") is None

for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
