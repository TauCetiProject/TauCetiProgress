"""D0 wire fixture, generation races, integrity failures, and plan/facts integration."""
import copy
import gzip
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from progress import apply, cli, facts, gh, plan, window
from progress.docs import Docs
from progress.immutable_docs import ArtifactError, ArtifactNotFound, ConfigurationError, FactsDocs, discover

FIXTURE = pathlib.Path(__file__).parent / "fixtures/facts-v1"
BASE = "https://facts.example/progress"


def encoded(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode()


def generation(data=None, manifest_changes=None, bundle=None):
    """Publish the shared fixture with optional edits, preserving all outer hashes."""
    data = copy.deepcopy(data or json.loads((FIXTURE / "facts.json").read_bytes()))
    unpacked = encoded(data)
    raw = gzip.compress(unpacked, mtime=0) if bundle is None else bundle
    digest = hashlib.sha256(raw).hexdigest()
    path = f"v1/generations/{data['source_commit']}/{digest}"
    manifest = {"schema_version": 1, "source_commit": data["source_commit"], "generation": path,
                "facts_sha256": digest, "compressed_bytes": len(raw), "uncompressed_bytes": len(unpacked),
                "modules": sorted(data["modules"]),
                "provenance": {"toolchain": "leanprover/lean4:v4.24.0", "doc_gen": "fixture-doc-gen4"}}
    manifest.update(manifest_changes or {})
    manifest_raw = encoded(manifest)
    pointer = {"schema_version": 1, "source_commit": data["source_commit"],
               "manifest_path": path + "/manifest.json",
               "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()}
    return {"v1/current.json": encoded(pointer), path + "/manifest.json": manifest_raw,
            path + "/facts.json.gz": raw}


class ImmutableDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.objects = {str(p.relative_to(FIXTURE)): p.read_bytes()
                        for p in (FIXTURE / "v1").rglob("*") if p.is_file()}
        self.requests = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "auto", "TAUCETI_FACTS_BASE": BASE,
                                "TAUCETI_FACTS_CACHE": str(self.cache)}).start()
        # Neither the immutable reader nor facts may introduce provider calls.
        patch.object(gh, "gh", side_effect=AssertionError("unexpected GitHub API traffic")).start()

    def fetch(self, url):
        self.assertTrue(url.startswith(BASE + "/"), url)
        rel = url.removeprefix(BASE + "/")
        self.requests.append(rel)
        try:
            return self.objects[rel]
        except KeyError:
            raise ArtifactNotFound("object unavailable: " + rel)

    def reader(self):
        return discover(BASE, cache_dir=self.cache, opener=self.fetch)

    def pinned(self, pin):
        return FactsDocs(pin, cache_dir=self.cache, opener=self.fetch)

    def test_shared_wire_fixture(self):
        # Gzip compression/header bytes may differ between Python/zlib versions. Verify the
        # checked-in wire bytes directly rather than requiring byte-identical recompression.
        raw = next(value for path, value in self.objects.items() if path.endswith(".gz"))
        self.assertEqual(gzip.decompress(raw), (FIXTURE / "facts.json").read_bytes())
        reader = self.reader()
        self.assertEqual(reader.source_commit(), "a" * 40)
        self.assertEqual(reader.declarations("TauCeti/Empty.html"), {})
        declarations = reader.declarations("TauCeti/A.html")
        self.assertEqual(declarations["TauCeti.alpha"]["kind"], "theorem")
        self.assertIsNone(declarations["TauCeti.alpha.generated"]["start"])
        self.assertEqual(declarations["TauCeti.alpha"]["url"],
                         "https://taucetiproject.org/docs/TauCeti/A.html#TauCeti.alpha")

    def test_two_deploys_and_mixed_pointer_timing(self):
        old = self.reader()
        pin = old.pin()
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        data["source_commit"] = "b" * 40
        data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"]["kind"] = "def"
        self.objects.update(generation(data))
        newer = self.reader()
        self.assertEqual(newer.source_commit(), "b" * 40)
        # Even without the local cache, a saved pin reads only its original immutable paths.
        import shutil
        shutil.rmtree(self.cache)
        self.requests.clear()
        self.assertEqual(self.pinned(pin).declarations("TauCeti/A.html"),
                         old.declarations("TauCeti/A.html"))
        self.assertNotIn("v1/current.json", self.requests)
        # Pointer replacement after the GET, before fetching the manifest, is harmless too.
        self.objects["v1/current.json"] = encoded({k: v for k, v in pin.items()
                                                   if k in ("schema_version", "source_commit",
                                                            "manifest_path", "manifest_sha256")})
        def racing_fetch(url):
            raw = self.fetch(url)
            if url.endswith("current.json"):
                self.objects.update(generation(data))
            return raw
        self.assertEqual(discover(BASE, cache_dir=self.cache, opener=racing_fetch).pin(), pin)

    def test_cached_pin_works_when_pointer_and_origin_are_unavailable(self):
        old = self.reader()
        self.objects.clear()
        self.requests.clear()
        self.assertEqual(self.pinned(old.pin()).pin(), old.pin())
        self.assertEqual(self.requests, [])
        self.assertIsInstance(self.reader(), Docs)  # no generation selected in a new plan

    def test_cache_from_another_origin_is_never_reused(self):
        pin = self.reader().pin()
        pin["facts_base"] = "https://other.example/progress"
        with self.assertRaisesRegex(ArtifactError, "other origin"):
            FactsDocs(pin, cache_dir=self.cache,
                      opener=lambda url: (_ for _ in ()).throw(ArtifactError("other origin unavailable")))

    def test_unsupported_or_unavailable_pointer_uses_legacy(self):
        for raw in (b'{"schema_version":2}',):
            self.objects["v1/current.json"] = raw
            self.assertIsInstance(self.reader(), Docs)
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "legacy"}):
            self.requests.clear()
            self.assertIsInstance(self.reader(), Docs)
            self.assertEqual(self.requests, [])

    def test_pointer_http_errors_are_distinct_from_absence(self):
        for status in (403, 429, 500):
            with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                    BASE, status, "unavailable", None, None)):
                with self.assertRaises(ArtifactError):
                    discover(BASE)
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                BASE, 404, "not found", None, None)):
            self.assertIsInstance(discover(BASE), Docs)
            with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "immutable"}):
                with self.assertRaisesRegex(ArtifactError, "immutable facts are required"):
                    discover(BASE)

    def test_strict_mode_and_invalid_configuration(self):
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "immutable"}):
            self.objects["v1/current.json"] = b'{"schema_version":2}'
            with self.assertRaisesRegex(ArtifactError, "immutable facts are required"):
                self.reader()
        with patch.dict(os.environ, {"TAUCETI_FACTS_MODE": "typo"}):
            with self.assertRaises(ConfigurationError):
                self.reader()

    def test_supported_pointer_failures_never_fall_back(self):
        pointer = json.loads(self.objects["v1/current.json"])
        for edit in ({"manifest_sha256": "0" * 64}, {"source_commit": "b" * 40},
                     {"manifest_path": "https://other.example/manifest.json"},
                     {"manifest_path": "../manifest.json"}):
            with self.subTest(edit=edit):
                self.objects["v1/current.json"] = encoded({**pointer, **edit})
                with self.assertRaises(ArtifactError):
                    self.reader()
        self.objects["v1/current.json"] = b"{truncated"
        with self.assertRaisesRegex(ArtifactError, "JSON"):
            self.reader()
        for raw in (b'{}', b'{"schema_version":"1"}', b'{"schema_version":true}'):
            self.objects["v1/current.json"] = raw
            with self.assertRaisesRegex(ArtifactError, "schema_version"):
                self.reader()
        self.objects["v1/current.json"] = b'{"schema_version":1,"schema_version":2}'
        with self.assertRaisesRegex(ArtifactError, "duplicate JSON key"):
            self.reader()

    def test_manifest_schema_source_lengths_hash_and_coverage(self):
        for edit in ({"schema_version": 2}, {"source_commit": "b" * 40},
                     {"compressed_bytes": 1}, {"uncompressed_bytes": 1},
                     {"facts_sha256": "0" * 64}, {"modules": []}, {"provenance": {}},
                     {"generation": "v1/generations/other"}):
            with self.subTest(edit=edit):
                self.objects = generation(manifest_changes=edit)
                with self.assertRaises(ArtifactError):
                    self.reader()

    def test_bundle_schema_and_ranges(self):
        for edit in ({"schema_version": 2},):
            data = json.loads((FIXTURE / "facts.json").read_bytes())
            data.update(edit)
            self.objects = generation(data)
            with self.assertRaisesRegex(ArtifactError, "schema"):
                self.reader()
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"]["end"] = 0
        self.objects = generation(data)
        with self.assertRaisesRegex(ArtifactError, "source range"):
            self.reader()
        del data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"]["start"]
        del data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"]["end"]
        self.objects = generation(data)
        with self.assertRaisesRegex(ArtifactError, "source range"):
            self.reader()

    def test_truncated_bundle_with_matching_hash_is_refused(self):
        self.objects = generation(bundle=gzip.compress(b'{"modules":{}}', mtime=0)[:-5])
        with self.assertRaisesRegex(ArtifactError, "gzip"):
            self.reader()

    def test_bundle_source_identity_is_checked(self):
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        data["source_commit"] = "b" * 40
        self.objects = generation(bundle=gzip.compress(encoded(data), mtime=0))
        with self.assertRaisesRegex(ArtifactError, "source commit mismatch"):
            self.reader()

    def test_declaration_schema_and_duplicate_names(self):
        for edit in ({"file": "TauCeti/Other.lean"}, {"url": "http://["},
                     {"url": "https://taucetiproject.org/docs/TauCeti/A.html#wrong"},
                     {"url": "https://taucetiproject.org/docs/Other.html#TauCeti.alpha"}):
            with self.subTest(edit=edit):
                data = json.loads((FIXTURE / "facts.json").read_bytes())
                data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"].update(edit)
                self.objects = generation(data)
                with self.assertRaises(ArtifactError):
                    self.reader()
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        info = data["modules"]["TauCeti/A.lean"]["TauCeti.alpha"]
        data["modules"]["TauCeti/Empty.lean"]["TauCeti.alpha"] = info
        self.objects = generation(data)
        with self.assertRaisesRegex(ArtifactError, "duplicate declaration"):
            self.reader()
        raw = encoded(json.loads((FIXTURE / "facts.json").read_bytes()))
        raw = raw.replace(b'"kind":"theorem"', b'"kind":"theorem","kind":"def"')
        self.objects = generation(bundle=gzip.compress(raw, mtime=0),
                                  manifest_changes={"uncompressed_bytes": len(raw)})
        with self.assertRaisesRegex(ArtifactError, "duplicate JSON key"):
            self.reader()

    def test_corrupt_bundle_is_refused_and_corrupt_cache_is_repaired(self):
        reader = self.reader()
        cache_file = next(self.cache.rglob("*-facts.json.gz"))
        cache_file.write_bytes(b"truncated")
        self.assertEqual(self.pinned(reader.pin()).pin(), reader.pin())
        cache_file.write_bytes(b"truncated")
        self.objects[next(k for k in self.objects if k.endswith(".gz"))] = b"bad hash"
        with self.assertRaisesRegex(ArtifactError, "SHA-256"):
            self.pinned(reader.pin())

    def test_manifest_cache_and_saved_coverage_are_verified(self):
        reader = self.reader()
        next(self.cache.rglob("*-manifest.json")).write_bytes(b"corrupt manifest")
        self.assertEqual(self.pinned(reader.pin()).pin(), reader.pin())
        pin = reader.pin()
        pin["coverage"] = []
        with self.assertRaisesRegex(ArtifactError, "coverage changed"):
            self.pinned(pin)

    def repo(self):
        self.code = self.root / "code"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.code)], check=True)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
                    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e"}
        self.first = self.commit("init", {"README.md": "fixture\n"})
        self.head = self.commit("feat: alpha (#101)", {
            "TauCeti/A.lean": "/-- **Alpha's theorem.** It states alpha. -/\ntheorem alpha : True := trivial\n",
            "TauCeti/Empty.lean": "import TauCeti.A\n"})
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        data["source_commit"] = self.head
        self.objects = generation(data)
        return data

    def commit(self, subject, changes):
        for path, text in changes.items():
            target = self.code / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink() if text is None else target.write_text(text)
        for args in (["add", "-A"], ["commit", "-q", "-m", subject]):
            subprocess.run(["git", "-C", str(self.code), *args], check=True, env=self.env,
                           capture_output=True)
        return window.head_sha(self.code, "HEAD")

    def build_plan(self, reader):
        roadmap = self.root / "roadmap" / "TauCetiRoadmap" / "Algebra"
        roadmap.mkdir(parents=True, exist_ok=True)
        (roadmap / "README.md").write_text("# Algebra\n")
        with patch.object(plan, "_own_login", return_value="test-user"), \
             patch.object(gh, "closed_prs_for_area", return_value={101: self.head}):
            return plan.build_plan(roadmap.parents[1], self.code, ref="main", min_prs=1,
                                   commits=[], open_prs=[], docs=reader)

    def test_plan_persists_pin_and_cli_uses_it_after_cutover(self):
        self.repo()
        # Exercise configured discovery as well as direct reader injection.
        with patch.object(FactsDocs, "_fetch", side_effect=self.fetch):
            p = self.build_plan(None)
        self.assertEqual(p["documentation"]["coverage"], ["TauCeti/A.lean", "TauCeti/Empty.lean"])
        plan_file, output = self.root / "plan.json", self.root / "facts.json"
        plan_file.write_text(plan.plan_json(p))
        self.objects.clear()
        # Facts consumes the saved pin and cached bytes without calling current.json or HTML.
        with patch.object(FactsDocs, "_fetch", side_effect=AssertionError("unexpected HTTP")):
            rc = cli.main(["facts", "--plan", str(plan_file), "--code-dir", str(self.code),
                           "--out", str(output)])
        self.assertEqual(rc, 0)
        result = json.loads(output.read_text())
        self.assertEqual(result["documentation"], p["documentation"])
        self.assertEqual([d["name"] for d in result["declarations"]], ["TauCeti.alpha"])
        self.assertEqual(result["declarations"][0]["pr"], 101)
        self.assertEqual(result["declarations"][0]["doc"], "**Alpha's theorem.** It states alpha.")

    def test_null_saved_pin_does_not_switch_to_legacy(self):
        self.repo()
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps({"from_sha": self.first, "to_sha": self.head, "prs": [101],
                                         "documentation": None}))
        with patch("progress.docs.Docs", side_effect=AssertionError("must not use legacy")):
            self.assertEqual(cli.main(["facts", "--plan", str(plan_file), "--code-dir", str(self.code)]), 78)

    def test_missing_required_module_refuses_plan_and_facts_without_writing(self):
        data = self.repo()
        del data["modules"]["TauCeti/Empty.lean"]
        self.objects = generation(data)
        reader = self.reader()
        with self.assertRaisesRegex(ArtifactError, "missing required modules"):
            self.build_plan(reader)
        p = {"from_sha": self.first, "to_sha": self.head, "prs": [101],
             "documentation": reader.pin()}
        plan_file, output = self.root / "plan.json", self.root / "facts.json"
        plan_file.write_text(json.dumps(p))
        with self.assertRaisesRegex(ArtifactError, "missing required modules"):
            facts.collect(self.code, self.first, self.head, pr_numbers=[101], docs=reader)
        self.assertEqual(cli.main(["facts", "--plan", str(plan_file), "--code-dir", str(self.code),
                                   "--out", str(output)]), 77)
        self.assertFalse(output.exists())
        self.assertEqual(json.loads(plan_file.read_text()), p)  # cursor/pin untouched

    def test_source_history_and_pin_plan_mismatch(self):
        self.repo()
        with self.assertRaisesRegex(ArtifactError, "source/window history"):
            facts.collect(self.code, "0" * 40, self.head, docs=self.reader())
        with self.assertRaisesRegex(ArtifactError, "differs from plan"):
            facts.collect(self.code, self.first, self.first, docs=self.reader())
        data = json.loads((FIXTURE / "facts.json").read_bytes())
        self.objects = generation(data)
        with self.assertRaisesRegex(ArtifactError, "not in main history"):
            self.build_plan(self.reader())

    def test_removed_module_does_not_require_coverage(self):
        data = self.repo()
        self.commit("feat: temporary (#102)", {"TauCeti/B.lean": "theorem b : True := trivial\n"})
        self.head = self.commit("remove B (#103)", {"TauCeti/B.lean": None})
        data["source_commit"] = self.head
        self.objects = generation(data)
        result = facts.collect(self.code, self.first, self.head, docs=self.reader())
        self.assertEqual([d["name"] for d in result["declarations"]], ["TauCeti.alpha"])

    def test_non_ascii_module_paths_match_git_coverage(self):
        self.repo()
        self.head = self.commit("feat: unicode module (#102)", {"TauCeti/É space.lean": "import TauCeti.A\n"})
        self.assertEqual(facts.required_modules(self.code, self.first, self.head, [102]),
                         ["TauCeti/É space.lean"])

    def test_legacy_saved_plan_does_not_discover_a_new_source(self):
        data = self.repo()
        legacy = Docs()
        with patch("progress.docs.Docs", return_value=legacy), \
             patch.object(legacy, "source_commit", return_value=self.head), \
             patch.object(legacy, "declarations", side_effect=lambda page:
                          data["modules"][page.removesuffix(".html") + ".lean"]), \
             patch("progress.immutable_docs.discover", side_effect=AssertionError("must use legacy")):
            result = facts.collect(self.code, self.first, self.head, pr_numbers=[101])
        self.assertEqual([d["name"] for d in result["declarations"]], ["TauCeti.alpha"])
        self.assertNotIn("documentation", result)

    def test_prepared_immutable_report_resumes_after_pointer_changes(self):
        self.repo()
        p = self.build_plan(self.reader())
        roadmap = self.root / "roadmap"
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"],
                     ["config", "user.name", "T"], ["add", "."], ["commit", "-qm", "base"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"]):
            window.git(args, roadmap)
        status, section = self.root / "status", self.root / "section"
        prose = "Alpha's theorem now supplies the required statement for the algebra roadmap. " * 4
        status.write_text(prose)
        section.write_text(prose)
        with patch.object(apply, "existing_pr", return_value=None), \
             patch.object(apply, "own_pr", return_value=None):
            self.assertEqual(apply.run(p, status, section, roadmap, dry_run=True), 0)
            prepared = window.head_sha(roadmap, "HEAD")
            data = json.loads((FIXTURE / "facts.json").read_bytes())
            data["source_commit"] = "b" * 40
            self.objects = generation(data)
            with patch.object(FactsDocs, "_fetch", side_effect=AssertionError("must reuse report")), \
                 patch.object(apply, "render_update", side_effect=AssertionError("must reuse prose")), \
                 patch.object(apply, "push_target", return_value=("fork", "tester")), \
                 patch.object(apply, "remote_branch_exists", return_value=True), \
                 patch.object(apply, "sweep", return_value=0), \
                 patch.object(gh, "gh", return_value="https://github.com/org/roadmap/pull/1") as publish:
                self.assertEqual(apply.run(p, status, section, roadmap), 0)
                publish.assert_called_once()
            self.assertEqual(window.head_sha(roadmap, "HEAD"), prepared)


if __name__ == "__main__":
    unittest.main()
