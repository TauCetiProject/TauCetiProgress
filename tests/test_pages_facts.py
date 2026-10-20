"""The real TauCeti #19012 bundle, local pinning, and ordinary mechanical recovery."""
import copy
import gzip
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, cli, docs, facts, gh, plan, window
from progress.docs import (ArtifactError, ArtifactNotFound, ConfigurationError, Docs, DocsGenerationChanged,
                           FactsReplanRequired, ReplanRequired, UnsupportedFactsSchema, discover)

FIXTURE = pathlib.Path(__file__).parent / "fixtures/pages-facts"
BASE = "https://taucetiproject.org/docs"


def gzip_bytes(raw):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as stream:
        stream.write(raw)
    return buffer.getvalue()


def pack(data):
    return gzip_bytes(json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n")


class PagesFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.data = json.loads((FIXTURE / "facts.json").read_bytes())
        self.raw = gzip_bytes((FIXTURE / "facts.json").read_bytes())
        self.requests = []
        self.error = None
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TAUCETI_FACTS_CACHE": str(self.cache)}).start()
        patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub API call")).start()
        patch.object(Docs, "_fetch", side_effect=self.fetch).start()
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
        return Docs(pin=pin, cache_dir=self.cache)

    def test_real_producer_fixture_is_one_response_and_preserves_generated_facts(self):
        reader = self.reader()
        self.assertEqual(gzip.decompress(self.raw), (FIXTURE / "facts.json").read_bytes())
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

    def test_discovery_uses_the_configured_docs_base(self):
        with patch.object(docs, "DOCS_BASE", BASE):
            self.assertEqual(discover(cache_dir=self.cache).base, BASE)
        self.assertEqual(self.requests, [BASE + "/facts.json.gz"])
        with self.assertRaises(ConfigurationError):
            discover(base="ftp://docs.example", cache_dir=self.cache)

    def test_later_deploy_does_not_replace_cached_plan(self):
        old = self.reader()
        self.data["source_commit"] = "b" * 40
        self.raw = pack(self.data)
        self.assertEqual(self.reader().source_commit(), "b" * 40)
        self.error = ArtifactError("offline")
        self.requests.clear()
        restored = self.restore(old.pin())
        self.assertEqual(restored.pin(), old.pin())
        self.assertEqual(restored.declarations("Demo/A.html"), old.declarations("Demo/A.html"))
        self.assertEqual(self.requests, [])

    def test_lost_or_corrupt_copy_can_be_refetched_only_if_hash_agrees(self):
        old = self.reader()
        old.pin()
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
        with patch.object(Docs, "_fetch", side_effect=ArtifactError("other origin offline")):
            with self.assertRaisesRegex(ArtifactError, "other origin offline"):
                self.restore(pin)

    def test_a_missing_or_newer_bundle_has_no_facts_to_offer(self):
        self.error = ArtifactNotFound("HTTP 404")
        with self.assertRaises(ArtifactNotFound):
            self.reader()
        self.error = None
        self.data["schema_version"] = 2
        self.raw = pack(self.data)
        with self.assertRaises(UnsupportedFactsSchema):
            self.reader()

    def test_declaration_schema_and_duplicate_names(self):
        alpha = self.data["modules"]["Demo/A.lean"]["Demo.alpha"]
        for edit in ({"file": "Demo/Other.lean"}, {"url": "http://["}, {"url": BASE + "/Demo/A.html#wrong"},
                     {"url": BASE + "/Other.html#Demo.alpha"}, {"kind": ""}, {"doc": None},
                     {"start": 3, "end": 2}, {"start": None}):
            with self.subTest(edit=edit):
                data = copy.deepcopy(self.data)
                data["modules"]["Demo/A.lean"]["Demo.alpha"].update(edit)
                self.raw = pack(data)
                with self.assertRaises(ArtifactError):
                    self.reader()
        data = copy.deepcopy(self.data)
        data["modules"]["Demo/Empty.lean"]["Demo.alpha"] = alpha
        self.raw = pack(data)
        with self.assertRaisesRegex(ArtifactError, "duplicate declaration"):
            self.reader()
        # doc-gen emits literal name fragments, including percent characters in quoted names.
        data = copy.deepcopy(self.data)
        name = "Demo.«literal%FF»"
        data["modules"]["Demo/A.lean"][name] = {**alpha, "url": BASE + "/Demo/A.html#" + name}
        self.raw = pack(data)
        self.assertIn(name, self.reader().declarations("Demo/A.html"))

    def test_transport_failures_wait_and_a_lost_pin_replans(self):
        pin = self.reader().pin()
        (self.restore(pin).cache_dir / "facts.json.gz").unlink()
        for error in (ArtifactError("HTTP 503"), ArtifactError("timeout"), ArtifactNotFound("HTTP 404")):
            self.error = error
            with self.assertRaises(FactsReplanRequired if isinstance(error, ArtifactNotFound) else ArtifactError):
                self.restore(pin)
        self.error = ArtifactError("HTTP 503")
        with self.assertRaisesRegex(ArtifactError, "503"):
            self.reader()

    def test_truncated_corrupt_or_invalid_bundle_is_never_empty(self):
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

    def test_emitted_pin_requires_a_durable_local_copy(self):
        with patch.object(docs, "store", return_value=False):
            with self.assertRaisesRegex(ArtifactError, "cannot retain pinned facts"):
                self.reader().pin()

    def test_saved_pin_coverage_is_verified(self):
        pin = self.reader().pin()
        pin["coverage"] = []
        with self.assertRaisesRegex(ArtifactError, "coverage changed"):
            self.restore(pin)

    def test_default_cache_uses_persistent_state(self):
        state = self.root / "state"
        with patch.dict(os.environ, {"TAUCETI_FACTS_CACHE": "", "XDG_STATE_HOME": str(state)}):
            reader = discover(base=BASE)
            reader.pin()
        self.assertTrue(reader.cache_dir.is_relative_to(state / "tauceti-progress/facts"))
        pin = self.reader().pin()
        pin["source_commit"] = "b" * 40
        # Equal bytes/hash prove this is a corrupt saved identity, not deployment churn.
        with self.assertRaisesRegex(ReplanRequired, "contradicts the pinned facts hash"):
            self.restore(pin)
        self.raw = b"invalid gzip named by an invalid saved pin"
        pin["source_commit"] = self.data["source_commit"]
        pin["facts_sha256"] = hashlib.sha256(self.raw).hexdigest()
        with self.assertRaisesRegex(ReplanRequired, "invalid hash-verified saved facts"):
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

    def cli_plan(self, target, min_prs=1):
        with patch.object(plan, "_own_login", return_value="tester"), \
             patch.object(gh, "recent_roadmap_commits", return_value=[]), \
             patch.object(gh, "open_progress_prs", return_value=[]), \
             patch.object(gh, "closed_prs_for_area", return_value={101: self.head}):
            return cli.main(["plan", "--roadmap-dir", str(self.roadmap), "--code-dir", str(self.code),
                             "--ref", "main", "--min-prs", str(min_prs), "--out", str(target)])

    def test_cli_plan_keeps_its_bundle_across_cache_loss_and_artifact_relocation(self):
        self.init_repo()
        target = self.root / "reports/plan.json"
        self.assertEqual(self.cli_plan(target), 0)
        p = json.loads(target.read_text())
        bundle = target.parent / p["documentation"]["bundle_file"]
        self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(),
                         p["documentation"]["facts_sha256"])
        shutil.rmtree(self.cache)
        relocated = self.root / "relocated"
        shutil.copytree(target.parent, relocated)
        shutil.rmtree(target.parent)
        self.error = ArtifactError("origin offline")
        self.requests.clear()
        output = self.root / "facts.json"
        self.assertEqual(cli.main(["facts", "--plan", str(relocated / "plan.json"), "--code-dir",
                                   str(self.code), "--out", str(output)]), 0)
        self.assertEqual(json.loads(output.read_text())["documentation"], p["documentation"])
        self.assertEqual(self.requests, [])

    def test_not_due_discovery_does_not_accumulate_bundles(self):
        self.init_repo()
        target = self.root / "reports/plan.json"
        self.assertEqual(self.cli_plan(target, min_prs=2), 75)
        self.assertFalse(self.cache.exists())
        self.assertFalse(target.parent.exists())

    def test_failed_atomic_plan_replacement_preserves_the_previous_pin_and_bundle(self):
        self.init_repo()
        target = self.root / "reports/plan.json"
        self.assertEqual(self.cli_plan(target), 0)
        previous = target.read_bytes()
        previous_pin = json.loads(previous)["documentation"]
        self.data["provenance"]["doc_gen"] += "-next-export"
        self.raw = pack(self.data)
        replace = os.replace

        def interrupted(source, destination):
            if pathlib.Path(destination) == target:
                raise OSError("interrupted plan rename")
            return replace(source, destination)

        with patch("os.replace", side_effect=interrupted):
            self.assertEqual(self.cli_plan(target), 77)
        self.assertEqual(target.read_bytes(), previous)
        sibling = target.parent / previous_pin["bundle_file"]
        self.assertEqual(hashlib.sha256(sibling.read_bytes()).hexdigest(), previous_pin["facts_sha256"])

    def test_cli_plan_without_a_bundle_is_an_infrastructure_wait(self):
        self.init_repo()
        for error in (ArtifactNotFound("HTTP 404"), ArtifactError("HTTP 503")):
            with self.subTest(error=str(error)):
                self.error = error
                target = self.root / "unavailable-plan.json"
                self.assertEqual(self.cli_plan(target), 77)
                self.assertFalse(target.exists())

    def test_a_plan_without_a_pin_is_replanned(self):
        self.init_repo()
        for pin in ({}, {"documentation": None}):
            with self.subTest(pin=pin):
                plan_file = self.root / "plan.json"
                plan_file.write_text(json.dumps({"from_sha": self.first, "to_sha": self.head,
                                                 "prs": [101], **pin}))
                self.assertEqual(cli.main(["facts", "--plan", str(plan_file), "--code-dir",
                                           str(self.code)]), 78)
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, self.first, self.head, pr_numbers=[101])

    def test_source_history_and_pin_plan_mismatch(self):
        self.init_repo()
        with self.assertRaisesRegex(ReplanRequired, "source/window history"):
            facts.collect(self.code, "0" * 40, self.head, docs=self.reader())
        with self.assertRaisesRegex(ReplanRequired, "differs from plan"):
            facts.collect(self.code, self.first, self.first, docs=self.reader())
        self.data["source_commit"] = "c" * 40
        self.raw = pack(self.data)
        with self.assertRaisesRegex(ArtifactError, "not in main history"):
            self.build_plan({101: self.head})

    def test_removed_module_does_not_require_coverage(self):
        self.init_repo()
        self.commit("feat: temporary (#102)", {"Demo/Gone.lean": "theorem gone : True := trivial\n"})
        window.git(["rm", "-q", "Demo/Gone.lean"], self.code)
        window.git(["commit", "-qm", "remove Gone (#103)"], self.code)
        self.head = window.head_sha(self.code, "HEAD")
        self.data["source_commit"] = self.head
        self.raw = pack(self.data)
        result = facts.collect(self.code, self.first, self.head, docs=self.reader())
        self.assertEqual(len(result["declarations"]), 16)

    def test_non_ascii_module_paths_match_git_coverage(self):
        self.init_repo()
        head = self.commit("feat: unicode module (#102)", {"Demo/É space.lean": "import Demo.A\n"})
        self.assertEqual(facts.required_modules(self.code, self.head, head, [102]), ["Demo/É space.lean"])

    def test_lost_pinned_copy_replans_on_404_or_complete_changed_schema(self):
        self.init_repo()
        pin = self.reader().pin()
        (self.restore(pin).cache_dir / "facts.json.gz").unlink()
        target, output = self.root / "plan.json", self.root / "facts.json"
        p = {"from_sha": self.first, "to_sha": self.head, "prs": [101], "documentation": pin}
        target.write_text(json.dumps(p))
        argv = ["facts", "--plan", str(target), "--code-dir", str(self.code), "--out", str(output)]
        self.error = ArtifactNotFound("HTTP 404")
        self.assertEqual(cli.main(argv), 76)
        self.error = None
        self.raw = pack({**self.data, "schema_version": 2})
        self.assertEqual(cli.main(argv), 76)
        self.raw = pack({**self.data, "provenance": {}})
        self.assertEqual(cli.main(argv), 76)
        self.assertEqual(self.cli_plan(self.root / "bad-discovery.json"), 77)
        self.raw = self.raw[:-8]
        self.assertEqual(cli.main(argv), 77)
        self.assertFalse(output.exists())
        self.assertEqual(json.loads(target.read_text()), p)

    def test_invalid_history_is_rejected_before_a_pin_fetch(self):
        self.init_repo()
        pin = self.reader().pin()
        (self.restore(pin).cache_dir / "facts.json.gz").unlink()
        self.requests.clear()
        with self.assertRaises(ReplanRequired):
            facts.collect(self.code, "0" * 40, self.head, pr_numbers=[101], documentation=pin)
        self.assertEqual(self.requests, [])

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
                                   "--out", str(output)]), 78)
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
