"""Real mainline windows keep native and historical PR namespaces separate."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

from progress import cli, facts, gh, plan, window
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
            f"landed (#8)\n\nSource-PR: {source}",
        ]
        for message in messages:
            with self.subTest(message=message), self.assertRaises(ProvenanceError):
                commit_source(message, legacy_repository=self.github)
        self.assertEqual(
            commit_source(f"landed (#7)\n\nSource-PR: {source}\nSource-PR: {source}"), PullSource(self.native, 7)
        )

    def test_queue_trailer_lookalikes_are_prose_and_do_not_block_history(self):
        for line in (
            "Source-PR trailers now carry identity",
            "Source-PR-Notes: x",
            "Source-PR : x",
            "Source-PR:",
            "Source-PR: ",
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    commit_source("landed (#7)\n\n" + line, legacy_repository=self.github), PullSource(self.github, 7)
                )
        tip = self.commit("landed (#7)\n\nSource-PR trailers now carry identity\nSource-PR-Notes: x")
        self.assertEqual(window.window_prs(self.root, self.initial, tip), [7])

    def test_git_multiline_subject_paragraph_retains_its_original_number(self):
        tip = self.commit("A long title\ncontinued (#7)\n\nBody without trailers")
        self.assertEqual(window.window_prs(self.root, self.initial, tip), [7])

    def test_distinct_citations_cannot_collapse_onto_one_current_number(self):
        old, new = PullSource(self.github, 7), PullSource(self.native, 103)
        self.commit("old merge (#7)")
        tip = self.commit(f"native merge (#103)\n\nSource-PR: {new.key}")
        with self.assertRaisesRegex(window.GitError, "distinct source citations"):
            window.window_prs(
                self.root,
                self.initial,
                tip,
                repository=self.native,
                legacy_repository=self.github,
                migration=MigrationMap([(old, new)]),
            )
        with self.assertRaises(ProvenanceError):
            MigrationMap([(old, PullSource(self.github, 8))])
        with self.assertRaises(ProvenanceError):
            MigrationMap([(old, new), (new, PullSource(self.other, 9))])

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
        original = window.git_history.read
        log_reads = []

        def counted(repo, revision):
            log_reads.append(revision)
            return original(repo, revision)

        @window.history_round
        def round_():
            first = window.first_parent_sources(self.root, "main", legacy_repository=self.github)
            first.clear()
            self.assertEqual(len(window.first_parent_sources(self.root, "main", legacy_repository=self.github)), 1)
            self.assertEqual(len(log_reads), 1)
            self.commit("two (#2)")
            self.assertEqual(len(window.first_parent_sources(self.root, "main", legacy_repository=self.github)), 2)
            self.assertEqual(len(log_reads), 2)

        with patch.object(window.git_history, "read", side_effect=counted):
            round_()
            window.first_parent_sources(self.root, "main", legacy_repository=self.github)
        self.assertEqual(len(log_reads), 3)

    def test_round_cache_bounds_and_legacy_namespaces_are_kept_separate(self):
        one = self.commit("one (#1)")
        self.commit("two (#2)")
        original = window.git_history.read
        reads = []

        def counted(repo, revision):
            reads.append(revision)
            return original(repo, revision)

        @window.history_round
        def round_():
            for _ in range(2):
                window.first_parent_sources(self.root, "main", legacy_repository=self.github)
            self.assertEqual(len(reads), 2)  # Three commits exceed the two-row cache bound.
            self.assertEqual(
                window.first_parent_sources(self.root, one, legacy_repository=self.github)[0][1].repository, self.github
            )
            self.assertEqual(
                window.first_parent_sources(self.root, one, legacy_repository=self.other)[0][1].repository, self.other
            )
            cache = window._HISTORY_ROUND.get()
            self.assertLessEqual(len(cache), 1)
            self.assertLessEqual(sum(map(len, cache.values())), 2)

        with (
            patch.object(window, "MAX_HISTORY_ENTRIES", 1),
            patch.object(window, "MAX_HISTORY_ROWS", 2),
            patch.object(window.git_history, "read", side_effect=counted),
        ):
            round_()
        self.assertIsNone(window._HISTORY_ROUND.get())

    def test_streaming_chunk_boundaries_and_large_unicode_line_do_not_retain_bodies(self):
        source = PullSource(self.native, 7)
        message = "huge (#7)\n\n" + "∀" * (3 * 1024 * 1024) + f"\nSource-PR: {source.key}\n"
        # Avoid an argv limit and exclude fixture construction from the reader's memory budget.
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "--allow-empty", "-q", "-F", "-"],
            input=message.encode(),
            env=self.env,
            check=True,
            capture_output=True,
        )
        del message
        tracemalloc.start()
        try:
            self.assertEqual(window.window_prs(self.root, self.initial, "main", repository=self.native), [7])
            _current, peak = tracemalloc.get_traced_memory()
            self.assertLess(peak, 4 * 1024 * 1024)
        finally:
            tracemalloc.stop()
        self.commit(f"boundary (#8)\n\n∀\nSource-PR: {PullSource(self.native, 8).key}\nRepair notes αβγ.")
        # Exercise small chunks on just the last commit, not on the 9 MiB fixture.
        with patch.object(window.git_history, "CHUNK_BYTES", 7):
            self.assertEqual(window.window_prs(self.root, "main~1", "main", repository=self.native), [8])

    def test_commit_overrides_repair_landed_ambiguity_and_can_explicitly_ignore_one_commit(self):
        sha = self.commit("prose without a number\n\nSource-PR: invalid")
        with self.assertRaisesRegex(window.GitError, sha[:12]):
            window.window_prs(self.root, self.initial, sha)
        repaired = MigrationMap.parse(
            {
                "schema": "tauceti-progress.migration/v1",
                "mappings": [],
                "commits": {sha: PullSource(self.github, 7).key},
            }
        )
        self.assertEqual(window.window_prs(self.root, self.initial, sha, migration=repaired), [7])
        ignored = MigrationMap(commits={sha: None})
        self.assertEqual(window.window_prs(self.root, self.initial, sha, migration=ignored), [])
        self.assertEqual(MigrationMap.parse(repaired.record()).record(), repaired.record())
        with self.assertRaises(ProvenanceError):
            MigrationMap(commits={"main": None})

    def test_invalid_utf8_names_the_commit_and_a_trusted_override_can_resolve_it(self):
        tree = window.git(["rev-parse", "HEAD^{tree}"], self.root).strip()
        raw = (
            f"tree {tree}\nparent {self.initial}\nauthor Fixture <fixture@example.invalid> 1 +0000\n"
            "committer Fixture <fixture@example.invalid> 1 +0000\n\nbad (#7)\n\n"
        ).encode() + b"invalid \xff\n"
        sha = (
            subprocess.run(
                ["git", "-C", str(self.root), "hash-object", "-t", "commit", "-w", "--stdin"],
                input=raw,
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .strip()
        )
        with self.assertRaisesRegex(window.GitError, f"{sha[:12]}: commit text is not valid UTF-8"):
            window.window_prs(self.root, self.initial, sha)
        self.assertEqual(
            window.window_prs(
                self.root, self.initial, sha, migration=MigrationMap(commits={sha: PullSource(self.github, 7)})
            ),
            [7],
        )

    def test_shallow_histories_bypass_the_round_cache(self):
        self.commit("one (#1)")
        clone = self.root / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone)], check=True, capture_output=True
        )
        original = window.git_history.read

        @window.history_round
        def round_():
            with patch.object(window.git_history, "read", wraps=original) as read:
                for _ in range(2):
                    window.first_parent_sources(clone, "main", legacy_repository=self.github)
                self.assertEqual(read.call_count, 2)
                self.assertEqual(window._HISTORY_ROUND.get(), {})

        round_()

    def test_configured_map_is_frozen_during_a_round_and_errors_leave_no_context(self):
        import json

        path = self.root / "migration.json"
        sha = self.commit("one (#1)")
        first = MigrationMap(commits={sha: PullSource(self.github, 7)})
        second = MigrationMap(commits={sha: None})
        path.write_text(json.dumps(first.record()))

        @window.history_round
        def round_():
            path.write_text(json.dumps(second.record()))
            self.assertEqual(window.window_prs(self.root, self.initial, sha), [7])
            return window.current_migration().record()

        with patch.dict(os.environ, TAUCETI_PROGRESS_HISTORY_MAP=str(path)):
            frozen = round_()
            self.assertEqual(frozen, first.record())
            with window.using_migration(MigrationMap.parse(frozen)):
                self.assertEqual(window.window_prs(self.root, self.initial, sha), [7])
            self.assertEqual(window.window_prs(self.root, self.initial, sha), [])
            path.write_text("invalid JSON")
            with self.assertRaises(ProvenanceError):
                round_()
        self.assertIsNone(window._HISTORY_ROUND.get())
        self.assertIsNone(window._MIGRATION.get())

    def test_local_replace_refs_cannot_change_canonical_commit_attribution(self):
        sha = self.commit("one (#1)")
        replacement = self.commit("different (#99)")
        subprocess.run(["git", "-C", str(self.root), "replace", sha, replacement], check=True, capture_output=True)
        self.assertEqual(window.window_prs(self.root, self.initial, sha), [1])

    def test_real_plan_bootstrap_reuses_full_history_and_facts_restore_its_migration(self):
        first = self.commit("ambiguous body\n\nSource-PR: not-a-citation")
        tip = self.commit("second (#8)")
        roadmap = self.root / "roadmap"
        for area in ("A", "B"):
            path = roadmap / "TauCetiRoadmap" / area
            path.mkdir(parents=True)
            (path / "README.md").write_text(f"# {area}\n")
        mapping = MigrationMap(commits={first: PullSource(self.github, 7)})
        map_file = self.root / "history-map.json"
        map_file.write_text(json.dumps(mapping.record()))
        docs = SimpleNamespace(
            source_commit=lambda: tip,
            generation="fixture",
            require_coverage=lambda _: None,
            pin=lambda: {"source_commit": tip},
        )
        original = window.git_history.read
        reads = []

        def read(repo, revision):
            reads.append(revision)
            return original(repo, revision)

        with (
            patch.dict(os.environ, TAUCETI_PROGRESS_HISTORY_MAP=str(map_file)),
            patch.object(gh, "closed_prs_for_areas", return_value={"A": {7: None}, "B": {8: None}}),
            patch.object(plan, "_own_login", return_value="fixture"),
            patch.object(window.git_history, "read", side_effect=read),
        ):
            result = plan.build_plan(roadmap, self.root, commits=[], open_prs=[], ref="main", min_prs=1, docs=docs)
        self.assertEqual(result["prs"], [7])
        self.assertEqual(result["history_migration"], mapping.record())
        self.assertEqual(reads.count(tip), 1)
        self.assertIsNone(window._HISTORY_ROUND.get())
        saved = self.root / "plan.json"
        saved.write_text(json.dumps(result))
        map_file.write_text("invalid current config")

        def collect(*args, **kwargs):
            self.assertEqual(window.current_migration().record(), mapping.record())
            return {"declarations": [], "files": []}

        with (
            patch.dict(os.environ, TAUCETI_PROGRESS_HISTORY_MAP=str(map_file)),
            patch.object(facts, "collect", side_effect=collect),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.main(["facts", "--plan", str(saved), "--code-dir", str(self.root)]), 0)
        self.assertIsNone(window._MIGRATION.get())

    def test_empty_query_and_fragment_citations_are_rejected(self):
        for suffix in ("?", "#"):
            with self.assertRaises(ProvenanceError):
                PullSource.parse(f"forgejo:https://forge.example/team/Math{suffix}#7")

    def test_streaming_trailer_separator_whitespace_matches_queue_grammar(self):
        source = PullSource(self.github, 7)
        sha = self.commit("long separator (#7)\n\nSource-PR:" + " " * 20000 + source.key)
        self.assertEqual(window.window_prs(self.root, self.initial, sha), [7])
        sha = self.commit("ignored prose (#8)\n\nSource-PR: \u2003" + "x" * 20000)
        self.assertEqual(window.window_prs(self.root, "main~1", sha), [8])


if __name__ == "__main__":
    unittest.main()
