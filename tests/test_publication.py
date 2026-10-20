"""Mutation retries require durable reconciliation; reads remain retryable."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, gh
from progress.forks import ForkUnknown, discover

args = ["repo", "fork", "org/repo", "--clone=false"]
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": tmp, "GH_TOKEN": "fixture"}):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "author\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "lost response")

    with patch.object(gh.subprocess, "run", side_effect=run), patch.object(gh.time, "sleep") as sleep:
        for _ in range(2):
            try:
                gh.gh(args, reconcile=lambda: None)
            except gh.GhError:
                pass
            else:
                raise AssertionError("uncertain write must remain pending")
        assert sum(c[1:] == args for c in calls) == 1
        sleep.assert_not_called()
        retained = next(pathlib.Path(tmp).glob("*.json"))
        assert json.loads(retained.read_text())["actor"] == "author"
        # A later verified fork acknowledges a lost success without another mutation.
        assert gh.gh(args, reconcile=lambda: "author/renamed") == "author/renamed"
        assert not retained.exists() and sum(c[1:] == args for c in calls) == 1
        # Authoritative absence permits one bounded retry after a grace period.
        try:
            gh.gh(args)
        except gh.GhError:
            pass
        retained = next(pathlib.Path(tmp).glob("*.json"))
        value = json.loads(retained.read_text())
        value["created"] -= 301
        value["absent_since"] = value["created"]
        retained.write_text(json.dumps(value))
        for _ in range(3):
            try:
                gh.gh(args, reconcile=lambda: False)
            except gh.GhError:
                pass
        assert sum(c[1:] == args for c in calls) == 3, "initial attempt, new operation, one retry"
        assert json.loads(retained.read_text())["attempts"] == 2
    with patch.object(gh.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "outage")) as run, patch.object(gh.time, "sleep"):
        try:
            gh.gh(["api", "repos/org/repo"])
        except gh.GhError:
            pass
        assert run.call_count == 3

# The protocol rejects partial reads, missing permissions, truncated pages and malformed ancestry.
def page():
    return [{"data": {"viewer": {"login": "author", "repository": None, "repositories": {
        "nodes": [{"id": "F", "nameWithOwner": "author/renamed", "owner": {"login": "author"},
                   "isFork": True, "isArchived": False, "isLocked": False, "viewerPermission": "WRITE", "parent": {
                       "id": "U", "nameWithOwner": "org/repo", "parent": None}}],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}},
        "repository": {"id": "U", "nameWithOwner": "org/repo", "isPrivate": False}}}]
assert discover(lambda _: json.dumps(page()), "org/repo")["fork"] == "author/renamed"
for change in ("partial", "permission", "pagination", "owner", "ancestry"):
    data = page()
    node = data[0]["data"]["viewer"]["repositories"]["nodes"][0]
    if change == "partial":
        data[0]["errors"] = [{"message": "outage"}]
    elif change == "permission":
        node["viewerPermission"] = None
    elif change == "pagination":
        data[0]["data"]["viewer"]["repositories"]["pageInfo"]["hasNextPage"] = True
    elif change == "owner":
        node["owner"]["login"] = "other"
    else:
        node["parent"] = {"id": "OTHER", "nameWithOwner": "org/repo"}
    try:
        discover(lambda _: json.dumps(data), "org/repo")
    except ForkUnknown:
        pass
    else:
        raise AssertionError(change)
print("PASS: retained mutation reconciliation and strict fork discovery")

# A successful asynchronous fork response is still pending until it becomes visible.
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": tmp}):
    def successful(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "author" if argv[1:3] == ["api", "user"] else "accepted", "")
    with patch.object(gh.subprocess, "run", side_effect=successful) as run:
        for _ in range(2):
            try:
                gh.gh(args, reconcile=lambda: None)
            except gh.GhError:
                pass
            else:
                raise AssertionError("asynchronous success needs verified visibility")
        assert sum(c.args[0][1:] == args for c in run.call_args_list) == 1
        assert list(pathlib.Path(tmp).glob("*.json"))

# Remote read outages cannot authorize another push, and create-only pushes cannot replace a branch.
for code in (0, 2, 128):
    with patch.object(apply, "_run", return_value=subprocess.CompletedProcess([], code, "", "offline")):
        try:
            exists = apply.remote_branch_exists("/unused", "progress/window/Area")
        except apply.ApplyError:
            assert code == 128
        else:
            assert exists is (code == 0)

# The Worker proxy is the sole journal owner when installed, including local waits.
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "TAUCETI_PROGRESS_OUTBOX": tmp, "TAUCETI_REAL_GH": "/fixture/native-gh"}):
    with patch.object(gh.subprocess, "run", return_value=subprocess.CompletedProcess([], 77, "", "retained")) as run:
        for _ in range(3):
            try:
                gh.gh(args, reconcile=lambda: None)
            except gh.GhError:
                pass
        assert run.call_count == 3 and not list(pathlib.Path(tmp).glob("*.json"))

# Complete absence can authorize PR creation retry; incomplete lookup cannot.
for nodes, more, expected in (([], False, False), ([], True, None),
    ([{"url": "url", "headRepositoryOwner": {"login": "author"}}], False, "url")):
    raw = json.dumps([{"data": {"repository": {"pullRequests": {"nodes": nodes,
                               "pageInfo": {"hasNextPage": more}}}}}])
    with patch.object(gh, "gh", return_value=raw):
        assert apply.reconcile_pr("progress/window/Area", "author", "org/repo") == expected

# push_target reads before creating, and verifies ancestry/permission after a lost creation response.
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": tmp}):
    os.environ.pop("TAUCETI_REAL_GH", None)
    created, calls = [], []
    def fork_cli(argv, **kwargs):
        args = argv[1:]
        calls.append(args)
        if args[:2] == ["api", "repos/org/repo"]:
            return subprocess.CompletedProcess(argv, 0, "false", "")
        if args[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "author", "")
        if args[:2] == ["api", "graphql"]:
            data = page()
            if not created:
                data[0]["data"]["viewer"]["repositories"]["nodes"] = []
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        if args[:2] == ["repo", "fork"]:
            created.append(True)
            return subprocess.CompletedProcess(argv, 1, "", "lost successful fork response")
        raise AssertionError(args)
    with patch.object(gh.subprocess, "run", side_effect=fork_cli), patch.object(apply, "_run", return_value=subprocess.CompletedProcess([], 1, "", "")):
        assert apply.push_target("/unused", "org/repo") == ("fork", "author")
    assert len(created) == 1
    first_read = next(i for i, call in enumerate(calls) if call[:2] == ["api", "graphql"])
    write = next(i for i, call in enumerate(calls) if call[:2] == ["repo", "fork"])
    assert first_read < write and sum(call[:2] == ["api", "graphql"] for call in calls) == 3
    assert not list(pathlib.Path(tmp).glob("*.json"))
