"""Native and bors merges share commit windows, including the gate's bootstrap cursor."""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from progress import files, gh, plan, window

spec = importlib.util.spec_from_file_location("collect", ROOT / ".github/scripts/collect.py")
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)


class BorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.code = pathlib.Path(self.tmp.name) / "code"
        self.roadmap = pathlib.Path(self.tmp.name) / "roadmap"
        self.env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e",
                        GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")
        self.run_git("init", "-q", "-b", "main", str(self.code))
        self.root = self.commit("root")
        # Same (#number) subjects, different GitHub PR states.
        self.bors = self.commit("feat: bors squash (#101)")
        self.native = self.commit("feat: merge queue squash (#100)")
        self.run_git("-C", str(self.code), "checkout", "-q", "-b", "topic", self.native)
        self.commit("internal reference (#999)")
        self.run_git("-C", str(self.code), "checkout", "-q", "main")
        self.run_git("-C", str(self.code), "merge", "--no-ff", "-q", "topic",
                     "-m", "Merge pull request #102 from example/topic")
        self.tip = window.head_sha(self.code, ref="main")
        self.area = self.roadmap / "TauCetiRoadmap/PDE"
        self.area.mkdir(parents=True)
        (self.area / "README.md").write_text("# PDE\n")
        self.rows = [
            {"number": 100, "mergedAt": "2026-01-01T00:00:00Z", "state": "MERGED"},
            {"number": 101, "mergedAt": None, "state": "CLOSED"},
            {"number": 102, "mergedAt": "2026-01-01T00:00:00Z", "state": "MERGED"},
            # Even a misleading title cannot put an unlanded PR into a window.
            {"number": 103, "mergedAt": None, "state": "CLOSED",
             "title": "[Merged by Bors] - rejected"},
            {"number": 999, "mergedAt": None, "state": "CLOSED"},
        ]

    def run_git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, env=self.env).stdout.strip()

    def commit(self, subject):
        self.run_git("-C", str(self.code), "commit", "-q", "--allow-empty", "-m", subject)
        return window.head_sha(self.code, ref="HEAD")

    def github(self, args, **kwargs):
        if args[:2] == ["pr", "list"]:
            self.assertIn("roadmap/PDE", args)
            state = args[args.index("--state") + 1]
            # Reproduce GitHub's omission of bors closures from the merged query.
            rows = self.rows if state == "closed" else [r for r in self.rows if r["mergedAt"]]
            return json.dumps(rows)
        if args[:2] == ["api", "user"]:
            return "operator"
        if args[0] == "api" and "/pulls/" in args[1]:
            # For unmerged PRs the API may report a test merge SHA reachable from main.
            # Only the native-merge guard makes that irrelevant to bootstrap.
            self.assertEqual(args[-1], 'if .merged then (.merge_commit_sha // "") else "" end')
            return ""
        self.fail(f"unexpected network request: {args}")

    def build(self):
        with patch.object(gh, "gh", side_effect=self.github), \
             patch.object(plan, "docs_source_commit", return_value=self.tip):
            return plan.build_plan(self.roadmap, self.code, commits=[], open_prs=[],
                                   ref="main", min_prs=1)

    def test_first_report_includes_bors_and_native_merges_only(self):
        result = self.build()
        self.assertEqual(result["prs"], [102, 100, 101])
        self.assertEqual(result["from_sha"], self.root)

    def test_existing_cursor_keeps_half_open_window(self):
        (self.area / "PROGRESS.md").write_text(
            files.new_progress_file("PDE") + files.render_section(
                "PDE", self.root, self.bors, [101], "2026-01-01", "prose " * 60))
        result = self.build()
        self.assertEqual(result["prs"], [102, 100])
        self.assertEqual(result["from_sha"], self.bors)

    def test_only_rejected_prs_do_not_bootstrap(self):
        self.rows = [self.rows[3]]
        with self.assertRaises(plan.NotDue):
            self.build()

    def test_gate_and_planner_agree_on_bors_bootstrap(self):
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[:2] == ["git", "clone"]:
                args = [*args[:-2], str(self.code), args[-1]]
            return real_run(args, **kwargs)

        with patch.object(gh, "gh", side_effect=self.github), \
             patch.object(subprocess, "run", side_effect=run):
            cursor = collect.bootstrap_cursor("PDE", ref="main")
        self.assertEqual(cursor, self.root)

    def test_query_failure_does_not_become_empty_history(self):
        with patch.object(gh, "gh", side_effect=gh.GhError("rate limit")):
            with self.assertRaises(gh.GhError):
                gh.closed_prs_for_area("PDE")
            self.assertIsNone(collect.bootstrap_cursor("PDE"))


if __name__ == "__main__":
    unittest.main()
