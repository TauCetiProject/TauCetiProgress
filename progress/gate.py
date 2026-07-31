"""The merge gate: decide whether a progress pull request may be merged, and refuse otherwise.

This is the security-critical module of the project, so it is worth being explicit about why.

TauCetiRoadmap's `main` ruleset requires an approving review from a code owner plus the `build`
check. A ruleset cannot scope that requirement to a path, so the only way a generated report can land
unattended is the roadmap App's `always` bypass -- and a bypass actor bypasses the *whole* ruleset,
including the status checks. Anyone may open a pull request against that repository, including from a
fork. Therefore:

    the checks in this file are the entire gate.

Everything is decided from data the caller fetched via the API; nothing here checks out or executes
pull-request content, and the caller must not mint a write token until `decide` has returned an
allow. The checks run cheapest-and-most-decisive first, so a hostile pull request is rejected on
provenance long before any content is parsed.

What this gate proves is *shape*: which paths changed, that the cursor continues, that the append is
byte-exact, and that the bytes validated are the bytes that will land (the head is pinned, and it
already contains current `main`). What it cannot prove is that the prose is true. That limit is
accepted deliberately and documented in README.md.

The path restriction bounds the damage to two markdown files in one directory -- and to one Zulip
message, since every merged section is announced automatically. That second sink is part of the blast
radius and is named here so it is not overlooked.
"""

import json
import re

from . import files

# Only these two basenames, and only inside ONE area directory.
ALLOWED_BASENAMES = ("STATUS.md", "PROGRESS.md")
BRANCH_RE = re.compile(r"\Aprogress/([0-9a-f]{7})-([0-9a-f]{7})/([A-Za-z0-9]+)\Z")
# Group 1 is the parent (an area lives under one or the other, never both), group 2 the area name,
# group 3 the basename. The parent is captured because an area name can legitimately exist under
# BOTH parents -- `Completed/` is where a finished roadmap is archived -- so matching on the area and
# basename alone would let one update write to two directories at once.
PATH_RE = re.compile(r"\A(TauCetiRoadmap|Completed)/([A-Za-z0-9]+)/(STATUS\.md|PROGRESS\.md)\Z")

# A blob mode other than a regular file means a symlink (120000), a gitlink/submodule (160000), or
# an executable. A symlink named STATUS.md pointing at something else is the classic way to make a
# path-restricted gate write outside its restriction, so modes are checked, not assumed.
REGULAR_MODES = {"100644"}

# The App permitted to report the required check. `build` in TauCetiRoadmap is a GitHub Actions
# check-run (app id 15368); any repository WRITER can POST a commit status or create a check-run
# under an arbitrary name, so an unauthenticated "something called build says success" is not
# evidence. Legacy commit statuses are not accepted here at all -- the roadmap repo publishes none.
GITHUB_ACTIONS_APP_ID = 15368

# A considered refusal is exit 3, distinct from both an allow (0) and a crash (anything else). The
# workflow relies on that distinction: a refusal is a normal outcome that leaves the pull request for
# a human, while an unexpected failure must go red rather than reading as a quiet "did not merge".
#
# Not 2: `argparse` exits 2 on a usage error, so a mistyped invocation would have been reported as a
# considered refusal. The workflow additionally requires the output to start with `REFUSED:`, so the
# two signals have to agree.
EX_REFUSED = 3


class Refused(Exception):
    """The pull request must not be merged. The message is posted for a human to read."""


def _refuse(reason):
    raise Refused(reason)


def check_provenance(pr, allowed_user_ids, base_repo, base_branch="main"):
    """Provenance first: is this pull request even a candidate?

    `pr` is the GitHub pull-request object. `allowed_user_ids` is a set of **numeric** user ids.
    Logins are renameable and a renamed login could be re-registered by someone else, so identity is
    always the immutable id.
    """
    if pr.get("state") != "open":
        _refuse(f"pull request is {pr.get('state')}, not open")
    if pr.get("draft"):
        _refuse("pull request is a draft")
    base = pr.get("base") or {}
    if (base.get("ref") or "") != base_branch:
        _refuse(f"base branch is {base.get('ref')!r}, expected {base_branch!r}")
    if ((base.get("repo") or {}).get("full_name") or "") != base_repo:
        _refuse(f"base repository is {(base.get('repo') or {}).get('full_name')!r}")

    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    if head_repo != base_repo:
        # A fork PR can carry any content and any author; those go to human review like every other
        # roadmap contribution.
        _refuse(f"head repository is {head_repo!r}, not {base_repo!r} (forks are never auto-merged)")

    author_id = ((pr.get("user") or {}).get("id"))
    if author_id not in set(allowed_user_ids):
        # Name the file: this refusal is posted as a comment, and an operator whose round produced a
        # report they are not permitted to land needs to know where the list is, not just that they
        # failed. `tauceti-progress due` checks the same list before a round starts, so reaching this
        # point at all means the two disagree, usually a stale local checkout.
        _refuse(
            f"author id {author_id} is not listed in .github/progress-publishers.txt on the base "
            f"branch, so this report cannot merge unattended"
        )

    branch = head.get("ref") or ""
    m = BRANCH_RE.match(branch)
    if not m:
        _refuse(f"head branch {branch!r} is not a progress branch")
    return {
        "area": m.group(3),
        "from_prefix": m.group(1),
        "to_prefix": m.group(2),
        "head_sha": head.get("sha") or "",
    }


