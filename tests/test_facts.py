"""Tests for the factual spine.

Declaration names, kinds, links, docstrings and source positions come from doc-gen4; git blame
decides what belongs to the window. Neither requires understanding Lean, which is the point -- an
earlier version tried to derive names by tracking `namespace`/`section`/`end` in Python and got them
subtly wrong, which is the worst outcome for something whose output becomes a link.

The documentation is a real `Docs` reader over a facts bundle built here, served by a stub
transport, so the tests are hermetic.
"""

import gzip
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ["TAUCETI_FACTS_CACHE"] = tempfile.mkdtemp(prefix="tauceti-facts-test-")

from progress import cli, facts, window  # noqa: E402
from progress.docs import ArtifactError, Docs, ReplanRequired  # noqa: E402
from progress.facts import FactsError  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def commit(tmp, subject, files):
    for rel, text in files.items():
        p = pathlib.Path(tmp) / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, capture_output=True, env=ENV)
    subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", subject],
                   check=True, capture_output=True, env=ENV)
    return window.git(["rev-parse", "HEAD"], tmp).strip()


BASE = "https://docs.example/docs"
ALPHA_DOC = "**Alpha's theorem.** It states alpha. And more besides."
BETA_DOC = "**Beta's theorem.** It states beta."


def bundle_docs(source_commit, modules):
    """A real reader over a bundle documenting `modules`: `{path: {name: (kind, start, end, doc)}}`."""
    data = {"schema_version": 1, "source_commit": source_commit,
            "provenance": {"toolchain": "leanprover/lean4:v4.0.0", "doc_gen": "test"},
            "modules": {path: {name: {"kind": kind, "file": path, "start": start, "end": end, "doc": doc,
                                      "url": f"{BASE}/{path[:-5]}.html#{name}"}
                               for name, (kind, start, end, doc) in decls.items()}
                        for path, decls in modules.items()}}
    raw = gzip.compress(json.dumps(data).encode())
    return Docs(base=BASE, opener=lambda url: raw)


ALPHA = """namespace TauCeti
/-- **Alpha's theorem.** It states alpha. And more besides. -/
theorem alpha : True := trivial
end TauCeti
"""

ALPHA_AND_BETA = """namespace TauCeti
/-- **Alpha's theorem.** It states alpha. And more besides. -/
theorem alpha : True := trivial

/-- **Beta's theorem.** It states beta. -/
theorem beta : True := trivial
end TauCeti
"""


def repo_with_two_prs(tmp):
    """Root, then a PR adding `alpha`, then a PR adding `beta` to the same file."""
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
    root = commit(tmp, "init", {"README.md": "x"})
    first = commit(tmp, "feat: alpha (#101)", {"TauCeti/A.lean": ALPHA})
    second = commit(tmp, "feat: beta (#102)", {"TauCeti/A.lean": ALPHA_AND_BETA})
    return root, first, second


# `alpha` occupies lines 2-3 in both versions; `beta` occupies lines 5-6 of the second.
def docs_for(commit_sha, with_beta=True, extra=None):
    decls = {"TauCeti.alpha": ("theorem", 2, 3, ALPHA_DOC)}
    if with_beta:
        decls["TauCeti.beta"] = ("theorem", 5, 6, BETA_DOC)
    return bundle_docs(commit_sha, {"TauCeti/A.lean": decls, **(extra or {})})


def refused(error, fn, *needles):
    try:
        fn()
    except error as exc:
        for needle in needles:
            assert needle in str(exc), str(exc)
        return exc
    raise AssertionError(f"expected {error.__name__}, not a report")


def test_names_kinds_and_urls_come_from_the_documentation():
    """Nothing is derived from the source text: the qualified name, the kind and the link are all
    what doc-gen4 published."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert set(by) == {"TauCeti.alpha", "TauCeti.beta"}, sorted(by)
        assert by["TauCeti.alpha"]["kind"] == "theorem"
        assert by["TauCeti.alpha"]["url"] == "https://docs.example/docs/TauCeti/A.html#TauCeti.alpha"


def test_blame_attributes_each_declaration_to_its_pull_request():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert by["TauCeti.alpha"]["pr"] == 101, by["TauCeti.alpha"]
        assert by["TauCeti.beta"]["pr"] == 102, by["TauCeti.beta"]


def test_declarations_predating_the_window_are_not_reported():
    """`alpha` was written before this window opens, so it is real but not news."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, first, second, docs=docs_for(second))
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.beta"}, sorted(names)


def test_the_pr_filter_is_honoured():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, pr_numbers=[101], docs=docs_for(second))
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, sorted(names)


