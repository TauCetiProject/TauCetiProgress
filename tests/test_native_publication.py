"""Qualified report framing and origin/actor-bound native mutation recovery."""

import copy
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tauceti_tools.forge import Forge, ForgeError, Incomplete, RepositoryRef  # noqa: E402

from progress import announce, apply, files, gate, gh, publication_input  # noqa: E402
from progress.native_publish import NativePublisher  # noqa: E402
from progress.native_source import NativeRepository, NativeSource  # noqa: E402


class Api:
    def __init__(self, origin="https://source.example"):
        self.forge = Forge("forgejo", origin)
        self._token = "fixture-token"
        self.actor_id = 7
        self.rows = {
            name: {"id": number, "full_name": name, "owner": {"id": 2}, "permissions": {"push": True},
                   "archived": False, "fork": False, "parent": None}
            for name, number in (("team/Code", 11), ("team/Roadmap", 12))
        }
        self.calls, self.owned = [], []
        self.failure = False

    def repository(self, ref):
        return copy.deepcopy(self.rows[ref.name])

    def current_user(self):
        return {"id": self.actor_id, "login": "operator"}

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if self.failure:
            raise ForgeError("lost native response")
        return {}

    def collection(self, path, **options):
        if path == "user/repos":
            return copy.deepcopy(self.owned)
        raise AssertionError(path)


class NativePublication(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-native-publish-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.environment = patch.dict(os.environ, {"TAUCETI_PROGRESS_OUTBOX": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.api = Api()
        self.source = self.bound(self.api)
        self.publisher = NativePublisher(self.source, token=self.api._token, actor_id=7)
        self.plan = {"source": self.source.record(), "roadmap": "Area", "rel_dir": "TauCetiRoadmap/Area",
                     "from_sha": "a" * 40, "to_sha": "b" * 40, "prs": [7],
                     "status_path": "TauCetiRoadmap/Area/STATUS.md", "progress_path": "TauCetiRoadmap/Area/PROGRESS.md"}
        self.prose = "This report describes the mathematical work in this precise commit window. " * 4

    def bound(self, api):
        return NativeSource(NativeRepository(RepositoryRef(api.forge, "team/Code"), api),
                            NativeRepository(RepositoryRef(api.forge, "team/Roadmap"), api))

    def framed(self):
        return apply.render_update(self.plan, self.prose, self.prose, None, None)

    def test_qualified_headers_round_trip_and_are_checked_together(self):
        status, progress, header = self.framed()
        self.assertEqual(files.parse_status(status)["source"], self.source.record())
        self.assertEqual(header["source"], self.source.record())
        wrong = self.bound(Api("https://other.example")).record()
        other_status = files.render_status("Area", "b" * 40, "", self.prose, source=wrong)
        with self.assertRaises(files.FormatError):
            files.validate_update("Area", None, other_status, None, progress)
        legacy_status = files.render_status("Area", "b" * 40, "", self.prose)
        with self.assertRaises(files.FormatError):
            files.validate_update("Area", None, legacy_status, None, progress)

    def test_pr_and_announcement_links_and_ids_follow_source_not_github_defaults(self):
        _, _, header = self.framed()
        body = apply.pr_body(self.plan, header)
        self.assertIn("https://source.example/team/Code/pulls/7", body)
        rendered = announce.render_message(header, self.prose)
        self.assertIn("https://source.example/team/Roadmap/src/branch/main/TauCetiRoadmap/Area/STATUS.md", rendered)
        other = {**header, "source": self.bound(Api("https://other.example")).record()}
        self.assertNotEqual(announce.section_id(header), announce.section_id(other))
        # Full windows, including equal seven-character prefixes, have distinct IDs.
        self.assertNotEqual(announce.section_id(header), announce.section_id({**header, "to_sha": "b" * 39 + "c"}))
        with self.assertRaises(ValueError):
            announce.render_message(header, self.prose, roadmap_url="https://github.com/team/Roadmap")

    def test_gate_source_is_independent_of_matching_pr_and_file_claims(self):
        _, _, header = self.framed()
        pr = {"body": apply.pr_body(self.plan, header)}
        with self.assertRaises(gate.Refused):
            gate.check_source_binding(pr, header, None)
        gate.check_source_binding(pr, header, self.source.record())
        with self.assertRaises(gate.Refused):
            gate.check_source_binding({"body": "unqualified"}, header, self.source.record())

    def test_qualified_history_cannot_silently_downgrade_or_switch_origins(self):
        old_status, old_progress, _ = self.framed()
        newer = {**self.plan, "from_sha": "b" * 40, "to_sha": "c" * 40}
        apply.render_update(newer, self.prose, self.prose, old_status, old_progress)
        for replacement in (None, self.bound(Api("https://other.example")).record()):
            changed = {**newer}
            if replacement is None:
                changed.pop("source")
            else:
                changed["source"] = replacement
            with self.assertRaises(files.FormatError):
                apply.render_update(changed, self.prose, self.prose, old_status, old_progress)

    def test_source_and_policy_binding_never_accepts_an_unbound_native_commit(self):
        with self.assertRaises(publication_input.InputMismatch):
            publication_input.require_message(self.plan, "legacy report")
        self.assertNotEqual(publication_input.digest(self.plan),
                            publication_input.digest({**self.plan, "source": self.bound(Api("https://other.example")).record()}))

    def test_lost_write_is_acknowledged_only_by_native_observation(self):
        self.api.failure = True
        context = {"candidate": "a" * 40}
        observed = []
        with self.assertRaises(gh.GhError):
            self.publisher._write(["create-report", 12, "branch", "main"], "repos/team/Roadmap/pulls", {},
                                  lambda: observed[0] if observed else None, context)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(list(self.root.glob("*.json"))), 1)
        observed.append("https://source.example/team/Roadmap/pulls/1")
        result = self.publisher._write(["create-report", 12, "branch", "main"], "repos/team/Roadmap/pulls", {},
                                       lambda: observed[0], context)
        self.assertEqual(result, observed[0])
        self.assertEqual(len(self.api.calls), 1)
        self.assertFalse(list(self.root.glob("*.json")))

    def test_changed_actor_or_repository_refuses_before_mutation(self):
        for mutate in (lambda: setattr(self.api, "actor_id", 8), lambda: self.api.rows["team/Code"].update(id=99)):
            mutate()
            with self.assertRaises(Incomplete):
                self.publisher._write(["fork"], "repos/team/Roadmap/forks", {}, lambda: False, {})
            self.api.actor_id = 7
        self.assertFalse(self.api.calls)

    def test_renamed_owned_fork_requires_native_ancestry_and_push_access(self):
        row = {"id": 31, "full_name": "operator/renamed", "owner": {"id": 7}, "permissions": {"push": True},
               "archived": False, "fork": True, "parent": self.api.rows["team/Roadmap"]}
        self.api.owned = [copy.deepcopy(row)]
        self.api.rows[row["full_name"]] = row
        found, absent = self.publisher._fork()
        self.assertEqual(found.native_id, 31)
        self.assertFalse(absent)
        self.api.rows[row["full_name"]]["permissions"]["push"] = False
        self.assertEqual(self.publisher._fork(), (None, None))
        self.api.rows[row["full_name"]]["id"] = 32
        with self.assertRaises(Incomplete):
            self.publisher._fork()


if __name__ == "__main__":
    unittest.main()
