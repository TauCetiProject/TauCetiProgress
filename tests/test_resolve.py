"""Tests for the `resolve` job of roadmap-workflows/progress-merge.yml.

That job decides which pull request the gate considers when CI completes. If it picks nothing, a
legitimate report is never gated again, so a failure here strands reports rather than admitting bad
ones. The script is taken from the workflow itself and run under bash with `gh` replaced by a stub
that serves canned API responses; `jq` is the real one.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "roadmap-workflows" / "progress-merge.yml"

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


# The expressions Actions substitutes into the script before bash sees it. All of them belong to
# other events, so on `workflow_run` each is the empty string. An expression not listed here fails
# the tests rather than being guessed at.
EXPRESSIONS = {
    "github.event.pull_request.draft": "",
    "github.event.pull_request.number": "",
    "inputs.pr": "",
}


def resolve_script():
    """The `run: |` block of the step `id: pick`, dedented, with its expressions substituted."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "id: pick")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    body_indent = min(len(l) - len(l.lstrip()) for l in body if l.strip())
    script = "\n".join(l[body_indent:] for l in body) + "\n"

    def substitute(m):
        expr = m.group(1).strip()
        if expr not in EXPRESSIONS:
            raise AssertionError(f"unexpected expression in the resolve script: {expr}")
        return EXPRESSIONS[expr]

    return re.sub(r"\$\{\{(.*?)\}\}", substitute, script)


# Stands in for `gh api --paginate --slurp <endpoint>`. Only the two lookups the workflow_run branch
# makes are served, each from a file; any other call, including a lookup the test expects to be
# skipped, fails, and `set -e` then fails the script.
GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
endpoint="${@: -1}"
if [ "$endpoint" = "repos/$REPO/commits/$HEAD_SHA/pulls" ] && [ -n "${FAKE_BY_COMMIT:-}" ]; then
  cat "$FAKE_BY_COMMIT"
elif [ "$endpoint" = "repos/$REPO/pulls?state=open&per_page=100&head=$HEAD_OWNER:$HEAD_BRANCH" ] \\
     && [ -n "${FAKE_BY_HEAD:-}" ]; then
  cat "$FAKE_BY_HEAD"
else
  echo "unexpected gh call: $*" >&2; exit 1
fi
"""

REPO = "TauCetiProject/TauCetiRoadmap"
SHA = "b838c399a4c489f4654e673a5d0b3f0ee2e72b74"
BRANCH = "progress/8745177-06fa4da/IntegralLattices"

# GitHub caps a pull request body at 65536 characters.
LONG_BODY = "x" * 65536


def pull(number, owner="TauCetiProject", **over):
    """A pull request as the REST API returns it, reduced to the fields the filter reads."""
    pr = {
        "number": number,
        "state": "open",
        "draft": False,
        "body": "",
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "head": {"ref": BRANCH, "sha": SHA, "repo": {"full_name": f"{owner}/TauCetiRoadmap"}},
    }
    for key, value in over.items():
        if key in ("base", "head"):
            pr[key] = {**pr[key], **value}
        else:
            pr[key] = value
    return pr


def ineligible(n, start):
    """`n` pull requests, numbered from `start`, each refused by a different filter, each with a body
    at GitHub's maximum length."""
    kinds = [
        {"draft": True},
        {"state": "closed"},
        {"base": {"ref": "release"}},
        {"base": {"repo": {"full_name": "someone/TauCetiRoadmap"}}},
        {"head": {"ref": "feature/IntegralLattices"}},
    ]
    return [pull(start + i, body=LONG_BODY, **kinds[i % len(kinds)]) for i in range(n)]


def run(by_commit=None, by_head=None, owner="TauCetiProject", branch=BRANCH, sha=SHA):
    """Run the resolver as a `workflow_run` event. `by_commit` and `by_head` are lists of pages, as
    `--paginate --slurp` returns them; None means that lookup must not be made. Returns the chosen
    `pr` output and the stub's log of gh calls."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "bin").mkdir()
        gh = tmp / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        script = tmp / "resolve.sh"
        script.write_text(resolve_script())
        env = {
            **os.environ,
            "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(tmp / "output"),
            "GH_LOG": str(tmp / "gh.log"),
            "EVENT": "workflow_run",
            "REPO": REPO,
            "OWNER": "TauCetiProject",
            "HEAD_SHA": sha,
            "HEAD_OWNER": owner,
            "HEAD_BRANCH": branch,
        }
        for name, pages in (("FAKE_BY_COMMIT", by_commit), ("FAKE_BY_HEAD", by_head)):
            if pages is not None:
                path = tmp / f"{name}.json"
                path.write_text(json.dumps(pages))
                env[name] = str(path)
        # `bash -e`, as Actions runs a `run:` block.
        proc = subprocess.run(["bash", "-e", str(script)], env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"resolve exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
        outputs = (tmp / "output").read_text().splitlines()
        picked = [l.removeprefix("pr=") for l in outputs if l.startswith("pr=")]
        assert len(picked) == 1, f"expected one pr= output, got {outputs}"
        return picked[0], (tmp / "gh.log").read_text().splitlines()


def big(pages):
    """Assert the response cannot travel as one command-line argument. Linux caps one argument at
    128 KiB and macOS caps all of them together at 1 MiB, so past 1 MiB a resolver that passes the
    response as an argument fails on both."""
    size = len(json.dumps(pages))
    assert size > 1024 * 1024, f"fixture is only {size} bytes"
    return pages


def test_a_large_commit_lookup_still_finds_the_report():
    """A response too large to be an argument, with the report on the second page after a full page
    of lower-numbered pull requests that each fail one filter. Passed to jq with `--argjson`, jq never
    started and the report was never gated."""
    pages = big([ineligible(30, 100), [pull(597)]])
    picked, _ = run(by_commit=pages, by_head=[[pull(597)]])
    assert picked == "597", picked


def test_a_large_head_lookup_still_finds_the_fork_report():
    """A fork commit has no pull requests in the base repository, so only the head lookup can find
    it; that response too may be larger than one argument."""
    pages = big([ineligible(29, 100) + [pull(597, owner="ldct")]])
    picked, _ = run(by_commit=[[]], by_head=pages, owner="ldct")
    assert picked == "597", picked


def test_a_fork_report_must_be_at_the_built_commit():
    """The head lookup names a branch, which can have moved since CI built it."""
    picked, _ = run(by_commit=[[]], by_head=[[pull(597, owner="ldct", head={"sha": "0" * 40})]],
                    owner="ldct")
    assert picked == "", picked


def test_the_lowest_eligible_number_wins():
    """Repeated runs over the same candidates must agree."""
    picked, _ = run(by_commit=[[pull(612), pull(597)]], by_head=[[pull(605)]])
    assert picked == "597", picked


def test_an_unexpected_branch_shape_skips_the_head_lookup():
    """The owner and branch come from the event payload, so they reach the API only in the shape of
    a login and a progress branch."""
    picked, calls = run(by_commit=[[]], branch=BRANCH + "&state=closed", owner="ldct")
    assert picked == "", picked
    assert len(calls) == 1 and "/commits/" in calls[0], calls
    picked, calls = run(by_commit=[[]], owner="ldct/../x")
    assert picked == "", picked
    assert len(calls) == 1, calls


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