def test_docstrings_come_from_the_documentation():
    """The first sentence of doc-gen4's docstring, which is what a report wants to quote."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        got = facts.collect(tmp, root, second, docs=docs_for(second))
        by = {d["name"]: d for d in got["declarations"]}
        assert by["TauCeti.alpha"]["doc"] == "**Alpha's theorem.** It states alpha.", by
        assert by["TauCeti.beta"]["doc"] == "**Beta's theorem.** It states beta."


def test_a_declaration_with_no_docstring_reports_none():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        root = commit(tmp, "init", {"README.md": "x"})
        head = commit(tmp, "feat: bare (#7)", {"TauCeti/A.lean": "theorem bare : True := trivial\n"})
        docs = bundle_docs(head, {"TauCeti/A.lean": {"bare": ("theorem", 1, 1, "")}})
        got = facts.collect(tmp, root, head, docs=docs)
        assert got["declarations"][0]["doc"] == ""


def test_a_removed_module_needs_no_documentation():
    """A file touched in the window but removed before the documented commit is not documented.
    The rest of the window is still reported."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        commit(tmp, "feat: add B (#103)", {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        fourth = commit(tmp, "remove B (#104)", {"TauCeti/B.lean": None})
        got = facts.collect(tmp, root, fourth, docs=docs_for(fourth, with_beta=False))
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, sorted(names)
        assert got["counts"]["files"] == 2, got["counts"]


def test_an_undocumented_module_refuses_a_partial_report():
    """A module present at the documented commit but absent from the bundle is incomplete
    documentation. A nonempty partial report must not advance past that module."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: B (#103)", {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        refused(ArtifactError, lambda: facts.collect(tmp, root, third, docs=docs_for(third)),
                "missing required modules", "TauCeti/B.lean")


def test_a_genuinely_empty_module_does_not_block_another_declaration():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: imports (#103)", {"TauCeti/B.lean": "import TauCeti.A\n"})
        got = facts.collect(tmp, root, third, docs=docs_for(third, extra={"TauCeti/B.lean": {}}))
        assert [d["name"] for d in got["declarations"]] == ["TauCeti.alpha", "TauCeti.beta"]
        assert got["counts"]["files"] == 2


def test_a_window_with_no_attributable_declarations_is_refused():
    """The backstop: whatever loses them, a window whose pull requests yield no declarations at all
    must not reach a writing model, which would announce that nothing happened."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        try:
            facts.collect(tmp, root, second, docs=bundle_docs(second, {"TauCeti/A.lean": {}}))
        except FactsError as exc:
            assert "2 pull request(s)" in str(exc), str(exc)
            assert "0 removed by the documented commit" in str(exc), str(exc)
        else:
            raise AssertionError("expected a refusal, not an empty window")


def test_revised_declarations_are_marked_not_new():
    """A declaration whose lines are only partly from this window existed before and was revised."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        root = commit(tmp, "init", {"README.md": "x"})
        before = "/-- Doc. -/\ntheorem t : True := by\n  trivial\n"
        first = commit(tmp, "feat: add (#1)", {"TauCeti/A.lean": before})
        after = "/-- Doc. -/\ntheorem t : True := by\n  exact trivial\n"
        second = commit(tmp, "refactor: tweak (#2)", {"TauCeti/A.lean": after})
        docs = bundle_docs(second, {"TauCeti/A.lean": {"t": ("theorem", 2, 3, "Doc.")}})
        got = facts.collect(tmp, root, second, docs=docs)
        assert got["declarations"][0]["new"] is True, "written entirely within the window"
        got2 = facts.collect(tmp, first, second, docs=docs)
        assert got2["declarations"][0]["new"] is False, "only the body line is from this window"


def test_documentation_behind_the_planned_window_refuses_to_advance():
    """A different docs build between plan and facts must not advance the planned cursor past
    pull requests whose declarations the extractor did not inspect."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        third = commit(tmp, "feat: later (#103)", {"TauCeti/B.lean": "theorem later : True := trivial\n"})
        refused(ReplanRequired, lambda: facts.collect(tmp, root, third, docs=docs_for(second)),
                second, third, "replan")


def test_documentation_from_a_foreign_history_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        refused(ReplanRequired, lambda: facts.collect(tmp, root, second, docs=docs_for("0" * 40)), "0000000")


def test_module_page_for_file():
    assert facts.module_page_for_file("TauCeti/Analysis/Fredholm/Basic.lean") == \
        "TauCeti/Analysis/Fredholm/Basic.html"
    assert facts.module_page_for_file("scripts/x.py") is None
    assert facts.module_page_for_file("TauCeti/A.txt") is None


def cli_facts(tmp, from_sha, to_sha, prs, docs):
    """`facts` on a plan pinning `docs`, as `plan --out` writes it. Returns (exit code, output path)."""
    plan_file = pathlib.Path(tmp) / "plan.json"
    out_file = pathlib.Path(tmp) / "facts.json"
    plan_file.write_text(json.dumps({"roadmap": "Algebra", "from_sha": from_sha, "to_sha": to_sha,
                                     "prs": prs, "documentation": docs.pin()}))
    rc = cli.main(["facts", "--plan", str(plan_file), "--code-dir", tmp, "--out", str(out_file)])
    return rc, out_file


def test_cli_facts_passes_the_plan_filter():
    """Regression: the CLI once dropped the filter, so a one-roadmap report was grounded in every
    roadmap's work in the range. The failure is silent -- the output merely gets bigger."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        rc, out_file = cli_facts(tmp, root, second, [101], docs_for(second))
        assert rc == 0, rc
        got = json.loads(out_file.read_text())
        names = {d["name"] for d in got["declarations"]}
        assert names == {"TauCeti.alpha"}, f"filter dropped: got {sorted(names)}"


def test_cli_facts_reports_a_refusal_as_an_error():
    """A refusal has to stop the round before a model is started: exit 1 with the reason, and no
    facts file left for anything to read."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = repo_with_two_prs(tmp)
        rc, out_file = cli_facts(tmp, root, second, [101, 102], bundle_docs(second, {"TauCeti/A.lean": {}}))
        assert rc == 1, rc
        assert not out_file.exists(), "a refused window must leave no facts file behind"


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
