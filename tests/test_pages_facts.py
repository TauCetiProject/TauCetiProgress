"""The real TauCeti #19012 bundle, local pinning, and ordinary mechanical recovery."""
import copy
import gzip
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, cli, facts, gh, plan, window
from progress.docs import Docs, DocsGenerationChanged
from progress.immutable_docs import (ArtifactError, ArtifactNotFound, ConfigurationError,
                                     FactsDocs, PagesDocs, discover)

FIXTURE = pathlib.Path(__file__).parent / "fixtures/pages-facts"
BASE = "https://taucetiproject.org/docs"


def pack(data):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as stream:
        stream.write(json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return buffer.getvalue()


class PagesFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.data = json.loads((FIXTURE / "facts.json").read_bytes())
        self.raw = pack(self.data)
        self.requests = []
        self.error = None
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "auto", "TAUCETI_FACTS_CACHE": str(self.cache)}).start()
        patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub API call")).start()
        patch.object(FactsDocs, "_fetch", side_effect=self.fetch).start()
        patch.object(facts, "LEAN_PREFIX", "Demo/").start()

    def fetch(self, url):
        self.requests.append(url)
        self.assertEqual(url, BASE + "/facts.json.gz")
        if self.error:
            raise self.error
        return self.raw

    def reader(self):
        return discover(base=BASE, cache_dir=self.cache)

    def restore(self, pin):
        return PagesDocs(pin=pin, cache_dir=self.cache)

    def test_real_producer_fixture_is_one_response_and_preserves_generated_facts(self):
        reader = self.reader()
        self.assertEqual(self.requests, [BASE + "/facts.json.gz"])
        self.assertEqual(reader.pin()["facts_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual((reader.cache_dir / "facts.json.gz").read_bytes(), self.raw)
        self.assertEqual(reader.declarations("Demo/Empty.html"), {})
        decls = reader.declarations("Demo/A.html")
        self.assertEqual(len(decls), 14)
        self.assertEqual(decls["Demo.instReprPoint.repr"]["kind"], "def")
        self.assertEqual(decls["Demo.instReprPoint"]["start"], 23)
        self.assertEqual(decls["Demo.alpha"]["doc"],
                         "**Alpha's theorem.** It states `True`.\n\nA second paragraph.")
        self.assertEqual(decls["Demo.alpha"]["url"], BASE + "/Demo/A.html#Demo.alpha")
        # No manifest, pointer, HTML, SOURCE_SHA, or provider request is needed.
        self.assertEqual(len(self.requests), 1)

    def test_discovery_uses_the_configured_docs_base_and_ignores_obsolete_r2_setting(self):
        with patch("progress.immutable_docs.Docs", return_value=Docs(base=BASE)), \
             patch.dict(os.environ, {"TAUCETI_FACTS_BASE": "https://obsolete.example/r2"}):
            self.assertIsInstance(discover(cache_dir=self.cache), PagesDocs)
        self.assertEqual(self.requests, [BASE + "/facts.json.gz"])

    def test_later_deploy_does_not_replace_cached_plan_even_in_legacy_mode(self):
        old = self.reader()
        self.data["source_commit"] = "b" * 40
        self.raw = pack(self.data)
        self.assertEqual(self.reader().source_commit(), "b" * 40)
        self.error = ArtifactError("offline")
        self.requests.clear()
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "legacy"}):
            restored = self.restore(old.pin())
        self.assertEqual(restored.pin(), old.pin())
        self.assertEqual(restored.declarations("Demo/A.html"), old.declarations("Demo/A.html"))
        self.assertEqual(self.requests, [])

    def test_lost_or_corrupt_copy_can_be_refetched_only_if_hash_agrees(self):
        old = self.reader()
        path = old.cache_dir / "facts.json.gz"
        path.write_bytes(b"corrupt")
        self.assertEqual(self.restore(old.pin()).pin(), old.pin())
        path.unlink()
        self.data["provenance"]["doc_gen"] += "-changed"
        self.raw = pack(self.data)  # even a new bundle at the same source must not replace the pin
        with self.assertRaises(DocsGenerationChanged) as caught:
            self.restore(old.pin())
        self.assertIn(old.pin()["facts_sha256"], str(caught.exception))
        self.assertIn(hashlib.sha256(self.raw).hexdigest(), str(caught.exception))
        self.assertFalse(path.exists())

    def test_cache_is_separated_by_origin(self):
        pin = self.reader().pin()
        pin["docs_base"] = "https://other.example/docs"
        with patch.object(FactsDocs, "_fetch", side_effect=ArtifactError("other origin offline")):
            with self.assertRaisesRegex(ArtifactError, "other origin offline"):
                self.restore(pin)

    def test_only_discovery_404_and_unsupported_schema_allow_legacy(self):
        self.error = ArtifactNotFound("HTTP 404")
        self.assertIsInstance(self.reader(), Docs)
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "immutable"}):
            with self.assertRaises(ArtifactNotFound):
                self.reader()
        self.error = None
        self.data["schema_version"] = 2
        self.raw = pack(self.data)
        self.assertIsInstance(self.reader(), Docs)
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "immutable"}):
            with self.assertRaises(ArtifactError):
                self.reader()
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "typo"}):
            with self.assertRaises(ConfigurationError):
                self.reader()

    def test_transport_failures_and_selected_pin_404_never_switch_to_html(self):
        pin = self.reader().pin()
        (self.restore(pin).cache_dir / "facts.json.gz").unlink()
        for error in (ArtifactError("HTTP 503"), ArtifactError("timeout"), ArtifactNotFound("HTTP 404")):
            self.error = error
            with self.assertRaises(ArtifactError):
                self.restore(pin)
        self.error = ArtifactError("HTTP 503")
        with self.assertRaisesRegex(ArtifactError, "503"):
            self.reader()

    def test_truncated_corrupt_or_invalid_bundle_is_not_legacy_or_empty(self):
        good = self.raw
        for raw in (good[:-8], good[:-1] + bytes([good[-1] ^ 1]), b"<html>not gzip</html>"):
            self.raw = raw
            with self.assertRaisesRegex(ArtifactError, "gzip"):
                self.reader()
        for edit in ({"schema_version": True}, {"source_commit": "HEAD"},
                     {"provenance": {}}, {"modules": {}}):
            self.raw = pack({**self.data, **edit})
            with self.assertRaises(ArtifactError):
                self.reader()
        duplicate = json.dumps(self.data).replace('"schema_version": 1',
                                                 '"schema_version": 1, "schema_version": 1')
        self.raw = gzip.compress(duplicate.encode())
        with self.assertRaisesRegex(ArtifactError, "duplicate JSON key"):
            self.reader()

    def test_discovery_requires_a_durable_local_copy(self):
        with patch.object(FactsDocs, "_store", return_value=None):
            with self.assertRaisesRegex(ArtifactError, "cannot retain pinned facts"):
                self.reader()

    def test_saved_pin_coverage_is_verified(self):
        pin = self.reader().pin()
        pin["coverage"] = []
        with self.assertRaisesRegex(ArtifactError, "coverage changed"):
            self.restore(pin)

    def init_repo(self):
        self.code = self.root / "code"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.code)], check=True)
        for key, value in (("user.name", "T"), ("user.email", "t@e")):
            window.git(["config", key, value], self.code)
        self.first = self.commit("init", {"README.md": "test\n"})
        self.head = self.commit("feat: fixture (#101)", json.loads((FIXTURE / "sources.json").read_bytes()))
        self.data["source_commit"] = self.head
        self.raw = pack(self.data)
        self.roadmap = self.root / "roadmap"
        area = self.roadmap / "TauCetiRoadmap" / "Algebra"
        area.mkdir(parents=True)
        (area / "README.md").write_text("# Algebra\n")

    def commit(self, subject, changes):
        for path, text in changes.items():
            target = self.code / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        window.git(["add", "-A"], self.code)
        window.git(["commit", "-qm", subject], self.code)
        return window.head_sha(self.code, "HEAD")

    def build_plan(self, labels):
        with patch.object(plan, "_own_login", return_value="tester"), \
             patch.object(gh, "closed_prs_for_area", return_value=labels):
            return plan.build_plan(self.roadmap, self.code, ref="main", min_prs=1,
                                   commits=[], open_prs=[], docs=self.reader())

    def test_missing_required_empty_module_refuses_plan_and_facts_without_output(self):
        self.init_repo()
        del self.data["modules"]["Demo/Empty.lean"]
        self.raw = pack(self.data)
        with self.assertRaisesRegex(ArtifactError, "missing required modules"):
            self.build_plan({101: self.head})
        p = {"from_sha": self.first, "to_sha": self.head, "prs": [101],
             "documentation": self.reader().pin()}
        plan_file, output = self.root / "plan.json", self.root / "facts.json"
        plan_file.write_text(json.dumps(p))
        self.assertEqual(cli.main(["facts", "--plan", str(plan_file), "--code-dir", str(self.code),
                                   "--out", str(output)]), 77)
        self.assertFalse(output.exists())
        self.assertEqual(json.loads(plan_file.read_text()), p)

    def test_missing_copy_after_deploy_replans_the_same_unadvanced_obligation_and_succeeds(self):
        self.init_repo()
        p = self.build_plan({101: self.head})
        old_head = self.head
        plan_file, output = self.root / "plan.json", self.root / "facts.json"
        plan_file.write_text(json.dumps(p))
        self.head = self.commit("feat: later (#102)", {"Demo/Later.lean": "theorem later : True := trivial\n"})
        self.data["source_commit"] = self.head
        self.data["modules"]["Demo/Later.lean"] = {"Demo.later": {
            "kind": "theorem", "file": "Demo/Later.lean", "start": 1, "end": 1, "doc": "",
            "url": BASE + "/Demo/Later.html#Demo.later"}}
        self.raw = pack(self.data)
        (self.restore(p["documentation"]).cache_dir / "facts.json.gz").unlink()
        argv = ["facts", "--plan", str(plan_file), "--code-dir", str(self.code), "--out", str(output)]
        self.assertEqual(cli.main(argv), cli.EX_DOCS_GENERATION_CHANGED)
        self.assertFalse(output.exists())
        self.assertEqual(json.loads(plan_file.read_text()), p)
        # The #25 mechanical retry requests a fresh plan before any writer can run.
        replanned = self.build_plan({101: old_head, 102: self.head})
        self.assertEqual(replanned["from_sha"], p["from_sha"])
        self.assertEqual(set(replanned["prs"]), {101, 102})
        plan_file.write_text(json.dumps(replanned))
        self.requests.clear()
        self.error = ArtifactError("origin offline after successful plan")
        self.assertEqual(cli.main(argv), 0)
        result = json.loads(output.read_text())
        self.assertEqual({d["pr"] for d in result["declarations"]}, {101, 102})
        self.assertEqual(len(result["declarations"]), 17)
        self.assertEqual(self.requests, [])

    def test_prepared_pages_report_resumes_and_publishes_without_live_facts(self):
        self.init_repo()
        p = self.build_plan({101: self.head})
        spine = facts.collect(self.code, p["from_sha"], p["to_sha"], pr_numbers=p["prs"],
                              documentation=p["documentation"])
        self.assertEqual(len(spine["declarations"]), 16)
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "T"],
                     ["config", "user.email", "t@e"], ["add", "."], ["commit", "-qm", "base"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"]):
            window.git(args, self.roadmap)
        status, section = self.root / "status", self.root / "section"
        prose = "The algebra roadmap now has Alpha's theorem and the supporting compiler-generated instances. " * 4
        status.write_text(prose)
        section.write_text(prose)
        with patch.object(apply, "existing_pr", return_value=None), \
             patch.object(apply, "own_pr", return_value=None):
            self.assertEqual(apply.run(p, status, section, self.roadmap, dry_run=True), 0)
            prepared = window.head_sha(self.roadmap, "HEAD")
            self.error = ArtifactError("live site unavailable")
            self.requests.clear()
            with patch.object(apply, "render_update", side_effect=AssertionError("must reuse prose")), \
                 patch.object(apply, "push_target", return_value=("fork", "tester")), \
                 patch.object(apply, "remote_branch_exists", return_value=True), \
                 patch.object(apply, "sweep", return_value=0), \
                 patch.object(gh, "gh", return_value="https://github.com/org/roadmap/pull/1") as publish:
                self.assertEqual(apply.run(p, status, section, self.roadmap), 0)
                publish.assert_called_once()
            self.assertEqual(window.head_sha(self.roadmap, "HEAD"), prepared)
            self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
