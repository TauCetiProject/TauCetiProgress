"""Retire the reports a landing has just made unmergeable.

An area can hold several open reports, and nothing used to close the ones that lose. They
accumulated: 13 of the 15 open progress pull requests were stranded this way when this was written.

**Retire on positive evidence, never on disagreement with a snapshot.** A report is retired only
when the committed `PROGRESS.md` shows its starting cursor has already been consumed -- that is,
some section already appended at it and the log has moved past. Anything else is left alone.

That distinction is the whole of this module's safety. "Its cursor is not the current one" sounds
equivalent and is not: a contents read can be stale, and a report starting *ahead* of a stale answer
disagrees with it exactly as loudly as one starting behind. Under-reading committed history can only
shrink the consumed set, so a stale answer retires fewer reports, never a live one.

That argument holds for full SHAs only. A branch name carries seven hex characters, and a report
starting at a commit a stale log has not seen yet can share its prefix with a cursor the log did
see. So the branch is only the cheap filter; the decision compares the report's full starting SHA,
read from its own head, against the full SHAs of the log, and keeps the report whenever that SHA
cannot be read.

An earlier version closed reports *before* a replacement landed, by arguing one window contained
another. Three things were wrong with it, and they are why this module runs where it does:

* It could cancel a landing already in flight. Close the loser, have the winner then fail its build,
  and nothing lands -- and the loser is now closed-unmerged, which `apply` treats as permanently
  refused, so no later run repairs it.
* It raced the gate. The gate reads `state` when it collects and trusts that snapshot until it
  writes, so a report closed while a run was validating landed anyway.
* It trusted a pull request body. Containment was read from metadata anyone can edit, so a stale or
  forged narrower body got a live report closed.

Running after the compare-and-swap removes all three: the ref update has already chosen the winner.

Ownership is deliberately not consulted here, unlike everywhere in `apply`. There the question was
whose work may be superseded, and the answer had to be "only our own" or a stranger could be vetoed.
Here the report is unmergeable for everybody, by construction. Leaving it open is not kindness: it
marks the area in flight, so it delays the next report for its own author as much as anyone. What
protects a stranger instead is the grammar: only a branch the generator itself could have produced,
targeting the branch a report must target, is ever touched.
"""

from . import files, gh
from .gate import BRANCH_RE

BASE_BRANCH = "main"


def retirement_evidence(progress_text):
    """`(consumed, live)` from a committed `PROGRESS.md`.

    `live` is the cursor a new report must start at; `consumed` is every cursor the log has already
    appended at and moved past. A report starting at one of those can never append again, and the
    log is committed history rather than a mutable field, so the evidence does not rot.

    Reading less history than exists shrinks `consumed`, which can only retire fewer reports. That
    is the direction a stale or truncated read must fail in.
    """
    try:
        sections = files.parse_sections(progress_text or "")
    except Exception:  # noqa: BLE001 -- a malformed log is evidence of nothing
        return set(), ""
    if not sections:
        return set(), ""
    live = sections[-1]["to_sha"]
    consumed = {s["from_sha"] for s in sections} | {s["to_sha"] for s in sections}
    consumed.discard(live)
    return consumed, live


def retirable_prs(open_prs, area, consumed, live, start_of):
    """Open reports for `area` whose starting cursor is provably spent: `[(row, reason)]`.

    The branch grammar is the gate's own, not a looser one. `progress/<7 hex>-<7 hex>/<Area>` is what
    the generator produces; anything else is somebody's ordinary pull request that happens to start
    with `progress/`, and "it cannot pass an automated gate" is not a reason to close a human's work.
    The base branch is checked for the same reason.

    A seven-character prefix is not a commit, so the branch only nominates candidates. `start_of(row)`
    must return the report's full starting SHA, or None when it cannot be established, and the report
    is retired only if that full SHA is itself a spent cursor and not the live one. A prefix match
    alone never retires anything: under-retiring is recoverable, the opposite is not.
    """
    out = []
    for row in open_prs:
        m = BRANCH_RE.match(row.get("headRefName") or "")
        if not m or m.group(3) != area:
            continue
        if (row.get("baseRefName") or BASE_BRANCH) != BASE_BRANCH:
            continue
        from7, to7 = m.group(1), m.group(2)
        if live and live.startswith(from7):
            continue
        if not any(c.startswith(from7) for c in consumed):
            continue
        start = start_of(row)
        if not start or not start.startswith(from7):
            print(f"#{row.get('number')}: could not establish the full starting SHA behind "
                  f"{from7}; kept")
            continue
        if start == live or start not in consumed:
            continue
        out.append((row, f"its window starts at {start[:7]}, which {area} already appended at and "
                         f"moved past; the cursor is now {live[:7] or 'unknown'}"))
    return out


