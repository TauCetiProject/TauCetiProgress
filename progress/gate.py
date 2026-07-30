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
byte-exact, that the head being merged is the head that was validated. What it cannot prove is that
the prose is true. That limit is accepted deliberately and documented in README.md; the path
restriction is what bounds the damage to two markdown files in one directory.
"""

import json
import re

from . import files

# Only these two basenames, and only inside one area directory.
ALLOWED_BASENAMES = ("STATUS.md", "PROGRESS.md")
BRANCH_RE = re.compile(r"\Aprogress/[0-9a-f]{7}-[0-9a-f]{7}/([A-Za-z0-9]+)\Z")
PATH_RE = re.compile(r"\A(?:TauCetiRoadmap|Completed)/([A-Za-z0-9]+)/(STATUS\.md|PROGRESS\.md)\Z")

# A blob mode other than a regular file means a symlink (120000), a gitlink/submodule (160000), or
# an executable. A symlink named STATUS.md pointing at something else is the classic way to make a
# path-restricted gate write outside its restriction, so modes are checked, not assumed.
REGULAR_MODES = {"100644"}


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
        _refuse(f"author id {author_id} is not in the allowlist")

    branch = head.get("ref") or ""
    m = BRANCH_RE.match(branch)
    if not m:
        _refuse(f"head branch {branch!r} is not a progress branch")
    return {"area": m.group(1), "head_sha": head.get("sha") or ""}


def check_files(changed_files, area):
    """Diff shape: exactly the two generated files, for exactly `area`, as regular files.

    Requiring *both* is deliberate. A `STATUS.md`-only update would move the snapshot forward while
    the window's prose was never written, and because the reporting cursor is the last `PROGRESS.md`
    section, no later run could reconstruct the gap. "Either file" is unsafe; "both files" is not.
    """
    if not changed_files:
        _refuse("no files changed")
    seen = {}
    for f in changed_files:
        path = f.get("filename") or ""
        m = PATH_RE.match(path)
        if not m:
            _refuse(f"path {path!r} is not an allowed generated file")
        if m.group(1) != area:
            _refuse(f"path {path!r} is not in the {area} directory")
        status = f.get("status")
        if status not in ("added", "modified"):
            _refuse(f"path {path!r} has status {status!r}; only added or modified are allowed")
        # `previous_filename` present means a rename, which could move a file out of the area.
        if f.get("previous_filename"):
            _refuse(f"path {path!r} is a rename from {f['previous_filename']!r}")
        seen[m.group(2)] = f
    missing = [name for name in ALLOWED_BASENAMES if name not in seen]
    if missing:
        _refuse(f"missing required file(s): {', '.join(missing)}; an update must change both")
    return seen


def check_modes(tree_entries):
    """Every changed blob must be an ordinary file: no symlink, submodule, or mode flip.

    `tree_entries` is `[{path, mode, type}]` for the two paths at the head commit. The TauCeti build
    workflow rejects symlinks for the same reason.
    """
    for entry in tree_entries:
        if entry.get("type") != "blob":
            _refuse(f"{entry.get('path')!r} is a {entry.get('type')!r}, not a file")
        if entry.get("mode") not in REGULAR_MODES:
            _refuse(f"{entry.get('path')!r} has mode {entry.get('mode')!r}, not a regular file")
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


def check_build(check_runs, head_sha, required="build"):
    """The `build` check must be green on the exact head being merged.

    The App bypasses required status checks, so this is asserted rather than relied upon -- the same
    reasoning as `decide_merge` in TauCetiReview, which refuses when `ci_build` is not success.
    """
    for run in check_runs:
        if (run.get("name") or "") != required:
            continue
        if (run.get("head_sha") or head_sha) != head_sha:
            continue
        concl = (run.get("conclusion") or "").upper()
        if concl in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return concl
        _refuse(f"{required} concluded {concl or 'pending'} on {head_sha[:7]}")
    _refuse(f"{required} has not reported on {head_sha[:7]}")


def decide(pr, changed_files, tree_entries, old_status, new_status_bytes, old_progress,
           new_progress_bytes, check_runs, allowed_user_ids, base_repo,
           current_main_cursor=None):
    """Run the whole gate. Returns `{"area", "head_sha", "section"}` or raises `Refused`.

    `current_main_cursor` is the area's cursor read from **freshly fetched `main`**, not from the
    pull request's stale base. Passing it closes the window where `main` moved on (another report
    merged) after this pull request was opened.
    """
    prov = check_provenance(pr, allowed_user_ids, base_repo)
    area, head_sha = prov["area"], prov["head_sha"]
    if not head_sha:
        _refuse("pull request has no head sha")

    check_files(changed_files, area)
    check_modes(tree_entries)
    section = check_content(
        area, old_status, new_status_bytes, old_progress, new_progress_bytes,
        expect_from_sha=current_main_cursor,
    )
    check_build(check_runs, head_sha)
    return {"area": area, "head_sha": head_sha, "section": section}


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
        )
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"ALLOWED: {summary(result)}")
    return 0
