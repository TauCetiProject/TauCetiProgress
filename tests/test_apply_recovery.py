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
        "prs": [1], "status_path": "Area/STATUS.md", "progress_path": "Area/PROGRESS.md"}
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