def report_start(row, progress_path, repo=gh.ROADMAP_REPO):
    """The full SHA a report's window starts at, read from the report's own head, or None.

    The newest section of the head's `PROGRESS.md` is the one the report appends, and its `from_sha`
    is the cursor it was generated against. It must agree with the branch name at both ends; if it
    does not, or the head cannot be read or parsed, the answer is None and the report is kept.

    This is content the pull request's author controls, and that is acceptable here: it only ever
    decides whether that same pull request is closed, and only in conjunction with a spent cursor
    read from `main`.
    """
    m = BRANCH_RE.match(row.get("headRefName") or "")
    head = row.get("headSha") or ""
    if not m or not head:
        return None
    try:
        text = gh.file_on_default_branch(progress_path, repo=repo, ref=head)
        sections = files.parse_sections(text or "")
    except Exception:  # noqa: BLE001 -- unreadable or malformed means "cannot tell": keep it
        return None
    if not sections:
        return None
    last = sections[-1]
    if not last["from_sha"].startswith(m.group(1)) or not last["to_sha"].startswith(m.group(2)):
        return None
    return last["from_sha"]


def close_orphans(rows, landed_url="", repo=gh.ROADMAP_REPO):
    """Close each retirable report, saying why. Returns the number that could not be closed.

    `repo` is threaded rather than defaulted at the call site: reading one repository's pull request
    numbers and closing another's by the same number is a whole-repository mix-up waiting to happen.

    Failures are counted rather than raised. The content is already on `main` by the time this runs,
    so a cleanup that does not complete must not turn a successful landing into a failed one.
    """
    failed = 0
    for row, reason in rows:
        note = f"Retired: {reason}."
        if landed_url:
            note += f" The window it would have appended to was taken by {landed_url}."
        note += " Reopening will not help; a fresh report for the current cursor is what is needed."
        try:
            gh.gh(["pr", "close", str(row["number"]), "--repo", repo, "--comment", note])
            print(f"retired {repo}#{row['number']}: {reason}")
        except gh.GhError as exc:
            failed += 1
            print(f"could not close {repo}#{row['number']} ({exc}); leaving it open")
    return failed


def other_parent(progress_path):
    """The same area's `PROGRESS.md` under the other parent directory, or None.

    `TauCetiRoadmap/<area>` and `Completed/<area>` are different roadmaps that may both exist, which
    is why the collector derives the parent from the changed paths rather than probing in a fixed
    order.
    """
    for a, b in (("TauCetiRoadmap/", "Completed/"), ("Completed/", "TauCetiRoadmap/")):
        if progress_path.startswith(a):
            return b + progress_path[len(a):]
    return None


def sweep_area(area, progress_path, landed_url="", repo=gh.ROADMAP_REPO):
    """Retire every open report for `area` whose starting cursor the committed log has spent.

    Returns `(closed, failed)`. `progress_path` carries the parent the landing actually touched,
    because `TauCetiRoadmap/<area>` and `Completed/<area>` keep different cursors in different files.

    Retires nothing when the name exists under BOTH parents, or when it cannot tell whether it does. A report branch records no parent, so
    the open reports cannot be attributed to one roadmap or the other, and retiring on the log we
    happen to be holding would retire the other roadmap's live reports.
    """
    try:
        text = gh.file_on_default_branch(progress_path, repo=repo)
    except gh.GhError as exc:
        print(f"could not read {progress_path} in {repo} ({exc}); nothing retired")
        return 0, 0
    if text is None:
        print(f"no log at {progress_path} in {repo}; nothing retired")
        return 0, 0
    sibling = other_parent(progress_path)
    if sibling:
        # Fails closed. Only GitHub saying 404 counts as absent; a lookup that did not answer could
        # be hiding the very sibling whose live reports this would otherwise retire.
        try:
            sibling_text = gh.file_on_default_branch(sibling, repo=repo)
        except gh.GhError as exc:
            print(f"could not tell whether {sibling} exists ({exc}); nothing retired")
            return 0, 0
        if sibling_text is not None:
            print(f"{area} exists under both parents ({progress_path} and {sibling}); report "
                  f"branches do not record which, so nothing is retired")
            return 0, 0
    consumed, live = retirement_evidence(text)
    if not consumed:
        print(f"{area}: no spent cursors in {progress_path}; nothing retired")
        return 0, 0
    rows = retirable_prs(gh.open_progress_prs(repo=repo), area, consumed, live,
                         start_of=lambda row: report_start(row, progress_path, repo=repo))
    if not rows:
        print(f"{area}: nothing to retire at cursor {live[:7]}")
        return 0, 0
    failed = close_orphans(rows, landed_url, repo=repo)
    return len(rows) - failed, failed
