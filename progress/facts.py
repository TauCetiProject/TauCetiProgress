"""What actually landed in a window, extracted from git rather than from what anyone claimed.

This module exists because of a specific failure mode. PR descriptions in this project are good,
but they are *self-reported*: an author writes "this PR proves Cauchy's integral formula" and the
description is what a reporting model would otherwise read. A description can overstate, can be
edited after the fact, and -- since anyone may open a PR -- can contain text written to steer a
model. A report built from descriptions alone can therefore announce a headline theorem that the
merged diff does not contain.

So the writing model is given this instead as its ground truth: for each PR in the window, the
declarations that PR actually added, with the first sentence of each docstring, attributed by
diffing that PR's merge commit against its first parent. Descriptions are still passed along, but
as commentary, clearly delimited and capped.

All of this is local git plus the declaration scanner: no Lean build, no network.
"""

from . import lean, window

LEAN_PREFIX = "TauCeti/"
LEAN_SUFFIX = ".lean"

# Per-PR caps. A single PR adding hundreds of declarations is real (a big port), but a report does
# not need all of them, and an unbounded list would blow the model's context on the bootstrap
# window. The truncation is recorded so the report can never read as complete when it is not.
MAX_DECLS_PER_PR = 40
MAX_DOC_CHARS = 400


def _changed_lean_files(repo_dir, commit):
    """Lean files changed by `commit` relative to its first parent."""
    out = window.git(
        ["diff", "--name-only", "--diff-filter=AMR", f"{commit}^", commit, "--", f"{LEAN_PREFIX}"],
        repo_dir,
    )
    return [p for p in (line.strip() for line in out.splitlines()) if p.endswith(LEAN_SUFFIX)]


def _show(repo_dir, commit, path):
    """File contents at a commit, or "" when the file did not exist there."""
    try:
        return window.git(["show", f"{commit}:{path}"], repo_dir)
    except window.GitError:
        return ""


def declarations_added_by(repo_dir, commit):
    """`{name: {"kind","doc","file"}}` for declarations `commit` added.

    Compared against the commit's first parent, so a squash-merged PR is attributed exactly, and a
    declaration merely *moved* between files still shows as added in its new home and absent from
    the old one -- which is the honest reading for a report, since the mathematics did not change.
    """
    added = {}
    for path in _changed_lean_files(repo_dir, commit):
        before = _show(repo_dir, f"{commit}^", path)
        after = _show(repo_dir, commit, path)
        for name, info in lean.added_declarations(before, after).items():
            if name not in added:
                added[name] = {
                    "kind": info["kind"],
                    "doc": info["doc"][:MAX_DOC_CHARS],
                    "file": path,
                }
    return added


def collect(repo_dir, from_sha, to_sha, pr_numbers=None):
    """The factual spine of a window.

    Returns `{"files": [...], "prs": [{number, files, declarations, truncated}], "declarations":
    [...], "counts": {...}}`. `declarations` is the flat union, most substantial first, which is
    what a prompt wants to lead with.
    """
    numbers = window.window_prs(repo_dir, from_sha, to_sha) if pr_numbers is None else list(pr_numbers)
    wanted = set(numbers)

    # Walk the mainline once, pairing each merge commit with its PR number.
    log = window.git(
        ["log", "--first-parent", "--format=%H%x09%s", f"{from_sha}..{to_sha}"], repo_dir
    )
    per_pr = []
    all_files = set()
    flat = {}
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        n = window.pr_number_of_subject(subject.strip())
        if n is None or n not in wanted:
            continue
        decls = declarations_added_by(repo_dir, sha.strip())
        files_touched = sorted({d["file"] for d in decls.values()} | set(_changed_lean_files(repo_dir, sha.strip())))
        all_files.update(files_touched)
        items = sorted(decls.items(), key=lambda kv: (-len(kv[1]["doc"]), kv[0]))
        truncated = max(0, len(items) - MAX_DECLS_PER_PR)
        kept = items[:MAX_DECLS_PER_PR]
        per_pr.append(
            {
                "number": n,
                "sha": sha.strip(),
                "subject": subject.strip(),
                "files": files_touched,
                "declarations": [{"name": k, **v} for k, v in kept],
                "truncated_declarations": truncated,
            }
        )
        for k, v in kept:
            flat.setdefault(k, {"name": k, **v, "pr": n})

    # A documented declaration is more likely to be a result worth naming than an undocumented
    # helper, so lead with the documented ones. This is a presentation order, not a judgement.
    ordered = sorted(flat.values(), key=lambda d: (0 if d["doc"] else 1, d["name"]))
    return {
        "from_sha": from_sha,
        "to_sha": to_sha,
        "prs": per_pr,
        "declarations": ordered,
        "files": sorted(all_files),
        "counts": {
            "prs": len(per_pr),
            "declarations": len(ordered),
            "documented": sum(1 for d in ordered if d["doc"]),
            "files": len(all_files),
            "truncated_declarations": sum(p["truncated_declarations"] for p in per_pr),
        },
    }
