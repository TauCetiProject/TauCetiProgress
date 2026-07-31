"""The decision: is an update due, which roadmap does it cover, and which PRs are in the window.

Everything here is script logic. No model is started until this has produced a plan, which is the
design rule for the whole tool: the parts that can be decided mechanically are decided
mechanically, and the model is left with prose.

Two thresholds, both overridable so they can be raised as the project grows:

* `IDLE_HOURS` -- an update is due only if *no* area has been updated for this long. This paces the
  whole project to about one report a day rather than one per area.
* `MIN_PRS` -- the winning area must have at least this many PRs in its window. Without a floor, a
  quiet day produces a padded report about three commits.
"""

import datetime
import json
import pathlib
import re

from . import files, gh, window
from .window import CODE_REF

IDLE_HOURS = 24.0
MIN_PRS = 10
# How long an open progress pull request keeps marking its area in flight. Past this it is assumed
# stuck rather than pending: one full cadence period is long enough for any pull request that was
# going to merge to have merged, and the merge check re-runs on every push and on CI completing.
STALE_PR_HOURS = 24.0

# The commit-subject prefix that marks a merged progress update. `apply` uses it as the PR title
# prefix, and a squash merge carries the PR title into the commit subject, so this one string links
# the cheap `due` check to the update mechanism.
COMMIT_PREFIX = "progress:"

STATUS_NAME = "STATUS.md"
PROGRESS_NAME = "PROGRESS.md"

# Where areas live in the roadmap repo. `Completed/` holds finished roadmaps (EffectiveBounds).
AREAS_DIR = "TauCetiRoadmap"
COMPLETED_DIR = "Completed"


class NotDue(Exception):
    """No update is due. Carries the human-readable reason; the CLI exits 75 on it, which is the
    worker's `EX_NOPROGRESS`, so a round falls through to other work."""


def docs_source_commit():
    """The TauCeti commit the published documentation was built from, or None if unreadable."""
    from .docs import Docs, DocsError

    try:
        return Docs().source_commit()
    except DocsError:
        return None


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(text):
    # GitHub returns `...Z`; `fromisoformat` wants an offset on older Pythons.
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def discover_areas(roadmap_dir):
    """`{area: relative_dir}` for every roadmap area in a checkout.

    An area is a top-level directory under `TauCetiRoadmap/` that contains a `README.md` -- exactly
    the rule `TauCeti/scripts/roadmap_label.py:canonical_areas` uses, so the areas here and the
    `roadmap/<Area>` labels can never disagree. Archived roadmaps under `Completed/` are included so
    their existing status files are still found, but they are excluded from selection unless new
    PRs arrive for them.

    Deliberately keyed on README presence and nothing else: an area may or may not carry a
    `Suggested.lean`, the file has been renamed before, and nested sub-roadmaps (RepresentationTheory)
    have their own READMEs one level down while remaining a single labelled area.
    """
    root = pathlib.Path(roadmap_dir)
    inner = root / AREAS_DIR
    base = inner if inner.is_dir() else root
    found = {}
    for parent, prefix in ((base, AREAS_DIR if base is inner else ""),
                           (root / COMPLETED_DIR, COMPLETED_DIR)):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "README.md").is_file():
                found[child.name] = f"{prefix}/{child.name}" if prefix else child.name
    return found


def read_area_files(roadmap_dir, rel_dir):
    """`(status_text_or_None, progress_text_or_None)` for one area."""
    base = pathlib.Path(roadmap_dir) / rel_dir
    status = base / STATUS_NAME
    progress = base / PROGRESS_NAME
    return (
        status.read_text(encoding="utf-8") if status.is_file() else None,
        progress.read_text(encoding="utf-8") if progress.is_file() else None,
    )


def last_update_age_hours(commits, now=None):
    """Hours since the newest merged progress update, or None if there has never been one.

    Reads the roadmap repo's own commit history, so the cadence is measured against when reports
    actually *landed* -- not against the TauCeti window they describe, and not against any local
    state a fleet could not share.
    """
    now = now or _utcnow()
    newest = None
    for date, subject in commits:
        if subject.startswith(COMMIT_PREFIX):
            ts = _parse_iso(date)
            if newest is None or ts > newest:
                newest = ts
    if newest is None:
        return None
    return (now - newest).total_seconds() / 3600.0


