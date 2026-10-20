"""The mechanical retry signal is distinct from invalid facts and unavailable documentation."""

import contextlib
import io
import json
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import cli, facts, plan
from progress.docs import ArtifactError, ArtifactNotFound, DocsGenerationChanged

OLD, NEW = "a" * 40, "b" * 40

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    plan_file, out = root / "plan.json", root / "facts.json"
    plan_file.write_text(json.dumps({"from_sha": "c" * 40, "to_sha": OLD, "prs": [1],
                                     "documentation": {"source_commit": OLD}}))
    argv = ["facts", "--plan", str(plan_file), "--code-dir", tmp, "--out", str(out)]

    # A deploy between stages: no facts artifact or success output is possible.
    with patch.object(facts, "collect", side_effect=DocsGenerationChanged(OLD, NEW)):
        err, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(stdout):
            assert cli.main(argv) == cli.EX_DOCS_GENERATION_CHANGED
        assert "facts: documentation-generation-changed:" in err.getvalue()
        assert f"expected={OLD}" in err.getvalue() and f"observed={NEW}" in err.getvalue()
        assert not out.exists() and not stdout.getvalue()

    # Unavailable documentation waits (77), and an invalid window is an error (1); neither
    # acquires the retry signal.
    for error, code in ((ArtifactError("HTTP 503"), 77), (ArtifactNotFound("HTTP 404"), 77),
                        (facts.FactsError("invalid window"), 1)):
        with patch.object(facts, "collect", side_effect=error):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                assert cli.main(argv) == code, (error, code)
            assert str(error) in err.getvalue() and not out.exists()

    # Planning passes the typed result through rather than downgrading it to NotDue.
    with patch("progress.docs.discover") as factory, \
         patch.object(plan, "_own_login", return_value="tester"):
        factory.return_value.source_commit.side_effect = DocsGenerationChanged(OLD, NEW)
        try:
            plan.build_plan(tmp, tmp, commits=[], open_prs=[])
        except DocsGenerationChanged:
            pass
        else:
            raise AssertionError("plan swallowed the retry signal")
    with patch.object(plan, "build_plan", side_effect=DocsGenerationChanged(OLD, NEW)):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert cli.main(["plan", "--code-dir", tmp, "--roadmap-dir", tmp,
                             "--out", str(out)]) == cli.EX_DOCS_GENERATION_CHANGED
        assert "plan: documentation-generation-changed:" in err.getvalue()
        assert not out.exists()
    with patch.object(plan, "build_plan", side_effect=plan.NotDue("busiest area too small")):
        with contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["plan", "--code-dir", tmp, "--roadmap-dir", tmp]) == 75

print("all generation-change tests passed")
