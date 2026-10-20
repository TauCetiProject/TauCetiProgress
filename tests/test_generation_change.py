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
from progress.docs import Docs, DocsError, DocsGenerationChanged, DocsNotFound, INDEX_PATH

OLD, NEW = "a" * 40, "b" * 40


def page(sha):
    return (f'<div class="decl" id="A.t"><div class="gh_link">'
            f'<a href="https://github.com/o/r/blob/{sha}/TauCeti/A.lean#L1-L2">src</a></div></div>')


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    plan_file, out = root / "plan.json", root / "facts.json"
    plan_file.write_text(json.dumps({"from_sha": "c" * 40, "to_sha": OLD, "prs": [1]}))
    argv = ["facts", "--plan", str(plan_file), "--code-dir", tmp, "--out", str(out)]
    # A deploy between stages: no git work, facts artifact or success output is possible.
    with patch("progress.docs.Docs") as factory, patch("progress.window.git") as git:
        factory.return_value.source_commit.return_value = NEW
        err, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(stdout):
            assert cli.main(argv) == cli.EX_DOCS_GENERATION_CHANGED
        assert "facts: documentation-generation-changed:" in err.getvalue()
        assert OLD[:7] in err.getvalue() and NEW[:7] in err.getvalue()
        assert not out.exists() and not stdout.getvalue()
        git.assert_not_called()

    # Fetch failure and genuinely invalid extraction never acquire the retry signal.
    for error in (DocsError("HTTP 503"), DocsNotFound("probe HTTP 404")):
        with patch("progress.docs.Docs") as factory:
            factory.return_value.source_commit.side_effect = error
            with contextlib.redirect_stderr(io.StringIO()) as err:
                assert cli.main(argv) == 1
            assert str(error) in err.getvalue() and not out.exists()
    with patch.object(facts, "collect", side_effect=facts.FactsError("invalid window")):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert cli.main(argv) == 1
        assert "invalid window" in err.getvalue() and not out.exists()

    # A mixed probe page can also fail planning; do not downgrade its typed result to NotDue.
    with patch("progress.docs.Docs") as factory:
        factory.return_value.source_commit.side_effect = DocsGenerationChanged(OLD, NEW)
        try:
            plan.docs_source_commit()
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

    # Within-stage drift keeps full generation identities, including when probing the first page.
    mixed = (f'<p class="gh_nav_link"><a href="https://github.com/o/r/blob/{OLD}/'
             'TauCeti/A.lean">src</a></p>') + page(NEW)
    pages = {INDEX_PATH: json.dumps({"declarations": {"A.t": {"docLink": "./probe.html#A.t"}}}),
             "probe.html": mixed}
    reader = Docs(cache_dir=root / "cache", opener=lambda url: pages[url.rsplit("/", 1)[-1]]
                  if not url.endswith(INDEX_PATH) else pages[INDEX_PATH])
    try:
        reader.source_commit()
    except DocsGenerationChanged as exc:
        assert exc.expected == OLD and exc.observed == NEW
    else:
        raise AssertionError("mixed source links must never choose a generation")

print("all generation-change tests passed")