def check_cadence(commits, idle_hours=IDLE_HOURS, now=None):
    """Raise NotDue unless enough time has passed since the last landed update."""
    age = last_update_age_hours(commits, now=now)
    if age is None:
        return "no progress update has ever landed"
    if age < idle_hours:
        raise NotDue(f"last progress update landed {age:.1f}h ago (< {idle_hours:g}h)")
    return f"last progress update landed {age:.1f}h ago"


def area_window(repo_dir, area_prs, from_sha, to_sha):
    """PR numbers in `(from_sha, to_sha]` that belong to an area, newest first.

    Attribution is an intersection: git says which PRs are in the range, one label query says
    which PRs belong to the area. Order comes from git (newest first).
    """
    numbers = window.window_prs(repo_dir, from_sha, to_sha)
    wanted = set(area_prs)
    return [n for n in numbers if n in wanted]


def bootstrap_from_sha(repo_dir, area, area_prs, ref=CODE_REF):
    """A `from_sha` for an area that has never been reported, or None if it has no merged PRs.

    The window is half-open, so the cursor must be the *parent* of the area's earliest labelled
    merge; using the merge itself would drop that first PR from every area's first report.
    """
    numbers = list(area_prs)
    if not numbers:
        return None
    earliest = min(numbers)
    merge = window.find_merge_commit(repo_dir, earliest, ref=ref)
    if merge is None:
        # The PR is labelled but its merge is not on the mainline we can see (a shallow checkout,
        # or a PR merged into another branch). Refuse rather than guess a cursor.
        raise window.GitError(
            f"could not locate the merge commit for {area}'s earliest PR #{earliest} in {ref}; "
            f"a full-history checkout is required to bootstrap an area"
        )
    return window.first_parent_before(repo_dir, merge)


def in_flight_areas(open_prs, now=None, stale_hours=STALE_PR_HOURS):
    """`({area: pr}, [stale_note])` from the open progress pull requests.

    The branch is `progress/<from7>-<to7>/<Area>`, so the area is the last segment.

    An open progress pull request marks its area in flight, but only for a while. One that the merge
    check refuses permanently never merges and never closes itself, and treating it as in flight
    forever would stop that roadmap being reported by *anyone*, including the maintainer, with no
    signal beyond the area quietly never appearing again. Past `stale_hours` it stops blocking and is
    reported as a note instead, so the next round covers the area over a wider window and the
    abandoned pull request becomes visible rather than merely obstructive.

    A pull request with no or unreadable `createdAt` keeps blocking. Age is the only evidence that it
    is stuck, and without it the safe assumption is that it is still in flight: opening a duplicate is
    worse than waiting.
    """
    now = now or _utcnow()
    blocked, stale = {}, []
    for pr in open_prs:
        parts = (pr.get("headRefName") or "").split("/")
        if len(parts) < 3:
            continue
        area_name = parts[-1]
        age_hours = None
        created = pr.get("createdAt")
        if created:
            try:
                age_hours = (now - _parse_iso(created)).total_seconds() / 3600.0
            except ValueError:
                age_hours = None
        if age_hours is not None and age_hours > stale_hours:
            stale.append(
                f"{area_name}: PR #{pr.get('number')} has been open {age_hours / 24:.1f} days "
                f"without merging, so it no longer marks the area in flight -- close it if it is dead"
            )
            continue
        blocked[area_name] = pr
    return blocked, stale


