#!/usr/bin/env python3
"""Fetch everything the merge gate needs into one JSON bundle, using only a read-only token.

Separating collection from decision keeps `progress.gate` a pure function of data, which is what
makes it testable against several dozen hostile inputs. This script does the untrusted I/O.

EVERYTHING IS PINNED TO TWO IMMUTABLE SHAs. That is the whole design, and it was the original
version's blocker. `GET /pulls/{n}/files` reports whatever the head is *at the moment of the call*,
with no way to pin it, so a collector that read the pull request, then its files, then its tree,
could observe three different commits: force-push a benign `B`, let the file list be read from `B`,
restore `A`, and the gate would validate `B` while the merge took `A` -- whose diff was never
inspected. `--match-head-commit` does not help; it only pins the head between validation and merge.

So the head SHA and the current `main` SHA are each read ONCE, and every later request names one of
them explicitly:

* `GET /compare/{main_sha}...{head_sha}` gives the changed paths and their blob SHAs, computed
  between exactly those two commits.
* The comparison must be `ahead` with `behind_by == 0`. That means the head already contains current
  `main`, so merging it is a fast-forward of content: the post-merge bytes are the head's bytes,
  which are the bytes validated. A head *behind* main would be squashed by a three-way merge whose
  result is not what was checked, so it is refused rather than merged.
* Blobs are fetched by the SHA the comparison reported.
* Modes come from `GET /git/trees/{head_sha}?recursive=1`, which reports true git modes; the contents
  api reports only a coarse type, may dereference symlinks, and gives no mode at all.
* The previous contents of the two files are read at `main_sha`, not from a working checkout that
  could have drifted.

Run as: collect.py --repo O/R --pr N --out bundle.json
"""

import argparse
import base64
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from progress import files, gate, publisher  # noqa: E402

# `compare` returns at most 300 files. More than that cannot be a progress report, and a truncated
# list could HIDE a path from the gate, so anything approaching the limit is refused outright rather
# than partially inspected.
MAX_COMPARE_FILES = 300


class CollectError(SystemExit):
    """Collection could not produce a trustworthy bundle. Always fatal: the gate must never run on
    data that might be incomplete."""


