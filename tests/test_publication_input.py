"""Real Git recovery across policy changes, local interruption and remote reuse."""

import contextlib
import copy
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply, cli, gh, publication_input, window  # noqa: E402
from progress.provenance import MigrationMap, retain_migration  # noqa: E402


class PublicationInput(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-input-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.repo = self.root / "roadmap"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "Area").mkdir()
        (self.repo / "Area/README.md").write_text("Roadmap\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", "-q", str(self.remote))
        self.git("remote", "add", "publish", str(self.remote))
        self.status, self.section = self.root / "status", self.root / "section"
        self.status.write_text("This retained prose describes the mathematics in this precise window. " * 5)
        self.section.write_text(self.status.read_text())
        self.empty = MigrationMap()
        self.a = MigrationMap(commits={"c" * 40: None})
        self.b = MigrationMap(commits={"d" * 40: None})
        self.plan = {"roadmap": "Area", "rel_dir": "Area", "from_sha": "a" * 40, "to_sha": "b" * 40,
                     "prs": [7], "status_path": "Area/STATUS.md", "progress_path": "Area/PROGRESS.md",
                     "documentation": {"facts_sha256": "f" * 64, "bundle_file": "old.gz"}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name in ("existing_pr", "own_pr"):
            stack.enter_context(patch.object(apply, name, return_value=None))
        stack.enter_context(patch.object(apply, "push_target", return_value=("publish", None)))
        stack.enter_context(patch.object(apply, "sweep", return_value=0))
        self.no_github = stack.enter_context(patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub I/O")))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def planned(self, policy):
        return {**copy.deepcopy(self.plan), "history_migration_sha256": policy.digest}

    def run_apply(self, policy, *, plan=None, dry_run=True, missing_prose=False):
        with window.using_migration(policy):
            return apply.run(plan or self.planned(policy),
                             self.root / "missing" if missing_prose else self.status,
                             self.root / "missing" if missing_prose else self.section,
                             self.repo, dry_run=dry_run)

    def bytes(self):
        return [(self.repo / self.plan[key]).read_bytes() for key in ("status_path", "progress_path")]

    def test_same_input_reuses_exact_commit_without_reading_or_regenerating_prose(self):
        self.assertEqual(self.run_apply(self.a), 0)
        original = self.git("rev-parse", "HEAD")
        moved = self.planned(self.a)
        moved["documentation"]["bundle_file"] = "new-location.gz"
        moved["history_migration_file"] = "moved-policy.json"
        with patch.object(apply, "render_update", side_effect=AssertionError("regenerated paid report")):
            self.assertEqual(self.run_apply(self.a, plan=moved, missing_prose=True), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertIn(publication_input.TRAILER + publication_input.digest(moved), self.git("show", "-s", "--format=%B"))

    def test_local_commit_cannot_be_relabelled_under_another_policy_or_facts(self):
        self.assertEqual(self.run_apply(self.a), 0)
        original, content = self.git("rev-parse", "HEAD"), self.bytes()
        self.assertEqual(self.run_apply(self.b, missing_prose=True), 78)
        changed = self.planned(self.a)
        changed["documentation"]["facts_sha256"] = "e" * 64
        self.assertEqual(self.run_apply(self.a, plan=changed, missing_prose=True), 78)
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertEqual(self.bytes(), content)
        self.no_github.assert_not_called()

    def test_pending_files_freeze_policy_before_the_commit_and_recover_without_rewriting(self):
        real_run = apply._run

        def interrupted(args, *rest, **kwargs):
            if args[:2] == ["git", "commit"]:
                raise apply.ApplyError("interrupted before commit")
            return real_run(args, *rest, **kwargs)

        with patch.object(apply, "_run", side_effect=interrupted):
            with self.assertRaises(apply.ApplyError):
                self.run_apply(self.a)
        prepared = self.bytes()
        self.assertEqual(self.run_apply(self.b, missing_prose=True), 78)
        self.assertEqual(self.bytes(), prepared)
        self.assertEqual(self.run_apply(self.a), 0)
        self.assertEqual(self.bytes(), prepared)

    def test_unknown_old_commit_is_recoverable_only_under_the_legacy_empty_policy(self):
        self.assertEqual(self.run_apply(self.empty), 0)
        self.git("commit", "--amend", "-qm", "legacy report without input metadata")
        original = self.git("rev-parse", "HEAD")
        self.assertEqual(self.run_apply(self.a, missing_prose=True), 78)
        self.assertEqual(self.run_apply(self.empty, missing_prose=True), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), original)

    def test_remote_old_policy_never_gets_a_new_policy_pr_body(self):
        self.assertEqual(self.run_apply(self.a), 0)
        branch = apply.branch_name(self.plan)
        original = self.git("rev-parse", "HEAD")
        self.git("push", "publish", "HEAD:refs/heads/" + branch)
        self.git("checkout", "-q", "main")
        self.git("branch", "-D", branch)
        self.assertEqual(self.run_apply(self.b), 0)
        local = self.git("rev-parse", "HEAD")
        self.assertNotEqual(original, local)
        self.assertEqual(self.run_apply(self.b, dry_run=False, missing_prose=True), 78)
        self.assertEqual(self.git("rev-parse", "HEAD"), local)
        self.assertEqual(self.git("ls-remote", "publish", "refs/heads/" + branch).split()[0], original)
        self.no_github.assert_not_called()

    def test_unbound_legacy_remote_still_has_to_match_the_actual_full_window(self):
        self.assertEqual(self.run_apply(self.empty), 0)
        branch = apply.branch_name(self.plan)
        for path in (self.plan["status_path"], self.plan["progress_path"]):
            target = self.repo / path
            target.write_text(target.read_text().replace("b" * 40, "b" * 39 + "c"))
        self.git("add", ".")
        self.git("commit", "--amend", "-qm", "legacy report for a different full window")
        self.git("push", "publish", "HEAD:refs/heads/" + branch)
        with self.assertRaises(publication_input.InputMismatch):
            apply.require_remote_input(self.planned(self.empty), branch, "publish", self.repo)

    def test_cli_restores_the_saved_policy_for_actual_commit_preparation(self):
        saved = self.root / "plan.json"
        plan = self.planned(self.a)
        plan["history_migration_file"] = retain_migration(self.a, saved)
        saved.write_text(json.dumps(plan))
        # No active map is needed: cmd_apply must use the verified saved snapshot.
        with window.using_migration(self.empty):
            self.assertEqual(cli.main(["apply", "--plan", str(saved), "--status-body", str(self.status),
                                       "--section-body", str(self.section), "--roadmap-dir", str(self.repo), "--dry-run"]), 0)
        self.assertIn(publication_input.digest(plan), self.git("show", "-s", "--format=%B"))


if __name__ == "__main__":
    unittest.main()
