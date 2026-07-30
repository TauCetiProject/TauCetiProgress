"""Tests for factual extraction, and for the area filter the CLI must apply.

The filter has its own test because getting it wrong is silent: `facts` still produces a large,
plausible-looking result, and the report is then grounded in every roadmap's work rather than the one
it claims to describe.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import cli, facts, window  # noqa: E402

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
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, capture_output=True, env=ENV)
    subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", subject],
                   check=True, capture_output=True, env=ENV)
    return window.git(["rev-parse", "HEAD"], tmp).strip()


def make_repo(tmp):
    """A mainline with three PRs, each adding one documented theorem in its own area."""
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
    root = commit(tmp, "init", {"README.md": "x"})
    a = commit(tmp, "feat: alpha (#101)", {
        "TauCeti/Algebra/A.lean": "/-- **Alpha's theorem.** It states alpha. -/\ntheorem alpha : True := trivial\n"})
    b = commit(tmp, "feat: beta (#102)", {
        "TauCeti/Analysis/B.lean": "/-- **Beta's theorem.** It states beta. -/\ntheorem beta : True := trivial\n"})
    c = commit(tmp, "feat: gamma (#103)", {
        "TauCeti/Topology/C.lean": "/-- Gamma is a helper. -/\ntheorem gamma : True := trivial\n"})
    return root, a, b, c


def test_collect_attributes_declarations_to_their_pr():
    with tempfile.TemporaryDirectory() as tmp:
        root, a, b, c = make_repo(tmp)
        got = facts.collect(tmp, root, c)
        by_name = {d["name"]: d for d in got["declarations"]}
        assert set(by_name) == {"alpha", "beta", "gamma"}, sorted(by_name)
        assert by_name["alpha"]["pr"] == 101
        assert by_name["beta"]["pr"] == 102
        assert by_name["gamma"]["pr"] == 103
        assert by_name["alpha"]["doc"].startswith("**Alpha's theorem.**")
        assert by_name["alpha"]["file"] == "TauCeti/Algebra/A.lean"
        assert got["counts"]["prs"] == 3
        assert got["counts"]["documented"] == 3


def test_collect_honours_the_pr_filter():
    """This is the area filter: only the named PRs' work may appear."""
    with tempfile.TemporaryDirectory() as tmp:
        root, a, b, c = make_repo(tmp)
        got = facts.collect(tmp, root, c, pr_numbers=[101, 103])
        names = {d["name"] for d in got["declarations"]}
        assert names == {"alpha", "gamma"}, sorted(names)
        assert got["counts"]["prs"] == 2
        assert all(p["number"] in (101, 103) for p in got["prs"])


def test_cli_facts_passes_the_plan_filter():
    """Regression: the CLI once dropped the filter, so a one-roadmap report was grounded in every
    roadmap's work in the commit range. The failure is silent -- the output merely gets bigger."""
    with tempfile.TemporaryDirectory() as tmp:
        root, a, b, c = make_repo(tmp)
        plan = {"roadmap": "Algebra", "from_sha": root, "to_sha": c, "prs": [101]}
        plan_file = pathlib.Path(tmp) / "plan.json"
        out_file = pathlib.Path(tmp) / "facts.json"
        plan_file.write_text(json.dumps(plan))
        rc = cli.main(["facts", "--plan", str(plan_file), "--code-dir", tmp, "--out", str(out_file)])
        assert rc == 0, rc
        got = json.loads(out_file.read_text())
        names = {d["name"] for d in got["declarations"]}
        assert names == {"alpha"}, f"filter dropped: got {sorted(names)}"


def test_declarations_added_by_ignores_preexisting():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        commit(tmp, "init", {"TauCeti/A.lean": "theorem old : True := trivial\n"})
        second = commit(tmp, "feat: more (#7)", {
            "TauCeti/A.lean": "theorem old : True := trivial\ntheorem fresh : True := trivial\n"})
        added = facts.declarations_added_by(tmp, second)
        assert set(added) == {"fresh"}, sorted(added)


def test_non_lean_changes_contribute_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True, capture_output=True)
        root = commit(tmp, "init", {"README.md": "x"})
        doc = commit(tmp, "doc: tweak (#9)", {"README.md": "y", "scripts/x.py": "print(1)"})
        got = facts.collect(tmp, root, doc)
        assert got["declarations"] == [], got["declarations"]
        assert got["counts"]["prs"] == 1


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