def gh_api(path):
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CollectError(f"gh api {path} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def gh_api_paged(path):
    """Every page of a list endpoint, flattened.

    `gh api --paginate` emits one JSON array per page, concatenated (`[...][...]`). Splicing that
    back together by replacing the literal `][` is wrong: the sequence can occur inside a *string
    value* -- a `patch` field carries the diff, and generated prose may contain brackets -- and the
    replacement silently rewrites the data rather than failing. Decode the documents properly
    instead, which is exact and does not depend on the `gh` version having `--slurp`.
    """
    proc = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CollectError(f"gh api --paginate {path} failed: {proc.stderr.strip()}")
    text = proc.stdout.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    out, idx = [], 0
    while idx < len(text):
        try:
            page, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise CollectError(f"could not parse paginated output of {path}: {exc}") from exc
        if not isinstance(page, list):
            raise CollectError(f"{path} returned a {type(page).__name__}, expected a list")
        out.extend(page)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return out


def gh_api_paged_field(path, field):
    """Every page of an endpoint that wraps its list in an object, flattened.

    `/check-runs` answers `{"total_count": N, "check_runs": [...]}` rather than a bare array, so the
    pages are concatenated JSON *objects*. Same decoding discipline as `gh_api_paged`; only the
    unwrapping differs.
    """
    proc = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CollectError(f"gh api --paginate {path} failed: {proc.stderr.strip()}")
    text = proc.stdout.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    out, idx = [], 0
    while idx < len(text):
        try:
            page, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise CollectError(f"could not parse paginated output of {path}: {exc}") from exc
        if not isinstance(page, dict):
            raise CollectError(f"{path} returned a {type(page).__name__}, expected an object")
        out.extend(page.get(field) or [])
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return out


def blob_text(repo, sha):
    """A blob's contents, by SHA. Empty string when there is no SHA."""
    if not sha:
        return ""
    obj = gh_api(f"repos/{repo}/git/blobs/{sha}")
    if obj.get("encoding") == "base64":
        raw = base64.b64decode(obj.get("content") or "")
        # Decode leniently; the gate re-checks UTF-8 validity and refuses if it is broken.
        return raw.decode("utf-8", "surrogateescape")
    return obj.get("content") or ""


def file_at(repo, ref, path):
    """A file's text at an exact ref, or None when it does not exist there."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content,.encoding"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        if "Not Found" in (proc.stderr or ""):
            return None
        raise CollectError(f"reading {path} at {ref[:7]} failed: {proc.stderr.strip()}")
    lines = proc.stdout.strip().splitlines()
    if len(lines) < 2:
        return None
    content, encoding = "\n".join(lines[:-1]), lines[-1]
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", "surrogateescape")
    return content


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--base-branch", default="main")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    # ----- the two anchors, read once ----------------------------------------------------------
    pr = gh_api(f"repos/{args.repo}/pulls/{args.pr}")
    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    if not head_sha:
        raise CollectError("pull request has no head sha")
    main_sha = gh_api(f"repos/{args.repo}/commits/{args.base_branch}").get("sha") or ""
    if not main_sha:
        raise CollectError(f"could not resolve {args.base_branch}")

    # The allowlist, read from the base commit rather than the head. Reading it at the head would let
    # a pull request add its own author and merge itself; reading it here means changing it is an
    # ordinary roadmap pull request against a path CODEOWNERS assigns to the core team.
    #
    # Both failures below are fatal rather than a refusal. A missing or malformed allowlist is a
    # misconfiguration of the repository, and the run going red is the signal; refusing quietly would
    # look exactly like "no reports were due", which is the failure mode hardest to notice.
    publishers_text = file_at(args.repo, main_sha, publisher.PUBLISHERS_PATH)
    if publishers_text is None:
        raise CollectError(
            f"{publisher.PUBLISHERS_PATH} does not exist at {main_sha[:7]}; without it no author can "
            f"be authorised and every report would be refused"
        )
    try:
        allowed_user_ids = sorted(publisher.parse_publishers(publishers_text))
    except ValueError as exc:
        raise CollectError(str(exc))

    # The area comes from the branch and is validated by the gate's own pattern. Reading it here with
    # the gate's regex keeps the two from disagreeing.
    branch = (pr.get("head") or {}).get("ref") or ""
    m = gate.BRANCH_RE.match(branch)
    area = m.group(3) if m else ""

    # ----- the diff, between exactly those two commits -----------------------------------------
    cmp_data = gh_api(f"repos/{args.repo}/compare/{main_sha}...{head_sha}?per_page={MAX_COMPARE_FILES}")
    changed = cmp_data.get("files") or []
    if len(changed) >= MAX_COMPARE_FILES:
        raise CollectError(
            f"{len(changed)} files changed, at or over the {MAX_COMPARE_FILES}-file comparison "
            f"limit; the list may be truncated, so it cannot be trusted"
        )
    behind = cmp_data.get("behind_by")
    status = cmp_data.get("status")

    tree = gh_api(f"repos/{args.repo}/git/trees/{head_sha}?recursive=1")
    if tree.get("truncated"):
        raise CollectError("the head tree was truncated; modes cannot be confirmed")
    wanted = {f.get("filename") or "" for f in changed if gate.PATH_RE.match(f.get("filename") or "")}
    tree_entries = [
        {"path": e.get("path"), "mode": e.get("mode"), "type": e.get("type"), "sha": e.get("sha")}
        for e in (tree.get("tree") or [])
        if e.get("path") in wanted
    ]

    by_path = {}
    for f in changed:
        path = f.get("filename") or ""
        if gate.PATH_RE.match(path):
            by_path[path] = f

    def blob_for(basename):
        for path, f in by_path.items():
            if path.endswith("/" + basename):
                return blob_text(args.repo, f.get("sha"))
        return ""

    new_status = blob_for("STATUS.md")
    new_progress = blob_for("PROGRESS.md")

    # ----- the previous contents, at main_sha, from the parent THIS DIFF TOUCHES ---------------
    #
    # The parent is derived from the changed paths, never probed in a fixed order. Probing
    # `TauCetiRoadmap/` first was a real hole: an area can exist under both parents, so a pull request
    # changing `Completed/<area>/` would be handed the ACTIVE log as its append-only baseline, and a
    # wholesale replacement of the archived log then looked like a valid append.
    old_status = old_progress = None
    old_paths = {}
    current_cursor = None
    parents = {gate.PATH_RE.match(p).group(1) for p in by_path}
    if len(parents) > 1:
        # The gate refuses this too, but collecting a baseline would mean choosing one arbitrarily.
        raise CollectError(f"the diff spans {sorted(parents)}; it must touch one directory")
    if area and parents:
        parent = parents.pop()
        old_paths = {
            "STATUS.md": f"{parent}/{area}/STATUS.md",
            "PROGRESS.md": f"{parent}/{area}/PROGRESS.md",
        }
        old_status = file_at(args.repo, main_sha, old_paths["STATUS.md"])
        old_progress = file_at(args.repo, main_sha, old_paths["PROGRESS.md"])
        if old_progress:
            try:
                current_cursor = files.cursor(old_progress)
            except files.FormatError:
                # An unparseable log on main is a real problem, but saying so is the gate's job.
                current_cursor = None

    # Only check-runs, and only from the head we pinned. Commit statuses are deliberately NOT
    # collected: any repository writer can POST one under any context, so they are not evidence, and
    # the gate refuses them if they somehow appear.
    check_runs = []
    runs = gh_api_paged_field(f"repos/{args.repo}/commits/{head_sha}/check-runs?per_page=100",
                              "check_runs")
    for run in runs:
        check_runs.append({
            "name": run.get("name"),
            "head_sha": head_sha,
            "conclusion": run.get("conclusion"),
            "status": run.get("status"),
            "app_id": ((run.get("app") or {}).get("id")),
            "source": "check_run",
        })

    bundle = {
        "base_repo": args.repo,
        "allowed_user_ids": allowed_user_ids,
        "pr": pr,
        "area": area,
        "head_sha": head_sha,
        "main_sha": main_sha,
        "compare_status": status,
        "behind_by": behind,
        "changed_files": changed,
        "tree_entries": tree_entries,
        "new_status": new_status,
        "new_progress": new_progress,
        "old_status": old_status,
        "old_progress": old_progress,
        # Recorded so the gate can confirm the baseline came from the directory being changed.
        "old_paths": old_paths,
        "current_main_cursor": current_cursor,
        "check_runs": check_runs,
    }
    pathlib.Path(args.out).write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    print(f"collected: area={area or '(none)'} head={head_sha[:7]} main={main_sha[:7]} "
          f"status={status} behind={behind} files={len(changed)} checks={len(check_runs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
