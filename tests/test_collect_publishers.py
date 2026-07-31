"""Tests for how the collector obtains the allowlist.

This is the security-critical part of opening publishing up beyond one account: the allowlist is a
file in the repository, so *where it is read from* is the whole control. Read at the pull request's
head, a report could add its own author and merge itself.
"""

import importlib.util
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("collect", ROOT / ".github" / "scripts" / "collect.py")
collect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect)

from progress import publisher  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


HEAD = "h" * 40
MAIN = "m" * 40
AREA = "PDE"
BRANCH = f"progress/aaaaaaa-bbbbbbb/{AREA}"


def run_main(publishers_at, extra_files=None):
    """Drive `collect.main` against canned GitHub responses.

    `publishers_at` maps a ref to the publishers file text found there, so a test can put a
    *different* list at the head than at main and see which one is honoured. Returns
    `(bundle, file_at_calls)`.
    """
    calls = []
    orig = (collect.gh_api, collect.gh_api_paged_field, collect.blob_text, collect.file_at)

    def fake_gh_api(path):
        if path.endswith("/pulls/1"):
            return {"head": {"sha": HEAD, "ref": BRANCH}, "user": {"id": 477956}}
        if "/commits/main" in path:
            return {"sha": MAIN}
        if "/compare/" in path:
            return {"status": "ahead", "behind_by": 0, "files": (extra_files if extra_files is not None else [
                {"filename": f"TauCetiRoadmap/{AREA}/STATUS.md", "sha": "s" * 40},
                {"filename": f"TauCetiRoadmap/{AREA}/PROGRESS.md", "sha": "p" * 40},
            ])}
        if "/git/trees/" in path:
            return {"truncated": False, "tree": [
                {"path": f"TauCetiRoadmap/{AREA}/STATUS.md", "mode": "100644", "type": "blob", "sha": "s" * 40},
                {"path": f"TauCetiRoadmap/{AREA}/PROGRESS.md", "mode": "100644", "type": "blob", "sha": "p" * 40},
            ]}
        raise AssertionError(f"unexpected gh_api call: {path}")

    def fake_file_at(repo, ref, path):
        calls.append((ref, path))
        if path == publisher.PUBLISHERS_PATH:
            return publishers_at.get(ref)
        return None

    collect.gh_api = fake_gh_api
    collect.gh_api_paged_field = lambda path, field: []
    collect.blob_text = lambda repo, sha: ""
    collect.file_at = fake_file_at
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "bundle.json"
            collect.main(["--repo", "O/R", "--pr", "1", "--out", str(out)])
            return json.loads(out.read_text()), calls
    finally:
        (collect.gh_api, collect.gh_api_paged_field, collect.blob_text, collect.file_at) = orig


def test_the_allowlist_is_read_at_the_base_commit():
    bundle, calls = run_main({MAIN: "477956 kim-em\n"})
    assert (MAIN, publisher.PUBLISHERS_PATH) in calls
    assert bundle["allowed_user_ids"] == [477956]


def test_the_allowlist_is_never_read_at_the_head():
    """A pull request that adds its own author to the file must gain nothing by it."""
    bundle, calls = run_main({MAIN: "477956 kim-em\n", HEAD: "477956 kim-em\n999999 attacker\n"})
    assert (HEAD, publisher.PUBLISHERS_PATH) not in calls, "the head list must never be consulted"
    assert bundle["allowed_user_ids"] == [477956], bundle["allowed_user_ids"]


def test_several_publishers_all_reach_the_gate():
    bundle, _ = run_main({MAIN: "# who may publish\n477956 kim-em\n1234 someone\n5678 another\n"})
    assert bundle["allowed_user_ids"] == [1234, 5678, 477956]


def test_a_missing_allowlist_is_fatal_not_a_quiet_refusal():
    """Misconfiguration must turn the run red; refusing silently looks like 'nothing was due'."""
    try:
        run_main({})
    except SystemExit as exc:
        assert publisher.PUBLISHERS_PATH in str(exc), exc
    else:
        raise AssertionError("a missing allowlist should be fatal")


def test_a_malformed_allowlist_is_fatal():
    try:
        run_main({MAIN: "477956\nnot-an-id\n"})
    except SystemExit as exc:
        assert "not a numeric user id" in str(exc), exc
    else:
        raise AssertionError("a malformed allowlist should be fatal")


def test_an_empty_allowlist_collects_as_empty():
    """Valid but empty: nobody may publish. That is a refusal, not a crash."""
    bundle, _ = run_main({MAIN: "# nobody yet\n"})
    assert bundle["allowed_user_ids"] == []


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
