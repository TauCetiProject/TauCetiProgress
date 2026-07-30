#!/usr/bin/env python3
"""Fetch everything the merge gate needs into one JSON bundle, using only a read-only token.

Separating collection from decision keeps `progress.gate` a pure function of data, which is what
makes it unit-testable against two dozen hostile inputs. This script does the untrusted I/O:

* the pull request, its changed-file list, and the git tree entries for the two paths (so blob
  *modes* can be checked -- a symlink named `STATUS.md` is the obvious way to escape a
  path-restricted gate);
* the two files' contents at the pull request's head, read as **blobs by sha** rather than by
  checking the branch out, so nothing from the pull request is ever written to the workspace or
  executed;
* the same two files as they stand on freshly-fetched `main`, and the area's current cursor from
  there, so validation is against the repository as it is now rather than the pull request's stale
  base.

Run as: collect.py --repo O/R --pr N --base-dir DIR --allowed-user-ids 1,2 --out bundle.json
"""

import argparse
import base64
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from progress import files, gate  # noqa: E402


def gh_api(path, method=None):
    args = ["gh", "api", path]
    if method:
        args += ["-X", method]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"gh api {path} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def gh_api_paged(path):
    proc = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"gh api --paginate {path} failed: {proc.stderr.strip()}")
    # `--paginate` concatenates JSON arrays; normalise to one list.
    text = proc.stdout.strip()
    if not text:
        return []
    if "][" in text:
        text = "[" + text.replace("][", ",").strip("[]") + "]"
    return json.loads(text)


def blob_text(repo, sha):
    """A blob's contents by sha. Returns "" for a missing blob."""
    if not sha:
        return ""
    obj = gh_api(f"repos/{repo}/git/blobs/{sha}")
    if obj.get("encoding") == "base64":
        raw = base64.b64decode(obj.get("content") or "")
        # Decode leniently here; the gate re-checks UTF-8 validity and refuses if it is broken.
        return raw.decode("utf-8", "surrogateescape")
    return obj.get("content") or ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--base-dir", required=True, help="a checkout of current main")
    ap.add_argument("--allowed-user-ids", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    pr = gh_api(f"repos/{args.repo}/pulls/{args.pr}")
    changed = gh_api_paged(f"repos/{args.repo}/pulls/{args.pr}/files?per_page=100")
    head_sha = ((pr.get("head") or {}).get("sha")) or ""

    # The area comes from the branch, and is validated by the gate's own branch pattern. Deriving it
    # here too would risk the two disagreeing, so read it with the gate's regex and leave the
    # judgement to the gate.
    branch = (pr.get("head") or {}).get("ref") or ""
    m = gate.BRANCH_RE.match(branch)
    area = m.group(1) if m else ""

    # Tree entries for the changed paths, so modes and types can be checked.
    tree_entries = []
    if head_sha:
        for f in changed:
            path = f.get("filename") or ""
            if not gate.PATH_RE.match(path):
                # Left for the gate to refuse; do not fetch anything for it.
                continue
            try:
                info = gh_api(f"repos/{args.repo}/contents/{path}?ref={head_sha}")
            except SystemExit:
                continue
            tree_entries.append({
                "path": path,
                "mode": {"file": "100644", "symlink": "120000", "submodule": "160000"}.get(
                    info.get("type"), "100644" if info.get("type") == "file" else "?"
                ),
                "type": "blob" if info.get("type") == "file" else info.get("type"),
                "sha": info.get("sha"),
            })

    by_name = {}
    for f in changed:
        path = f.get("filename") or ""
        m2 = gate.PATH_RE.match(path)
        if m2:
            by_name[m2.group(2)] = f

    new_status = blob_text(args.repo, (by_name.get("STATUS.md") or {}).get("sha"))
    new_progress = blob_text(args.repo, (by_name.get("PROGRESS.md") or {}).get("sha"))

    # The same two files on current main, plus the cursor derived from them.
    base = pathlib.Path(args.base_dir)
    old_status = old_progress = None
    current_cursor = None
    if area:
        for parent in ("TauCetiRoadmap", "Completed"):
            d = base / parent / area
            if d.is_dir():
                sp, pp = d / "STATUS.md", d / "PROGRESS.md"
                old_status = sp.read_text(encoding="utf-8") if sp.is_file() else None
                old_progress = pp.read_text(encoding="utf-8") if pp.is_file() else None
                break
        if old_progress:
            try:
                current_cursor = files.cursor(old_progress)
            except files.FormatError:
                # An unparseable log on main is a real problem, but it is the gate's job to say so.
                current_cursor = None

    check_runs = []
    if head_sha:
        data = gh_api(f"repos/{args.repo}/commits/{head_sha}/check-runs?per_page=100")
        for run in data.get("check_runs") or []:
            check_runs.append({
                "name": run.get("name"),
                "head_sha": head_sha,
                "conclusion": run.get("conclusion"),
            })
        # Older integrations report a commit *status* rather than a check run.
        st = gh_api(f"repos/{args.repo}/commits/{head_sha}/status")
        for s in st.get("statuses") or []:
            check_runs.append({
                "name": s.get("context"),
                "head_sha": head_sha,
                "conclusion": "success" if s.get("state") == "success" else s.get("state"),
            })

    bundle = {
        "base_repo": args.repo,
        "allowed_user_ids": [int(x) for x in re.split(r"[,\s]+", args.allowed_user_ids) if x],
        "pr": pr,
        "area": area,
        "changed_files": changed,
        "tree_entries": tree_entries,
        "new_status": new_status,
        "new_progress": new_progress,
        "old_status": old_status,
        "old_progress": old_progress,
        "current_main_cursor": current_cursor,
        "check_runs": check_runs,
    }
    pathlib.Path(args.out).write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    print(f"collected: area={area or '(none)'} head={head_sha[:7]} "
          f"files={len(changed)} checks={len(check_runs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
