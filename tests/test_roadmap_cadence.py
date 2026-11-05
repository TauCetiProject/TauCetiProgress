"""Planning checks the fetched Roadmap history, independently of discovery."""

import datetime
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import cli, gh, plan, window


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "roadmap"
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com",
                        GIT_AUTHOR_DATE="2026-11-05T12:00:00Z", GIT_COMMITTER_DATE="2026-11-05T12:00:00Z")
        self.git("init", "-q", "-b", "main", str(self.repo))
        self.commit("progress: recent report")
        for i in range(31):
            self.commit(f"unrelated roadmap change {i}")
        self.git("-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], env=self.env, text=True).strip()

    def commit(self, title):
        self.git("-C", str(self.repo), "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", title)

    def test_planning_rechecks_fetched_history_without_api_or_thirty_commit_cutoff(self):
        now = datetime.datetime(2026, 11, 5, 13, tzinfo=datetime.timezone.utc)
        # Discovery could have said due before this report landed. Planning
        # must see the newly fetched report even behind >30 unrelated commits.
        with patch.object(gh, "recent_roadmap_commits", side_effect=AssertionError("duplicate API read")):
            with self.assertRaises(plan.NotDue):
                plan.build_plan(self.repo, self.repo, roadmap_ref="origin/main", now=now)

    def test_unpublished_local_report_does_not_change_the_fetched_cadence(self):
        before = plan.roadmap_commits(self.repo, "origin/main")
        self.env.update(GIT_AUTHOR_DATE="2026-11-06T12:00:00Z", GIT_COMMITTER_DATE="2026-11-06T12:00:00Z")
        self.commit("progress: not published")
        self.assertEqual(plan.roadmap_commits(self.repo, "origin/main"), before)

    def test_missing_ref_and_shallow_history_are_not_empty_history(self):
        with self.assertRaises(window.GitError):
            plan.roadmap_commits(self.repo, "origin/missing")
        shallow = pathlib.Path(self.tmp.name) / "shallow"
        self.git("clone", "-q", "--depth=1", self.repo.as_uri(), str(shallow))
        with self.assertRaises(window.GitError):
            plan.roadmap_commits(shallow, "origin/main")

    def test_cli_passes_explicit_ref_and_standalone_keeps_api_default(self):
        with patch.object(plan, "build_plan", side_effect=plan.NotDue("recent report")) as build:
            self.assertEqual(cli.main(["plan", "--roadmap-dir", str(self.repo),
                                       "--code-dir", str(self.repo), "--roadmap-ref", "origin/main"]), 75)
            self.assertEqual(build.call_args.kwargs["roadmap_ref"], "origin/main")
        self.assertIsNone(cli.build_parser().parse_args(
            ["plan", "--roadmap-dir", str(self.repo), "--code-dir", str(self.repo)]).roadmap_ref)


if __name__ == "__main__":
    unittest.main()
