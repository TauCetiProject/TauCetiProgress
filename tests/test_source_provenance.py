"""Real mainline windows keep native and historical PR namespaces separate."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from progress import facts, window
from progress.provenance import MigrationMap, ProvenanceError, PullSource, commit_source


class SourceWindows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-provenance-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.github = RepositoryRef(GITHUB, "TauCetiProject/TauCeti")
        self.native = RepositoryRef(Forge("forgejo", "https://forge.example"), "TauCetiProject/TauCeti")
        self.other = RepositoryRef(Forge("forgejo", "https://other.example"), self.native.name)
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True, capture_output=True)
        self.initial = self.commit("initial")

    def commit(self, message):
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--allow-empty", "-q", "-m", message],
            env=self.env,
            check=True,
            capture_output=True,
        )
        return window.git(["rev-parse", "HEAD"], self.root).strip()

    def test_identical_numbers_on_three_origins_remain_distinct(self):
        citations = [PullSource(repo, 7) for repo in (self.github, self.native, self.other)]
        self.assertEqual(len(set(citations)), 3)
        for citation in citations:
            self.assertEqual(PullSource.parse(citation.key), citation)
        self.assertEqual(PullSource.parse("github:https://GITHUB.com:443/taucetiproject/TAUCETI#7"), citations[0])

    def test_qualified_body_is_used_even_when_repair_notes_follow_trailers(self):
        native = PullSource(self.native, 8)
        sha = self.commit(f"landed (#8)\n\nSource-PR: {native.key}\nAccepted-Head: {'a' * 40}\n\nRepair explanation.")
        self.assertEqual(window.first_parent_sources(self.root, "main"), [(sha, native)])
        self.assertEqual(window.window_prs(self.root, self.initial, sha, repository=self.native), [8])
        self.assertEqual(window.find_merge_commit(self.root, 8, ref="main", repository=self.native), sha)

    def test_github_reader_refuses_native_number_collision_instead_of_misattributing_facts(self):
        self.commit("historical GitHub merge (#7)")
        tip = self.commit(f"native merge (#7)\n\nSource-PR: {PullSource(self.native, 7).key}")
        with self.assertRaisesRegex(window.GitError, "explicit migration"):
            window.window_prs(self.root, self.initial, tip)
        with self.assertRaisesRegex(window.GitError, "explicit migration"):
            facts.pr_commits(self.root, self.initial, tip, [7])

    def test_explicit_renumbering_map_drives_window_and_earliest_merge(self):
        before = self.commit("old GitHub merge (#7)")
        tip = self.commit(f"new native merge (#7)\n\nSource-PR: {PullSource(self.native, 7).key}")
        mapping = MigrationMap.parse(
            {
                "schema": "tauceti-progress.migration/v1",
                "mappings": [
                    {"from": PullSource(self.github, 7).key, "to": PullSource(self.native, 103).key},
                ],
            }
        )
        options = {"repository": self.native, "legacy_repository": self.github, "migration": mapping}
        self.assertEqual(window.window_prs(self.root, self.initial, tip, **options), [7, 103])
        self.assertEqual(window.earliest_merged(self.root, [7, 103], ref="main", **options), (103, before))
        self.assertEqual(window.find_merge_commit(self.root, 7, ref="main", **options), tip)
        self.assertEqual(window.find_merge_commit(self.root, 103, ref="main", **options), before)
        self.assertEqual(window.window_prs(self.root, before, tip, **options), [7])

    def test_unqualified_history_requires_an_explicit_legacy_namespace(self):
        tip = self.commit("historical GitHub merge (#7)")
        with self.assertRaisesRegex(window.GitError, "explicit legacy repository"):
            window.window_prs(self.root, self.initial, tip, repository=self.native)
        with self.assertRaisesRegex(window.GitError, "explicit migration"):
            window.window_prs(self.root, self.initial, tip, repository=self.native, legacy_repository=self.github)

    def test_legacy_source_trailer_is_github_even_under_a_native_reader(self):
        citation = commit_source("landed (#7)\n\nSource-PR: TauCetiProject/TauCeti#7", legacy_repository=self.native)
        self.assertEqual(citation, PullSource(self.github, 7))

    def test_ambiguous_malformed_or_conflicting_source_never_falls_back_to_subject(self):
        source = PullSource(self.native, 7).key
        messages = [
            f"landed (#7)\n\nSource-PR: {source}\nSource-PR: {PullSource(self.other, 7).key}",
            "landed (#7)\n\nSource-PR: invalid",
            "landed (#7)\n\nSource-PR: ",
            "landed (#7)\n\nSource-PR invalid",
            f"landed (#8)\n\nSource-PR: {source}",
        ]
        for message in messages:
            with self.subTest(message=message), self.assertRaises(ProvenanceError):
                commit_source(message, legacy_repository=self.github)
        self.assertEqual(
            commit_source(f"landed (#7)\n\nSource-PR: {source}\nSource-PR: {source}"), PullSource(self.native, 7)
        )

    def test_migration_duplicate_sources_destinations_and_wrong_target_refuse(self):
        old = PullSource(self.github, 7)
        new = PullSource(self.native, 103)
        for rows in ([(old, new), (old, new)], [(old, new), (PullSource(self.github, 8), new)]):
            with self.assertRaises(ProvenanceError):
                MigrationMap(rows)
        with self.assertRaises(ProvenanceError):
            MigrationMap([(old, new)]).number(old, self.other)
        with self.assertRaises(ProvenanceError):
            MigrationMap.parse({"schema": "future", "mappings": []})

    def test_original_github_subjects_and_source_trailers_still_work(self):
        one = self.commit("one (#1)\n\nSource-PR: TauCetiProject/TauCeti#1")
        two = self.commit("Merge pull request #2 from contributor/topic")
        self.assertEqual(window.window_prs(self.root, self.initial, two), [2, 1])
        self.assertEqual(facts.pr_commits(self.root, self.initial, two, [1]), {one: 1})
        self.assertEqual(window.earliest_merged(self.root, [1, 2], ref="main"), (1, one))

    def test_round_cache_reuses_immutable_history_but_ref_moves_and_new_rounds_reread(self):
        self.commit("one (#1)")
        original = window.git
        log_reads = []

        def counted(args, repo):
            if args[0] == "log":
                log_reads.append(args)
            return original(args, repo)

        @window.history_round
        def round_():
            first = window.first_parent_sources(self.root, "main", legacy_repository=self.github)
            first.clear()
            self.assertEqual(len(window.first_parent_sources(self.root, "main", legacy_repository=self.github)), 1)
            self.assertEqual(len(log_reads), 1)
            self.commit("two (#2)")
            self.assertEqual(len(window.first_parent_sources(self.root, "main", legacy_repository=self.github)), 2)
            self.assertEqual(len(log_reads), 2)

        with patch.object(window, "git", side_effect=counted):
            round_()
            window.first_parent_sources(self.root, "main", legacy_repository=self.github)
        self.assertEqual(len(log_reads), 3)

    def test_round_cache_bounds_and_legacy_namespaces_are_kept_separate(self):
        one = self.commit("one (#1)")
        self.commit("two (#2)")
        original = window.git
        reads = []

        def counted(args, repo):
            if args[0] == "log":
                reads.append(args)
            return original(args, repo)

        @window.history_round
        def round_():
            for _ in range(2):
                window.first_parent_sources(self.root, "main", legacy_repository=self.github)
            self.assertEqual(len(reads), 2)  # Two citations exceed the one-row cache bound.
            self.assertEqual(
                window.first_parent_sources(self.root, one, legacy_repository=self.github)[0][1].repository, self.github
            )
            self.assertEqual(
                window.first_parent_sources(self.root, one, legacy_repository=self.other)[0][1].repository, self.other
            )
            cache = window._HISTORY_ROUND.get()
            self.assertLessEqual(len(cache), 1)
            self.assertLessEqual(sum(map(len, cache.values())), 1)

        with (
            patch.object(window, "MAX_HISTORY_ENTRIES", 1),
            patch.object(window, "MAX_HISTORY_ROWS", 1),
            patch.object(window, "git", side_effect=counted),
        ):
            round_()
        self.assertIsNone(window._HISTORY_ROUND.get())


if __name__ == "__main__":
    unittest.main()
