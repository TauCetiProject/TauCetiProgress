"""Resume a lost PR response using the same branch, prose and one GitHub mutation."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, gh

plan = {"roadmap": "Area", "rel_dir": "Area", "from_sha": "a" * 40, "to_sha": "b" * 40,
        "prs": [3, 1, 2], "status_path": "Area/STATUS.md", "progress_path": "Area/PROGRESS.md"}
prose = "The retained report describes the mathematical work in this exact window. " * 4
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": tmp + "/outbox"}):
    root = pathlib.Path(tmp)
    repo = root / "roadmap"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (repo / "Area").mkdir()
    (repo / "Area/README.md").write_text("Area\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    status, section = root / "status-body.md", root / "section-body.md"
    status.write_text(prose)
    section.write_text(prose)
    original = [status.read_bytes(), section.read_bytes()]
    published, mutations = [], []
    real_run = subprocess.run
    def run(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        args = argv[1:]
        if args[:2] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "author", "")
        if args[:2] == ["pr", "create"]:
            mutations.append(args)
            published.append({"url": "https://github.com/org/repo/pull/7", "headRepositoryOwner": {"login": "author"}})
            return subprocess.CompletedProcess(argv, 1, "", "connection reset after successful creation")
        if args[:2] == ["api", "graphql"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"data": {"repository": {"pullRequests": {
                "nodes": published, "pageInfo": {"hasNextPage": False}}}}}]), "")
        raise AssertionError(args)
    with patch.object(apply, "existing_pr", return_value=None), \
         patch.object(apply, "own_pr", side_effect=lambda *a, **kw: published[0] if published and kw.get("states") != ("closed",) else None), \
         patch.object(apply, "push_target", return_value=("fork", "author")), \
         patch.object(apply, "remote_branch_exists", return_value=True), \
         patch.object(apply, "sweep", return_value=0), patch.object(gh.subprocess, "run", side_effect=run):
        assert apply.run(plan, status, section, repo) == 0
        branch = git("branch", "--show-current")
        head = git("rev-parse", "HEAD")
        assert apply.run(plan, status, section, repo) == apply.EX_NOPROGRESS
        assert git("branch", "--show-current") == branch and git("rev-parse", "HEAD") == head
        assert len(mutations) == 1
        assert [status.read_bytes(), section.read_bytes()] == original
        assert not list((root / "outbox").glob("*.json"))
print("PASS: lost PR creation response resumes without duplicate mutation or prose changes")

# A pre-push failure leaves the deterministic local commit reusable on the next apply.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    repo = root / "roadmap"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (repo / "Area").mkdir()
    (repo / "Area/README.md").write_text("Area")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    status, section = root / "status", root / "section"
    status.write_text(prose)
    section.write_text(prose)
    with patch.object(apply, "existing_pr", return_value=None), patch.object(apply, "own_pr", return_value=None), \
         patch.object(apply, "push_target", side_effect=gh.GhError("fork pending")):
        for _ in range(2):
            try:
                apply.run(plan, status, section, repo)
            except gh.GhError:
                pass
            else:
                raise AssertionError("publication should remain pending")
            current = git("rev-parse", "HEAD")
            if _ == 0:
                retained_commit = current
            else:
                assert current == retained_commit, "resume must preserve the exact local report commit"
        # Unrelated main changes and a renderer upgrade preserve the original commit.
        git("checkout", "-q", "main")
        (repo / "UNRELATED.md").write_text("main advanced")
        git("add", ".")
        git("commit", "-qm", "unrelated merge")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        with patch.object(apply, "render_update", side_effect=AssertionError("must reuse saved bytes")):
            try:
                apply.run(plan, status, section, repo)
            except gh.GhError:
                pass
        assert git("rev-parse", "HEAD") == retained_commit
        # A changed window is terminal and leaves the branch intact.
        changed = {**plan, "prs": [2]}
        assert apply.run(changed, status, section, repo) == apply.EX_RETAINED
        assert git("rev-parse", "HEAD") == retained_commit
        # If the area's cursor has moved, return a terminal recovery result.
        git("checkout", "-q", "main")
        from progress import files
        (repo / plan["progress_path"]).write_text(files.new_progress_file("Area") + files.render_section(
            "Area", "9" * 40, "c" * 40, [9], "earlier", prose))
        git("add", ".")
        git("commit", "-qm", "area cursor advanced")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        assert apply.run(plan, status, section, repo) == apply.EX_RETAINED
        assert git("rev-parse", f"refs/heads/{apply.branch_name(plan)}") == retained_commit
print("PASS: interrupted fork publication preserves and reuses the exact local branch")

# A crash before commit leaves a resumable branch and the exact generated bytes.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    repo = root / "roadmap"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (repo / "Area").mkdir()
    (repo / "Area/README.md").write_text("Area")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    status, section = root / "status", root / "section"
    status.write_text(prose)
    section.write_text(prose)
    real_git = apply._run
    def crash_before_commit(args, *a, **kw):
        if args[:2] == ["git", "commit"]:
            raise apply.ApplyError("simulated interruption before commit")
        return real_git(args, *a, **kw)
    with patch.object(apply, "existing_pr", return_value=None), patch.object(apply, "own_pr", return_value=None), \
         patch.object(apply, "push_target", side_effect=gh.GhError("fork pending")):
        with patch.object(apply, "_run", side_effect=crash_before_commit):
            try:
                apply.run(plan, status, section, repo)
            except apply.ApplyError:
                pass
        before = (repo / plan["progress_path"]).read_bytes()
        try:
            apply.run(plan, status, section, repo)
        except gh.GhError:
            pass
        assert (repo / plan["progress_path"]).read_bytes() == before
        assert git("rev-list", "--count", "origin/main..HEAD") == "1"
print("PASS: pre-commit interruption resumes without a forced reset or prose regeneration")
