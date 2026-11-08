"""Unsafe command shapes must enter publication recovery rather than read retries."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, forks, gh  # noqa: E402


class RetryClassification(unittest.TestCase):
    def test_mutation_after_query_prefix_is_journaled_and_not_repeated(self):
        args = ["api", "graphql", "-f", 'query=query Q { viewer { login } } mutation M { closeIssue(input:{issueId:"I"}) { clientMutationId } }', "-f", "operationName=M"]
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": root, "TAUCETI_REAL_GH": ""}):
            writes = []

            def native(argv, **kwargs):
                if argv[1:3] == ["api", "user"]:
                    return subprocess.CompletedProcess(argv, 0, "author\n", "")
                writes.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "response lost after acceptance")

            with patch.object(gh.subprocess, "run", side_effect=native), patch.object(gh.time, "sleep") as sleep:
                for _ in range(2):
                    with self.assertRaises(gh.GhError):
                        gh.gh(args)
                self.assertEqual(writes, [["gh", *args]])
                sleep.assert_not_called()
                intent = json.loads(next(pathlib.Path(root).glob("*.json")).read_text())
                self.assertEqual(intent["argv"], args)
                self.assertEqual(intent["attempts"], 1)

    def test_post_and_unknown_forms_never_become_retryable_reads(self):
        for args in [
            ["api", "repos/one/Project/issues", "-if", "title=Created"],
            ["api", "repos/one/Project/issues", "-f", "query=query{viewer{login}}", "--jq", "graphql"],
            ["api", "graphql", "-f", "query=query Q { viewer{login} } mutation M { x }", "-f", "operationName=M"],
            ["api", "graphql", "-f", "query=query { viewer{login} }", "--input", "request.json"],
            ["api", "graphql", "-F", "query=@request.graphql"],
            ["api", "graphql", "--field=query=@request.graphql"],
            ["api", "graphql", "-Fquery=@request.graphql"],
            ["api", "graphql", "-F", "query={repo} X { x }"],
            ["api", "graphql", "-f", "query=query{viewer{login}}", "-f", "query=mutation{x}"],
            ["api", "graphql", "-f", "query=query{viewer{login}}", "-f", "query[operation]=mutation{x}"],
            ["api", "graphql", "-f", "query=query{viewer{login}}", "-f", "extensions=persisted"],
            ["api", "graphql", "--jq", "query=query{viewer{login}}"],
            ["api", "graphql", "-f", "query=subscription { updates }"],
            ["api", "https://api.github.com/graphql", "-f", "query=query{viewer{login}}"],
            ["api", "user", "--future-option"], ["api", "user", "--jq"],
            ["api", "graphql", "-f"],
            ["pr", "create", "--title", "Progress"], ["repo", "fork", "one/Project"],
        ]:
            with self.subTest(args=args):
                self.assertFalse(gh.readonly(args))

    def test_known_reads_preserve_bounded_retries_and_payload_arity(self):
        for args in [
            ["api", "user", "--jq", ".login"],
            ["api", "repos/one/Project/pulls/12"],
            ["api", "repos/one/graphql", "--jq", ".permissions.push"],
            ["api", "repos/one/Project/contents/graphql"],
            ["api", "repos/one/Project/commits?per_page=30", "--jq", ".[].sha"],
            ["api", "graphql", "--paginate", "--slurp", "-f", "query=query($name:String!){repository(name:$name){id}}", "-f", "name=Project"],
            ["api", "graphql", "-fquery={ viewer{login} }", "--jq", "-XPOST"],
            ["api", "graphql", "-f=query={ viewer{login} }"],
            ["api", "graphql", "--raw-field=query=query{viewer{login}}", "--jq=--input"],
            ["pr", "list", "--repo", gh.CODE_REPO, "--state", "open"],
        ]:
            with self.subTest(args=args), patch.object(gh.subprocess, "run") as native, patch.object(gh.time, "sleep") as sleep:
                self.assertTrue(gh.readonly(args))
                native.side_effect = [subprocess.CompletedProcess([], 1, "", "temporary outage"), subprocess.CompletedProcess([], 0, "{}", "")]
                self.assertEqual(gh.gh(args), "{}")
                self.assertEqual(native.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_actual_read_builders_remain_retryable(self):
        class Captured(RuntimeError):
            pass

        builders = [
            ("area history", lambda: gh.closed_prs_for_area("Area")),
            ("batched histories", lambda: gh.closed_prs_for_areas(["Area", "Other"])),
            ("PR recovery", lambda: apply.reconcile_pr("progress/window/Area", "author")),
            ("fork discovery", lambda: forks.discover(gh.gh, "one/Project")),
            ("roadmap commits", gh.recent_roadmap_commits),
            ("PR details", lambda: gh.pr_details([12])),
            ("open progress PRs", gh.open_progress_prs),
            ("push permission on repo named graphql", lambda: apply.push_target(pathlib.Path("/unused"), repo="one/graphql")),
        ]
        for name, build in builders:
            with self.subTest(builder=name), patch.object(gh, "gh", side_effect=Captured) as transport:
                with self.assertRaises(Captured):
                    build()
                transport.assert_called_once()
                self.assertTrue(gh.readonly(transport.call_args.args[0]), transport.call_args.args[0])

    def test_known_graphql_partial_errors_still_raise(self):
        with patch.object(gh.subprocess, "run") as native:
            native.return_value = subprocess.CompletedProcess([], 0, '{"data":{},"errors":[{"message":"partial"}]}', "")
            with self.assertRaisesRegex(gh.GhError, "partial GraphQL read response"):
                gh.gh(["api", "graphql", "-f", "query=query{viewer{login}}"])
            native.assert_called_once()


if __name__ == "__main__":
    unittest.main()