def build_plan(
    roadmap_dir,
    code_dir,
    commits=None,
    open_prs=None,
    ref=CODE_REF,
    idle_hours=IDLE_HOURS,
    min_prs=MIN_PRS,
    stale_hours=STALE_PR_HOURS,
    now=None,
    only_area=None,
):
    """The whole decision. Returns a plan dict, or raises NotDue.

    `commits` and `open_prs` may be supplied by the caller (the worker already holds them, and the
    tests inject fixtures); otherwise they are fetched.
    """
    now = now or _utcnow()
    commits = gh.recent_roadmap_commits() if commits is None else commits
    cadence_reason = check_cadence(commits, idle_hours=idle_hours, now=now)

    open_prs = gh.open_progress_prs() if open_prs is None else open_prs
    blocked, stale = in_flight_areas(open_prs, now=now, stale_hours=stale_hours)

    areas = discover_areas(roadmap_dir)
    if only_area:
        if only_area not in areas:
            raise NotDue(f"{only_area} is not a roadmap area in this checkout")
        areas = {only_area: areas[only_area]}

    # The window ends at the commit the PUBLISHED DOCUMENTATION describes, not at the branch tip.
    #
    # `docgen` nominates the most recent commit with a published build, but the deploy is
    # independent, so the branch can sit ahead of the site for a while. Ending the window at the tip
    # would record a cursor covering work the report never described -- the next window starts after
    # it, so that work would never be reported at all -- and would offer links for results whose
    # pages do not exist yet. Ending it at the documented commit makes the header, the prose and the
    # links describe one and the same state.
    docs_sha = docs_source_commit()
    if docs_sha is None:
        raise NotDue("the published documentation could not be read, so no window can be closed")
    tip = window.head_sha(code_dir, ref=ref)
    if docs_sha != tip and not window.is_ancestor(code_dir, docs_sha, tip):
        raise NotDue(
            f"the documentation was built from {docs_sha[:7]}, which is not an ancestor of {ref} "
            f"({tip[:7]}); they describe different histories"
        )
    to_sha = docs_sha

    candidates = []
    skipped = list(stale)
    for area, rel_dir in areas.items():
        if area in blocked:
            skipped.append(f"{area}: PR #{blocked[area]['number']} is still open")
            continue
        status_text, progress_text = read_area_files(roadmap_dir, rel_dir)
        try:
            from_sha = files.cursor(progress_text) if progress_text else None
        except files.FormatError as exc:
            skipped.append(f"{area}: unparseable PROGRESS.md ({exc})")
            continue

        # One label query per area. See gh.merged_prs_for_area for why attribution is per-area
        # rather than per-PR.
        area_prs = gh.merged_prs_for_area(area)
        if not area_prs:
            skipped.append(f"{area}: no merged PRs yet")
            continue

        bootstrapped = False
        if from_sha is None:
            from_sha = bootstrap_from_sha(code_dir, area, area_prs, ref=ref)
            bootstrapped = True
            if from_sha is None:
                skipped.append(f"{area}: no merged PRs yet")
                continue
        if from_sha == to_sha:
            skipped.append(f"{area}: already at {to_sha[:7]}")
            continue

        # The SHA window is the authority on what belongs in a report, and deliberately the ONLY
        # authority. An earlier version also subtracted every PR number any previous section had
        # claimed, as a guard against a PR being relabelled after it was reported. That made a
        # section's `prs` list -- attacker-supplied metadata in a file anyone may append to -- able to
        # suppress real work permanently: a report claiming thousands of numbers would leave every
        # later window for that roadmap looking empty, with no error anywhere.
        #
        # The failure it guarded against is a PR appearing in two reports after someone changed its
        # label. That is cosmetic. Silently unreportable history is not, so the trade goes the other
        # way.
        fresh = area_window(code_dir, area_prs, from_sha, to_sha)
        if not fresh:
            skipped.append(f"{area}: nothing new since {from_sha[:7]}")
            continue

        candidates.append(
            {
                "area": area,
                "rel_dir": rel_dir,
                "from_sha": from_sha,
                "prs": fresh,
                "bootstrapped": bootstrapped,
                "status_text": status_text,
                "progress_text": progress_text,
            }
        )

    if not candidates:
        raise NotDue("; ".join(skipped) or "no candidate areas")

    ranked = sorted(
        candidates,
        key=lambda c: (-len(c["prs"]), c["area"]),
    )
    best = ranked[0]
    if len(best["prs"]) < min_prs:
        raise NotDue(
            f"{cadence_reason}, but the busiest area ({best['area']}) has only "
            f"{len(best['prs'])} PR(s) in its window (< {min_prs})"
        )

    return {
        "roadmap": best["area"],
        "rel_dir": best["rel_dir"],
        "from_sha": best["from_sha"],
        "to_sha": to_sha,
        "prs": best["prs"],
        "bootstrapped": best["bootstrapped"],
        "reason": (
            f"{cadence_reason}; {best['area']} has {len(best['prs'])} PR(s) since "
            f"{best['from_sha'][:7]}"
        ),
        "skipped": skipped,
        "status_path": f"{best['rel_dir']}/{STATUS_NAME}",
        "progress_path": f"{best['rel_dir']}/{PROGRESS_NAME}",
        "from_date": window.commit_date(code_dir, best["from_sha"]),
        "to_date": window.commit_date(code_dir, to_sha),
        "runner_up": [
            {"area": c["area"], "prs": len(c["prs"])} for c in ranked[1:4]
        ],
    }


def plan_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True)
