"""Retire the reports a landing has just made unmergeable.

An area can hold more than one open report, and nothing used to close the ones that lose. They
accumulated: 13 of the 15 open progress pull requests were stranded this way when this was written.

**What makes a report unmergeable is a fact, not a prediction.** The gate requires a byte-exact
append at the cursor recorded in `PROGRESS.md`, so once `main` moves, every open report for that
area whose window does not start at the new cursor can never satisfy it again. That is decidable
from the branch name and the cursor alone -- no metadata, no comparison of windows, no ordering.

An earlier version of this closed reports *before* the replacement landed, by arguing that one
window contained another. Three things were wrong with it, and they are why this module runs where
it does:

* It could cancel a landing that was already in flight. Close the loser, have the winner then fail
  its build, and nothing lands -- and the loser is now closed-unmerged, which `apply` treats as
  permanently refused, so no later run repairs it.
* It raced the gate. The gate reads `state` when it collects and trusts that snapshot until it
  writes, so a report closed while a run was validating landed anyway. Closing only after the swap
  has happened removes the race rather than narrowing it.
* It trusted a pull request body. Containment was read from metadata anyone can edit, so a stale or
  forged narrower body got a live report closed.

Running after the compare-and-swap removes all three at once: the ref update has already chosen the
winner, so there is nothing left to predict.

Ownership is deliberately not consulted here, unlike everywhere in `apply`. There the question was
whose work may be superseded, and the answer had to be "only our own" or a stranger could be vetoed.
Here the report is unmergeable for everybody, by construction. Leaving it open is not kindness: it
marks the area in flight, so it delays the next report for its own author as much as anyone.
"""

from . import files, gh

BRANCH_PREFIX = "progress/"


def orphaned_prs(open_prs, area, live_cursor):
    """Open reports for `area` that can no longer append at `live_cursor`: `[(row, reason)]`.

    `live_cursor` must be the cursor as it is NOW, read after the landing. Passing a stale one
    inverts the test: a run holding an old cursor would read the only mergeable report as the
    orphan. An empty cursor returns nothing rather than guessing.
    """
    if not live_cursor:
        return []
    out = []
    for row in open_prs:
        parts = (row.get("headRefName") or "").split("/")
        if len(parts) != 3 or parts[-1] != area:
            continue
        from7 = parts[1].split("-")[0] if "-" in parts[1] else ""
        if not from7:
            continue
        if not live_cursor.startswith(from7):
            out.append((row, f"its window starts at {from7}; the {area} cursor is now "
                             f"{live_cursor[:7]}, so it can no longer append"))
    return out


def close_orphans(rows, landed_url=""):
    """Close each orphaned report, saying why. Returns the number that could not be closed.

    Failures are counted rather than raised: the content is already on `main` by the time this runs,
    so a cleanup that does not complete must not turn a successful landing into a failed one. The
    count is returned so the caller can still say so out loud.
    """
    failed = 0
    for row, reason in rows:
        note = f"Retired: {reason}."
        if landed_url:
            note += f" The window it would have appended to was taken by {landed_url}."
        note += " Reopening will not help; a fresh report for the current cursor is what is needed."
        try:
            gh.gh(["pr", "close", str(row["number"]), "--repo", gh.ROADMAP_REPO, "--comment", note])
            print(f"retired #{row['number']}: {reason}")
        except gh.GhError as exc:
            failed += 1
            print(f"could not close #{row['number']} ({exc}); leaving it open")
    return failed


def other_parent(progress_path):
    """The same area's `PROGRESS.md` under the other parent directory, or None.

    `TauCetiRoadmap/<area>` and `Completed/<area>` are different roadmaps that may both exist, which
    is why the collector derives the parent from the changed paths rather than probing in a fixed
    order. `sweep_area` needs it to answer a question the collector does not have to: whether the
    name is ambiguous.
    """
    for a, b in (("TauCetiRoadmap/", "Completed/"), ("Completed/", "TauCetiRoadmap/")):
        if progress_path.startswith(a):
            return b + progress_path[len(a):]
    return None


def sweep_area(area, progress_path, landed_url="", repo=gh.ROADMAP_REPO):
    """Read the live cursor for `area` and retire every open report that cannot reach it.

    Returns `(closed, failed)`. Reads the cursor through the API rather than from a checkout: this
    runs moments after the ref moved, which is exactly when a clone is stale.

    `progress_path` carries the parent the landing actually touched, because the cursor for
    `TauCetiRoadmap/<area>` and for `Completed/<area>` are different values in different files.

    Retires nothing at all when the name exists under BOTH parents. A report branch is
    `progress/<from7>-<to7>/<Area>` and records no parent, so in that case the open reports cannot be
    attributed to one roadmap or the other, and retiring on the cursor we happen to hold would retire
    the other roadmap's live reports. Leaving a few orphans for a human beats discarding live work;
    the ambiguity is reported rather than resolved by guessing.
    """
    live = files.cursor(gh.file_on_default_branch(progress_path, repo=repo) or "") or ""
    if not live:
        print(f"no cursor at {progress_path} on {repo}; nothing retired")
        return 0, 0
    sibling = other_parent(progress_path)
    if sibling and gh.file_on_default_branch(sibling, repo=repo) is not None:
        print(f"{area} exists under both parents ({progress_path} and {sibling}); report branches "
              f"do not record which, so nothing is retired")
        return 0, 0
    rows = orphaned_prs(gh.open_progress_prs(repo=repo), area, live)
    if not rows:
        print(f"{area}: nothing to retire at cursor {live[:7]}")
        return 0, 0
    failed = close_orphans(rows, landed_url)
    return len(rows) - failed, failed
