"""Direct native planning and factual attribution; no gh or shared-state fallback."""

import base64
import gzip
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import GITHUB, Forge, ForgeError, Forgejo, Incomplete, RepositoryRef  # noqa: E402

from progress import facts, gh, plan, window  # noqa: E402
from progress.docs import Docs, ReplanRequired  # noqa: E402
from progress.native_source import NativeRepository, NativeSource, history_options_from_record  # noqa: E402


class NativePlanning(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-native-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.requests, self.failures = [], {}
        self.closed, self.open = [], []
        self.repository_ids = {"team/Code": 11, "team/Roadmap": 12}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                path = urlsplit(self.path)
                name = path.path.removeprefix("/api/v1/")
                query = parse_qs(path.query)
                status, body = 200, None
                if name in outer.failures:
                    status, body = outer.failures[name], {"message": "fixture refusal"}
                elif name == "user":
                    body = {"id": 21, "login": "operator"}
                elif name.startswith("repos/"):
                    rest = name.removeprefix("repos/")
                    owner, repo, *tail = rest.split("/")
                    repository = owner + "/" + repo
                    endpoint = "/".join(tail)
                    if not endpoint:
                        body = {"full_name": repository, "id": outer.repository_ids[repository]}
                    elif endpoint == "pulls":
                        rows = outer.closed if query.get("state") == ["closed"] else outer.open
                        page = int(query.get("page", ["1"])[0])
                        body = rows[(page - 1) * 2 : page * 2]  # server cap below requested limit
                    elif endpoint.startswith("pulls/"):
                        number = int(endpoint.split("/")[1])
                        body = next((row for row in outer.closed + outer.open if row["number"] == number), None)
                    elif endpoint == "commits":
                        body = (
                            [{"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}, "message": "initial"}}]
                            if query.get("page") == ["1"]
                            else []
                        )
                    elif endpoint.startswith("contents/"):
                        body = {
                            "type": "file",
                            "encoding": "base64",
                            "content": base64.b64encode(b"saved cursor").decode(),
                        }
                if body is None:
                    status, body = 404, {"message": "not found"}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.forge = Forge("forgejo", f"http://127.0.0.1:{server.server_port}")
        self.api = Forgejo(self.forge, token="fixture-native-token")
        self.content = NativeRepository(RepositoryRef(self.forge, "team/Code"), self.api)
        self.roadmap = NativeRepository(RepositoryRef(self.forge, "team/Roadmap"), self.api)
        self.source = NativeSource(self.content, self.roadmap)
        self.code = self.root / "code"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.code)], check=True, capture_output=True)
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        self.initial = self.commit("Initial")
        self.requests.clear()

    def commit(self, message, file=None):
        if file:
            path = self.code / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("theorem alpha : True := trivial\n")
            subprocess.run(["git", "-C", str(self.code), "add", file], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.code), "commit", "--allow-empty", "-qm", message],
            check=True,
            capture_output=True,
            env=self.git_env,
        )
        return window.git(["rev-parse", "HEAD"], self.code).strip()

    def row(self, number, area="Area", merged=False, sha=None):
        return {
            "number": number,
            "state": "closed",
            "merged": merged,
            "merge_commit_sha": sha,
            "labels": [{"name": "roadmap/" + area}],
        }

    def docs(self, sha):
        base = "https://docs.example/docs"
        data = {
            "schema_version": 1,
            "source_commit": sha,
            "provenance": {"toolchain": "leanprover/lean4:v4.0.0", "doc_gen": "fixture"},
            "modules": {
                "TauCeti/A.lean": {
                    "alpha": {
                        "kind": "theorem",
                        "file": "TauCeti/A.lean",
                        "start": 1,
                        "end": 1,
                        "doc": "Alpha.",
                        "url": base + "/TauCeti/A.html#alpha",
                    }
                }
            },
        }
        return Docs(base=base, raw=gzip.compress(json.dumps(data).encode()), cache_dir=self.root / "facts-cache")

    def test_one_complete_scan_combines_closed_unmerged_and_native_merges_for_all_areas(self):
        self.closed = [self.row(5, "Other"), self.row(4, merged=True, sha="a" * 40), self.row(3), self.row(2, "Other")]
        self.assertEqual(
            self.content.closed_histories(["Area", "Other"]),
            {"Area": {4: "a" * 40, 3: None}, "Other": {5: None, 2: None}},
        )
        paths = [path for path, _ in self.requests]
        self.assertEqual(len(paths), 3)  # two data pages and the empty page, independent of area count
        self.assertTrue(all("state=closed" in path for path in paths))
        self.assertTrue(all(token == "token fixture-native-token" for _, token in self.requests))

    def test_real_git_plan_and_facts_keep_native_identity_when_github_has_same_number(self):
        sha = self.commit("Queue accepted\n\nSource-PR: " + self.content.repository.key + "#7", "TauCeti/A.lean")
        self.closed = [self.row(7)]
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        docs = self.docs(sha)
        with patch.object(gh, "gh", side_effect=AssertionError("native work must not fall back to gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
            collected = facts.collect(
                self.code, result["from_sha"], sha, pr_numbers=result["prs"], docs=docs, source_record=result["source"]
            )
        self.assertEqual(result["prs"], [7])
        self.assertEqual(result["from_sha"], self.initial)
        self.assertEqual(result["source"]["content"]["forge"]["url"], self.forge.url)
        self.assertEqual(result["source"]["content"]["id"], 11)
        self.assertEqual(collected["source"], result["source"])
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 7)])
        wrong = {
            **result["source"],
            "content": {**result["source"]["content"], "forge": {"kind": "forgejo", "url": "https://other.example"}},
        }
        with self.assertRaisesRegex(window.GitError, "explicit migration"):
            facts.collect(self.code, result["from_sha"], sha, pr_numbers=[7], docs=docs, source_record=wrong)

    def test_native_merge_credits_the_commit_that_landed_lines_on_main(self):
        subprocess.run(["git", "-C", str(self.code), "checkout", "-qb", "proof"], check=True, capture_output=True)
        self.commit("Feature commit", "TauCeti/A.lean")
        subprocess.run(["git", "-C", str(self.code), "checkout", "-q", "main"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.code),
                "merge",
                "--no-ff",
                "-qm",
                "Native merge (#7)\n\nSource-PR: " + self.content.repository.key + "#7",
                "proof",
            ],
            env=self.git_env,
            check=True,
            capture_output=True,
        )
        sha = window.git(["rev-parse", "HEAD"], self.code).strip()
        self.closed = [self.row(7, merged=True, sha=sha)]
        roadmap = self.root / "roadmap"
        (roadmap / "TauCetiRoadmap/Area").mkdir(parents=True)
        (roadmap / "TauCetiRoadmap/Area/README.md").write_text("# Area\n")
        docs = self.docs(sha)
        with patch.object(gh, "gh", side_effect=AssertionError("native work reached gh")):
            result = plan.build_plan(roadmap, self.code, source=self.source, ref="main", min_prs=1, docs=docs)
            collected = facts.collect(
                self.code, result["from_sha"], sha, pr_numbers=[7], docs=docs, source_record=result["source"]
            )
        self.assertEqual([(d["name"], d["pr"]) for d in collected["declarations"]], [("alpha", 7)])

    def test_open_report_ownership_projects_native_head_and_owner_identities(self):
        self.open = [
            {
                "number": 3,
                "state": "open",
                "merged": False,
                "title": "Report",
                "body": "prose",
                "created_at": "2020-01-01T00:00:00Z",
                "head": {
                    "label": "progress/aaaaaaa-bbbbbbb/Area",
                    "repo": {
                        "id": 99,
                        "name": "Roadmap",
                        "full_name": "operator/Roadmap",
                        "owner": {"id": 99, "login": "operator"},
                    },
                },
            }
        ]
        rows = self.roadmap.open_reports()
        owns = self.roadmap.report_owner_filter()
        self.assertFalse(owns(rows[0]))  # same login, another authenticated server-local user ID
        self.open[0]["head"]["repo"]["owner"]["id"] = 21
        self.assertTrue(owns(self.roadmap.open_reports()[0]))
        blocked, _ = plan.in_flight_areas(rows, owner_filter=owns)
        self.assertEqual(blocked, {})

    def test_pr_commentary_url_remains_on_source_origin_and_missing_body_refuses(self):
        self.closed = [{**self.row(7), "title": "Proof", "body": None}]
        details = self.content.pr_details([7])
        self.assertEqual(details[0]["url"], self.content.repository.pull_url(7))
        self.assertEqual(details[0]["body"], "")
        self.assertEqual(details[0]["repository"]["id"], 11)
        del self.closed[0]["body"]
        with self.assertRaises(Incomplete):
            self.content.pr_details([7])

    def test_unavailable_or_partial_history_never_becomes_empty(self):
        self.failures["repos/team/Code/pulls"] = 403
        with self.assertRaises(ForgeError):
            self.content.closed_histories(["Area"])
        self.failures.clear()
        self.closed = [self.row(7, merged=True)]
        with self.assertRaises(Incomplete):
            self.content.closed_histories(["Area"])
        self.closed = [{"number": 7, "state": "closed", "merged": False}]
        with self.assertRaises(Incomplete):
            self.content.closed_histories(["Area"])

    def test_duplicate_changed_pages_and_ambiguous_area_labels_refuse(self):
        self.closed = [self.row(7), self.row(6), self.row(7, merged=True, sha="a" * 40)]
        with self.assertRaisesRegex(Incomplete, "changed during pagination"):
            self.content.closed_histories(["Area"])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.content.closed_histories(["Area", "area"])

    def test_repository_replacement_and_client_origin_change_refuse(self):
        self.repository_ids["team/Code"] = 99
        with self.assertRaisesRegex(Incomplete, "identity changed"):
            self.content.revalidate()
        before = len(self.requests)
        self.api.forge = Forge("forgejo", "https://other.example")
        with self.assertRaisesRegex(Incomplete, "origin changed"):
            self.content.closed_histories(["Area"])
        self.assertEqual(len(self.requests), before)

    def test_empty_content_and_missing_repository_are_distinct(self):
        self.assertEqual(self.content.file_at("Area/PROGRESS.md"), "saved cursor")
        self.failures["repos/team/Code/contents/Area/PROGRESS.md"] = 404
        self.assertIsNone(self.content.file_at("Area/PROGRESS.md"))
        self.failures["repos/team/Code"] = 403
        with self.assertRaises(ForgeError):
            self.content.file_at("Area/PROGRESS.md")

    def test_saved_source_schema_and_legacy_namespace_are_explicit(self):
        record = self.source.record()
        self.assertEqual(history_options_from_record(record)["repository"], self.content.repository)
        old = RepositoryRef(GITHUB, "team/Code")
        source = NativeSource(self.content, self.roadmap, legacy_content=old)
        self.assertEqual(history_options_from_record(source.record())["legacy_repository"], old)
        bad = {**record, "content": {**record["content"], "id": True}}
        with self.assertRaises(ValueError):
            history_options_from_record(bad)
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, self.initial, self.initial, source_record=bad)

    def test_native_in_flight_ownership_uses_ids_not_reused_logins(self):
        actor = {"login": "operator", "id": self.forge.key + "/users/21"}
        head = {"forge": {"kind": "forgejo", "url": self.forge.url}, "name": "operator/Roadmap", "id": 88}
        owns = self.roadmap.report_owner_filter()
        self.assertTrue(owns({"source_head_repository": head, "headRepositoryOwner": actor}))
        self.assertFalse(
            owns({"source_head_repository": head, "headRepositoryOwner": {**actor, "id": self.forge.key + "/users/99"}})
        )
        self.assertTrue(owns({"source_head_repository": self.roadmap.identity.record(), "headRepositoryOwner": {}}))


if __name__ == "__main__":
    unittest.main()
