"""Protected workflow policy snapshots and independent merge-policy comparison."""

import contextlib
import io
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

from progress import apply, cli, facts, gate  # noqa: E402
from progress.docs import ReplanRequired  # noqa: E402
from progress.provenance import MigrationMap, PullSource, retain_migration, saved_migration  # noqa: E402

SCRIPT = ROOT / ".github/scripts/history_policy.py"


class ProtectedPolicy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-protected-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.policy = MigrationMap(commits={"a" * 40: PullSource.parse("owner/Math#7")})
        (self.checkout / "progress-history.json").write_text(json.dumps(self.policy.record()))
        self.output = self.root / "snapshot.json"
        self.envfile = self.root / "github-env"

    def run_policy(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repository",
                "owner/Configuration",
                "--ref",
                "b" * 40,
                "--root",
                str(self.checkout),
                "--output",
                str(self.output),
                "--github-env",
                str(self.envfile),
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_actual_preparation_script_writes_canonical_snapshot_and_environment(self):
        result = self.run_policy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(MigrationMap.parse(json.loads(self.output.read_text())).digest, self.policy.digest)
        self.assertEqual(self.envfile.read_text(), f"TAUCETI_PROGRESS_HISTORY_MAP={self.output}\n")
        self.assertIn(self.policy.digest, result.stdout)

    def test_missing_immutable_ref_or_escaping_symlinked_policy_is_refused(self):
        for extra in (("--ref", "main"), ("--path", "../progress-history.json"), ("--path", "/etc/passwd")):
            result = self.run_policy(*extra)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(self.output.exists())
        path = self.checkout / "progress-history.json"
        real = self.root / "real.json"
        path.rename(real)
        path.symlink_to(real)
        self.assertEqual(self.run_policy().returncode, 1)
        self.assertFalse(self.output.exists())

    def test_legacy_empty_workflow_policy_needs_no_extra_checkout(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(self.output)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(MigrationMap.parse(json.loads(self.output.read_text())).digest, MigrationMap().digest)

    def test_metadata_cannot_choose_the_gate_policy_and_legacy_reports_still_work(self):
        plan = {
            "roadmap": "Area",
            "from_sha": "c" * 40,
            "to_sha": "d" * 40,
            "prs": [7],
            "history_migration_sha256": self.policy.digest,
        }
        body = apply.pr_body(plan, {})
        gate.check_history_policy({"body": body}, self.policy.digest, empty=False)
        with self.assertRaisesRegex(gate.Refused, "history policy differs"):
            gate.check_history_policy({"body": body}, MigrationMap().digest, empty=True)
        with self.assertRaisesRegex(gate.Refused, "history policy differs"):
            gate.check_history_policy({"body": "legacy"}, self.policy.digest, empty=False)
        gate.check_history_policy({"body": "legacy"}, MigrationMap().digest, empty=True)

    def test_large_policy_is_retained_beside_small_plan_and_verified_on_restore(self):
        policy = MigrationMap(commits={f"{n:040x}": PullSource.parse(f"owner/Math#{n}") for n in range(1, 5001)})
        path = self.root / "plan.json"
        plan = {"history_migration_sha256": policy.digest,
                "history_migration_file": retain_migration(policy, path)}
        self.assertLess(len(json.dumps(plan)), 300)
        self.assertEqual(saved_migration(plan, path).digest, policy.digest)
        with self.assertRaisesRegex(ReplanRequired, "invalid"):
            saved_migration({**plan, "history_migration_file": "../outside.json"}, path)
        (path.parent / plan["history_migration_file"]).write_text(json.dumps(MigrationMap().record()))
        with self.assertRaisesRegex(ReplanRequired, "invalid"):
            saved_migration(plan, path)

    def test_missing_or_corrupt_retained_policy_has_typed_recovery_before_work(self):
        path = self.root / "plan.json"
        plan = {"documentation": {}, "history_migration_sha256": self.policy.digest,
                "history_migration_file": retain_migration(self.policy, path)}
        path.write_text(json.dumps(plan))
        sidecar = path.parent / plan["history_migration_file"]
        for content, expected_facts in ((None, 76), ("bad JSON", 78)):
            if content is None:
                sidecar.unlink()
            else:
                sidecar.write_text(content)
            with (patch.object(facts, "collect") as collect, patch.object(apply, "run") as publish,
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(cli.main(["facts", "--plan", str(path), "--code-dir", str(self.root)]), expected_facts)
                self.assertEqual(cli.main([
                    "apply", "--plan", str(path), "--status-body", "paid-status",
                    "--section-body", "paid-section", "--roadmap-dir", str(self.root),
                ]), 78)
            collect.assert_not_called()
            publish.assert_not_called()

    def test_nonempty_policy_requires_output_file_before_planning(self):
        with (patch.dict(os.environ, TAUCETI_PROGRESS_HISTORY_MAP=str(self.checkout / "progress-history.json")),
              patch("progress.plan.build_plan") as build, contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(cli.main(["plan", "--roadmap-dir", str(self.root), "--code-dir", str(self.root)]), 1)
        build.assert_not_called()

    def test_legacy_plan_with_changed_policy_requests_free_mechanical_replan(self):
        saved = self.root / "legacy-plan.json"
        saved.write_text(
            json.dumps(
                {"from_sha": "c" * 40, "to_sha": "d" * 40, "prs": [7], "documentation": {"source_commit": "d" * 40}}
            )
        )
        with (
            patch.dict(os.environ, TAUCETI_PROGRESS_HISTORY_MAP=str(self.checkout / "progress-history.json")),
            patch.object(facts, "collect") as collect,
            contextlib.redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(cli.main(["facts", "--plan", str(saved), "--code-dir", str(self.root)]), 76)
        collect.assert_not_called()
        self.assertIn("facts-replan-required", error.getvalue())


if __name__ == "__main__":
    unittest.main()