def check_files(changed_files, area):
    """Diff shape: exactly the two generated files, in exactly ONE `area` directory, as regular files.

    Requiring *both* is deliberate. A `STATUS.md`-only update would move the snapshot forward while
    the window's prose was never written, and because the reporting cursor is the last `PROGRESS.md`
    section, no later run could reconstruct the gap. "Either file" is unsafe; "both files" is not.

    Requiring exactly *two* files in *one* parent is equally deliberate, and was a real hole: keying
    only on the basename let a pull request change four paths -- `TauCetiRoadmap/<area>/{STATUS,
    PROGRESS}.md` **and** `Completed/<area>/{STATUS,PROGRESS}.md` -- and pass, because both basenames
    were present and every path matched the pattern. The content validators then inspected only one
    pair, so the other two would have merged unexamined.

    Returns `{basename: file}` plus the resolved parent directory.
    """
    if not changed_files:
        _refuse("no files changed")

    # First pass: every path must be an allowed generated file in the branch's own area, added or
    # modified, never renamed in.
    parsed = []
    for f in changed_files:
        path = f.get("filename") or ""
        m = PATH_RE.match(path)
        if not m:
            _refuse(f"path {path!r} is not an allowed generated file")
        parent, path_area, basename = m.group(1), m.group(2), m.group(3)
        if path_area != area:
            _refuse(f"path {path!r} is not in the {area} directory")
        status = f.get("status")
        if status not in ("added", "modified"):
            _refuse(f"path {path!r} has status {status!r}; only added or modified are allowed")
        # `previous_filename` present means a rename, which could move a file out of the area.
        if f.get("previous_filename"):
            _refuse(f"path {path!r} is a rename from {f['previous_filename']!r}")
        parsed.append((parent, basename, f))

    # Second pass, in order of how much the message tells a reader: one directory, then no
    # duplicates, then both files present.
    parents = {parent for parent, _, _ in parsed}
    if len(parents) != 1:
        _refuse(f"update spans {sorted(parents)}; it must change one directory, not several")
    seen = {}
    for _, basename, f in parsed:
        if basename in seen:
            _refuse(f"{basename} appears twice; an update changes each file once")
        seen[basename] = f
    missing = [name for name in ALLOWED_BASENAMES if name not in seen]
    if missing:
        _refuse(f"missing required file(s): {', '.join(missing)}; an update must change both")
    return seen, parents.pop()


def check_modes(tree_entries, required_paths):
    """Every changed blob must be an ordinary file: no symlink, submodule, or mode flip.

    `tree_entries` is `[{path, mode, type}]` read from the git TREE api at the head commit, which
    reports true modes (`100644`, `100755`, `120000`, `160000`). The TauCeti build workflow rejects
    symlinks for the same reason.

    `required_paths` must be supplied and every one of them must have an entry. Iterating only over
    whatever was handed in was a fail-OPEN hole: an empty list -- which `collect.py` produced whenever
    a per-path fetch failed -- passed vacuously, and the symlink defence is precisely the check that
    must never fail open.
    """
    by_path = {e.get("path"): e for e in tree_entries}
    for path in sorted(required_paths):
        entry = by_path.get(path)
        if entry is None:
            _refuse(f"no tree entry for {path!r}; cannot confirm it is a regular file")
        if entry.get("type") != "blob":
            _refuse(f"{path!r} is a {entry.get('type')!r}, not a file")
        if entry.get("mode") not in REGULAR_MODES:
            _refuse(f"{path!r} has mode {entry.get('mode')!r}, not a regular file")
    extra = sorted(set(by_path) - set(required_paths))
    if extra:
        _refuse(f"unexpected tree entries: {extra}")
    return True


