"""Tests for the merge gate.

These are the most important tests here. The roadmap App bypasses the whole ruleset, so this gate is
the only thing preventing an unwanted change to a human-owned repository. Every check therefore has
an explicit negative test, and the default expectation is refusal.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import files, gate  # noqa: E402
from progress.gate import Refused  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def refuses(fn, needle=None):
    try:
        fn()
    except Refused as exc:
        if needle and needle not in str(exc):
            raise AssertionError(f"refused for the wrong reason: {str(exc)!r}") from None
        return str(exc)
    raise AssertionError("expected a refusal, the gate ALLOWED this")


REPO = "TauCetiProject/TauCetiRoadmap"
OK_IDS = [477956]
FROM = "1f1d752" + "0" * 33
TO = "3f41440" + "0" * 33
HEAD = "f" * 40
AREA = "ReductiveGroups"
BRANCH = f"progress/{FROM[:7]}-{TO[:7]}/{AREA}"


def make_pr(**over):
    pr = {
        "state": "open",
        "draft": False,
        "user": {"id": 477956, "login": "kim-em"},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
    }
    pr.update(over)
    return pr


def make_files(area=AREA, names=("STATUS.md", "PROGRESS.md"), status="modified", **over):
    out = []
    for n in names:
        f = {"filename": f"TauCetiRoadmap/{area}/{n}", "status": status}
        f.update(over)
        out.append(f)
    return out


def make_tree(area=AREA, mode="100644"):
    return [
        {"path": f"TauCetiRoadmap/{area}/STATUS.md", "mode": mode, "type": "blob"},
        {"path": f"TauCetiRoadmap/{area}/PROGRESS.md", "mode": mode, "type": "blob"},
    ]


def make_content(from_sha=FROM, to_sha=TO, prs=(1, 2, 3), area=AREA, prose="What landed."):
    old_progress = files.new_progress_file(area)
    new_progress = old_progress + files.render_section(area, from_sha, to_sha, list(prs), "w", prose)
    new_status = files.render_status(area, to_sha, "2026-07-30T00:00:00Z", "Where we are.")
    return None, new_status, old_progress, new_progress


CHECKS_OK = [{"name": "build", "head_sha": HEAD, "conclusion": "success"}]


def call(pr=None, changed=None, tree=None, content=None, checks=None, cursor=None, ids=None):
    old_status, new_status, old_progress, new_progress = content or make_content()
    return gate.decide(
        pr=pr or make_pr(),
        changed_files=changed if changed is not None else make_files(),
        tree_entries=tree if tree is not None else make_tree(),
        old_status=old_status,
        new_status_bytes=new_status.encode(),
        old_progress=old_progress,
        new_progress_bytes=new_progress.encode(),
        check_runs=checks if checks is not None else CHECKS_OK,
        allowed_user_ids=ids if ids is not None else OK_IDS,
        base_repo=REPO,
        current_main_cursor=cursor,
    )


# ----- the happy path (exactly one) -------------------------------------------------------------


def test_allows_a_well_formed_update():
    result = call()
    assert result["area"] == AREA
    assert result["head_sha"] == HEAD
    assert result["section"]["prs"] == [1, 2, 3]
    assert AREA in gate.summary(result)


# ----- provenance ------------------------------------------------------------------------------


def test_refuses_a_fork():
    pr = make_pr(head={"ref": BRANCH, "sha": HEAD, "repo": {"full_name": "attacker/TauCetiRoadmap"}})
    refuses(lambda: call(pr=pr), "forks are never auto-merged")


def test_refuses_an_unlisted_author():
    pr = make_pr(user={"id": 999999, "login": "kim-em"})
    # Note the login still says kim-em: identity is the numeric id precisely so that a renamed or
    # impersonated login cannot pass.
    refuses(lambda: call(pr=pr), "not in the allowlist")


def test_refuses_a_draft():
    refuses(lambda: call(pr=make_pr(draft=True)), "draft")


def test_refuses_a_closed_pr():
    refuses(lambda: call(pr=make_pr(state="closed")), "not open")


def test_refuses_a_wrong_base_branch():
    pr = make_pr(base={"ref": "gh-pages", "repo": {"full_name": REPO}})
    refuses(lambda: call(pr=pr), "base branch")


def test_refuses_a_non_progress_branch():
    pr = make_pr(head={"ref": "patch-1", "sha": HEAD, "repo": {"full_name": REPO}})
    refuses(lambda: call(pr=pr), "not a progress branch")


def test_refuses_a_branch_whose_area_is_not_alphanumeric():
    pr = make_pr(head={"ref": f"progress/{FROM[:7]}-{TO[:7]}/../../etc", "sha": HEAD,
                       "repo": {"full_name": REPO}})
    refuses(lambda: call(pr=pr), "not a progress branch")


# ----- diff shape ------------------------------------------------------------------------------


def test_refuses_an_extra_path():
    changed = make_files() + [{"filename": f"TauCetiRoadmap/{AREA}/README.md", "status": "modified"}]
    refuses(lambda: call(changed=changed), "not an allowed generated file")


def test_refuses_a_workflow_path():
    changed = make_files() + [{"filename": ".github/workflows/evil.yml", "status": "added"}]
    refuses(lambda: call(changed=changed), "not an allowed generated file")


def test_refuses_status_only():
    """The motivating case for requiring both files: the cursor would advance past unwritten prose."""
    refuses(lambda: call(changed=make_files(names=("STATUS.md",))), "missing required file")


def test_refuses_progress_only():
    refuses(lambda: call(changed=make_files(names=("PROGRESS.md",))), "missing required file")


def test_refuses_files_from_another_area():
    changed = make_files(area="PDE")
    refuses(lambda: call(changed=changed), "not in the ReductiveGroups directory")


def test_refuses_a_deletion():
    refuses(lambda: call(changed=make_files(status="removed")), "only added or modified")


def test_refuses_a_rename():
    changed = make_files()
    changed[0]["previous_filename"] = "TauCetiRoadmap/PDE/STATUS.md"
    refuses(lambda: call(changed=changed), "rename")


def test_refuses_no_change():
    refuses(lambda: call(changed=[]), "no files changed")


def test_refuses_writing_into_two_parent_directories():
    """An area name can exist under BOTH `TauCetiRoadmap/` and `Completed/` (that is where a finished
    roadmap is archived). Keying only on the basename let a PR change all FOUR paths and pass, because
    both basenames were present and every path matched; the content validators then looked at only one
    pair, so the other two would have merged unexamined."""
    changed = make_files() + [
        {"filename": f"Completed/{AREA}/STATUS.md", "status": "modified"},
        {"filename": f"Completed/{AREA}/PROGRESS.md", "status": "modified"},
    ]
    refuses(lambda: call(changed=changed), "one directory")


def test_refuses_a_duplicated_path():
    changed = make_files() + [{"filename": f"TauCetiRoadmap/{AREA}/STATUS.md", "status": "modified"}]
    refuses(lambda: call(changed=changed), "appears twice")


def test_refuses_more_files_than_allowed():
    """Belt and braces alongside the both-files check: a repeated path is caught too."""
    changed = make_files()
    changed.append({"filename": f"TauCetiRoadmap/{AREA}/STATUS.md", "status": "added"})
    refuses(lambda: call(changed=changed), "appears twice")


# ----- modes -----------------------------------------------------------------------------------


def test_refuses_when_a_tree_entry_is_missing():
    """FAIL-CLOSED. Iterating only over the entries handed in meant an EMPTY list passed vacuously --
    and the collector produced exactly that whenever a per-path fetch failed, so the symlink defence
    (the one check that must never fail open) could be skipped by making a fetch fail."""
    refuses(lambda: call(tree=[]), "no tree entry")
    partial = [{"path": f"TauCetiRoadmap/{AREA}/STATUS.md", "mode": "100644", "type": "blob"}]
    refuses(lambda: call(tree=partial), "no tree entry")


def test_refuses_an_executable_mode():
    """The contents api reports a 100755 blob as plain type "file", so modes now come from the tree
    api. Harmless in itself for markdown, but the gate claimed to check modes and did not."""
    refuses(lambda: call(tree=make_tree(mode="100755")), "not a regular file")


def test_refuses_unexpected_tree_entries():
    tree = make_tree() + [{"path": "README.md", "mode": "100644", "type": "blob"}]
    refuses(lambda: call(tree=tree), "unexpected tree entries")


def test_refuses_a_symlink():
    """A symlink named STATUS.md is the classic way to escape a path restriction."""
    refuses(lambda: call(tree=make_tree(mode="120000")), "not a regular file")


def test_refuses_a_submodule():
    tree = make_tree()
    tree[0] = {"path": tree[0]["path"], "mode": "160000", "type": "commit"}
    refuses(lambda: call(tree=tree), "not a file")


# ----- content ---------------------------------------------------------------------------------


def test_refuses_an_injected_marker_in_prose():
    content = make_content(prose='Landed. <!--tauceti-status:v1 {"roadmap":"PDE"}-->')
    refuses(lambda: call(content=content), "reserved marker")


def test_refuses_a_rewritten_log():
    """PROGRESS.md must grow only at the end."""
    old_status, new_status, old_progress, new_progress = make_content()
    tampered = "EDITED " + new_progress
    refuses(lambda: call(content=(old_status, new_status, old_progress, tampered)),
            "above the end")


def test_refuses_a_cursor_that_does_not_continue_current_main():
    """`main` may have moved on since the PR was opened; validating against a stale base would let
    the same window land twice."""
    refuses(lambda: call(cursor="9" * 40), "expected")


def test_allows_when_the_cursor_matches_current_main():
    result = call(cursor=FROM)
    assert result["section"]["from_sha"] == FROM


def test_refuses_mismatched_status_and_section():
    old_progress = files.new_progress_file(AREA)
    new_progress = old_progress + files.render_section(AREA, FROM, TO, [1], "w", "x")
    # Snapshot claims a different commit than the window ends at.
    new_status = files.render_status(AREA, "c" * 40, "t", "s")
    refuses(lambda: call(content=(None, new_status, old_progress, new_progress)), "must describe")


def test_refuses_invalid_utf8():
    old_status, new_status, old_progress, new_progress = make_content()
    bad = new_progress.encode()[:-3] + b"\xff\xfe"
    try:
        gate.decide(
            pr=make_pr(), changed_files=make_files(), tree_entries=make_tree(),
            old_status=old_status, new_status_bytes=new_status.encode(),
            old_progress=old_progress, new_progress_bytes=bad,
            check_runs=CHECKS_OK, allowed_user_ids=OK_IDS, base_repo=REPO,
        )
    except (Refused, files.FormatError) as exc:
        assert "UTF-8" in str(exc), str(exc)
        return
    raise AssertionError("expected a refusal")


def test_refuses_an_area_mismatch_between_branch_and_content():
    """The branch says ReductiveGroups; the files say PDE."""
    content = make_content(area="PDE")
    changed = make_files(area="PDE")
    refuses(lambda: call(changed=changed, content=content), "not in the ReductiveGroups directory")


# ----- build ------------------------------------------------------------------------------------


def test_refuses_a_failing_build():
    checks = [{"name": "build", "head_sha": HEAD, "conclusion": "failure"}]
    refuses(lambda: call(checks=checks), "concluded FAILURE")


def test_refuses_a_pending_build():
    checks = [{"name": "build", "head_sha": HEAD, "conclusion": None}]
    refuses(lambda: call(checks=checks), "concluded pending")


def test_refuses_a_missing_build():
    refuses(lambda: call(checks=[]), "has not reported")


def test_refuses_a_failing_build_listed_after_a_success():
    """A head can carry BOTH a check-run and a commit status named `build` (the collector appends
    check-runs, then statuses). Returning on the first match let a success hide a later failure."""
    checks = [
        {"name": "build", "head_sha": HEAD, "conclusion": "success"},
        {"name": "build", "head_sha": HEAD, "conclusion": "failure"},
    ]
    refuses(lambda: call(checks=checks), "concluded FAILURE")


def test_allows_when_every_build_entry_is_green():
    checks = [
        {"name": "build", "head_sha": HEAD, "conclusion": "success"},
        {"name": "build", "head_sha": HEAD, "conclusion": "success"},
    ]
    assert call(checks=checks)["area"] == AREA


def test_refuses_a_build_for_another_commit():
    """A green build on an earlier commit says nothing about the head being merged."""
    checks = [{"name": "build", "head_sha": "0" * 40, "conclusion": "success"}]
    refuses(lambda: call(checks=checks), "has not reported")


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