def check_content(area, old_status, new_status_bytes, old_progress, new_progress_bytes,
                  expect_from_sha=None):
    """Content: both files parse, agree, and the log grew only at the end.

    Bytes in, so invalid UTF-8 is caught here rather than raising something unhelpful later.
    """
    new_status = files.check_utf8("STATUS.md", new_status_bytes)
    new_progress = files.check_utf8("PROGRESS.md", new_progress_bytes)
    try:
        return files.validate_update(
            area, old_status, new_status, old_progress, new_progress,
            expect_from_sha=expect_from_sha,
        )
    except files.FormatError as exc:
        _refuse(str(exc))


def check_up_to_date(compare_status, behind_by, head_sha, main_sha):
    """The head must already contain current `main`.

    This is what makes the merge safe to reason about. If the head is BEHIND main, merging it is a
    three-way merge, and the resulting bytes are a combination of the head and whatever landed on
    main since -- not the bytes that were validated. Requiring `ahead` with `behind_by == 0` means
    the head's tree IS the post-merge tree for the paths in question, so validating the head's blobs
    validates exactly what will land.

    A head that has fallen behind is not an error; the worker simply rebuilds the report against the
    newer main. Refusing is the correct outcome, not a failure.
    """
    if behind_by is None or compare_status is None:
        _refuse("could not determine whether the head contains current main")
    if int(behind_by) != 0 or compare_status != "ahead":
        _refuse(
            f"head {head_sha[:7]} is {compare_status} main {main_sha[:7]} and is behind by "
            f"{behind_by}; rebuild the report on current main so the merged bytes are the "
            f"validated bytes"
        )
    return True


def check_build(check_runs, head_sha, required="build", app_id=GITHUB_ACTIONS_APP_ID):
    """The `build` check must be a completed success, from the expected App, on the exact head.

    The merging App bypasses required status checks, so this is asserted rather than relied upon --
    the same reasoning as `decide_merge` in TauCetiReview.

    Three things this is strict about, each a way the looser version could be fooled:

    * **Provenance.** Any repository writer can create a check-run or POST a commit status under any
      name, so a result is only evidence if it came from the App that actually runs CI. Legacy commit
      statuses are refused outright -- the roadmap repo publishes none, and accepting them would open
      exactly that forgery route.
    * **Literal success.** `neutral` and `skipped` count as passing for ordinary branch protection,
      which means a workflow that skipped the build entirely would have satisfied this.
    * **No contradictions.** Every matching entry must agree; a success listed ahead of a failure
      used to win because the first match returned.
    """
    matching = [r for r in check_runs if (r.get("name") or "") == required]
    if not matching:
        _refuse(f"{required} has not reported on {head_sha[:7]}")
    for run in matching:
        # Every field is REQUIRED. Defaulting a missing field to the acceptable value meant a bare
        # {"name": "build", "conclusion": "SUCCESS"} passed: no app to check, status assumed
        # completed, head assumed to match. An absent field is unknown provenance, which is exactly
        # the thing this refuses.
        if run.get("source") != "check_run":
            _refuse(f"{required} on {head_sha[:7]} came from {run.get('source')!r}, not a check run")
        if run.get("head_sha") != head_sha:
            _refuse(f"{required} names head {run.get('head_sha')!r}, not {head_sha}")
        got_app = run.get("app_id")
        # Compared as an integer, not coerced into one: `int()` accepted "15368" and 15368.9 alike.
        if isinstance(got_app, bool) or not isinstance(got_app, int) or got_app != app_id:
            _refuse(f"{required} on {head_sha[:7]} was reported by app {got_app!r}, not {app_id}")
        if run.get("status") != "completed":
            _refuse(f"{required} is {run.get('status')!r} on {head_sha[:7]}, not completed")
        # Compared exactly, not case-folded: GitHub emits lowercase conclusions, so anything else is
        # not something GitHub wrote.
        if run.get("conclusion") != "success":
            _refuse(f"{required} concluded {run.get('conclusion')!r} on {head_sha[:7]}")
    return f"{len(matching)}x success"


def check_baseline_paths(old_paths, parent, area):
    """The append-only baseline must come from the directory the diff actually touches.

    An area can exist under both `TauCetiRoadmap/` and `Completed/`. A collector that probed a fixed
    order would hand a `Completed/` update the ACTIVE log as its baseline, and a wholesale
    replacement of the archived log would then look like a valid append. The paths the baseline was
    read from are therefore recorded and checked here rather than trusted.
    """
    if not old_paths:
        # No baseline at all is legitimate only for an area's first report; validate_update enforces
        # the rest (a first report starts from a fresh preamble).
        return True
    for name in ALLOWED_BASENAMES:
        got = old_paths.get(name)
        want = f"{parent}/{area}/{name}"
        if got != want:
            _refuse(f"baseline for {name} was read from {got!r}, expected {want!r}")
    return True


def decide(pr, changed_files, tree_entries, old_status, new_status_bytes, old_progress,
           new_progress_bytes, check_runs, allowed_user_ids, base_repo,
           current_main_cursor=None, compare_status=None, behind_by=None, main_sha="",
           old_paths=None):
    """Run the whole gate. Returns `{"area", "head_sha", "section"}` or raises `Refused`.

    `current_main_cursor` is the area's cursor read from **freshly fetched `main`**, not from the
    pull request's stale base. Passing it closes the window where `main` moved on (another report
    merged) after this pull request was opened.
    """
    prov = check_provenance(pr, allowed_user_ids, base_repo)
    area, head_sha = prov["area"], prov["head_sha"]
    if not head_sha:
        _refuse("pull request has no head sha")

    check_up_to_date(compare_status, behind_by, head_sha, main_sha)
    seen, parent = check_files(changed_files, area)
    check_baseline_paths(old_paths or {}, parent, area)
    check_modes(tree_entries, [f"{parent}/{area}/{name}" for name in ALLOWED_BASENAMES])
    section = check_content(
        area, old_status, new_status_bytes, old_progress, new_progress_bytes,
        expect_from_sha=current_main_cursor,
    )
    check_build(check_runs, head_sha)

    # The branch name encodes the window it reports, and `apply` derives it from the same plan that
    # produced the header. Requiring them to agree binds the branch to its content, so a branch
    # cannot be reused to carry a different window's update. It does not authenticate WHO produced
    # the head -- any repository writer can push to an open branch, and `pr.user.id` still names the
    # opener -- so restricting who may push `progress/*` remains a repository-level control; see
    # roadmap-workflows/README.md.
    if not section["from_sha"].startswith(prov["from_prefix"]):
        _refuse(
            f"branch says the window starts at {prov['from_prefix']} but the section says "
            f"{section['from_sha'][:7]}"
        )
    if not section["to_sha"].startswith(prov["to_prefix"]):
        _refuse(
            f"branch says the window ends at {prov['to_prefix']} but the section says "
            f"{section['to_sha'][:7]}"
        )
    return {"area": area, "head_sha": head_sha, "main_sha": main_sha, "section": section}


def summary(result):
    return (
        f"{result['area']}: window {result['section']['from_sha'][:7]}.."
        f"{result['section']['to_sha'][:7]}, {len(result['section']['prs'])} PR(s), "
        f"head {result['head_sha'][:7]}"
    )


def main(argv=None):
    """CLI used by the reusable workflow: reads a JSON bundle, prints a verdict, exits 0 or 1.

    The workflow fetches every input with the read-only default token and hands them over as one
    file, so this process needs no credentials at all.
    """
    import argparse
    import pathlib
    import sys

    ap = argparse.ArgumentParser(description="Decide whether a progress PR may be merged.")
    ap.add_argument("--bundle", required=True, help="JSON file with the fetched pull-request data")
    args = ap.parse_args(argv)

    data = json.loads(pathlib.Path(args.bundle).read_text(encoding="utf-8"))
    try:
        result = decide(
            pr=data["pr"],
            changed_files=data["changed_files"],
            tree_entries=data.get("tree_entries") or [],
            old_status=data.get("old_status"),
            new_status_bytes=data["new_status"].encode("utf-8", "surrogateescape"),
            old_progress=data.get("old_progress"),
            new_progress_bytes=data["new_progress"].encode("utf-8", "surrogateescape"),
            check_runs=data.get("check_runs") or [],
            allowed_user_ids=data["allowed_user_ids"],
            base_repo=data["base_repo"],
            current_main_cursor=data.get("current_main_cursor"),
            compare_status=data.get("compare_status"),
            behind_by=data.get("behind_by"),
            main_sha=data.get("main_sha") or "",
            old_paths=data.get("old_paths") or {},
        )
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return EX_REFUSED
    print(f"ALLOWED: {summary(result)}")
    return 0
