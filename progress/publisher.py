"""Who may publish an automated progress report, checked before a round spends anything.

Two independent things have to be true for a report to land unattended:

* The operator's numeric user id is listed in `.github/progress-publishers.txt` on the roadmap
  repository's default branch. That file is the merge check's allowlist.
* The operator can **push a branch** to the roadmap repository. `apply` pushes the report branch to
  the canonical repository, never a fork, because the merge check refuses fork heads.

Checking both *before* a round starts is the whole point of this module, because both failures are
expensive and neither is obvious:

* An operator who cannot push runs `plan`, `facts` and the writing model -- the costly part -- and
  only then dies on the push, having produced nothing.
* An operator who can push but is not listed opens a pull request the merge check refuses and will
  never merge. That is the worse case: an open progress pull request is its area's in-flight marker,
  so the refused pull request then stops *every* operator, including the maintainer, from reporting
  on that area until a human closes it.

The allowlist is read from the default branch, never from a pull request head. The merge check reads
it at the base commit for the same reason: a pull request must not be able to list its own author.
Adding a publisher is an ordinary roadmap pull request, and `/.github/` there belongs to
`@TauCetiProject/humans` in CODEOWNERS, so it takes a core-team review.

This is a courtesy check, not a security boundary. It runs on the operator's own machine and an
operator can simply not call it; the merge check on the server is what actually decides, and it
consults the same file.
"""

import base64
import json
import re

from . import gh

PUBLISHERS_PATH = ".github/progress-publishers.txt"

# Deliberately not `str.isdigit`, which accepts superscripts and non-ASCII digits that `int()` then
# happily parses. An allowlist entry that does not look like what it means is exactly the kind of
# thing that should fail loudly here.
_ID_RE = re.compile(r"\A[0-9]{1,20}\Z")

# Characters that Python's `str.splitlines` treats as line breaks but a reviewer's eyes, a terminal
# and GitHub's diff view generally do not. Left in, they let an entry hide inside what renders as a
# single comment line:
#
#     # looks like one comment<U+2028>999999 attacker
#
# splits into a comment AND a live allowlist entry. Since the whole security value of this file is
# that a human reviewed the diff, anything that makes the file read differently to a human than to
# this parser is rejected outright rather than normalised.
_EXOTIC_BREAKS = "\v\f\x1c\x1d\x1e\x85  "


class NotAPublisher(RuntimeError):
    """This identity cannot land a progress report, so a round would waste its work."""


def parse_publishers(text):
    """The set of numeric user ids in the publishers file.

    One id per line; `#` starts a comment; anything after the id on a line is free text, so the file
    can carry the login beside each id and stay readable. Ids are numeric because logins are
    renameable, and a renamed login would hand publishing rights to whoever claimed the old name.

    A malformed line raises rather than being skipped. Dropping an unparseable entry would silently
    shrink the allowlist, and a too-small allowlist fails as "reports quietly stopped", which is far
    harder to diagnose than a parse error naming the line.
    """
    if text.startswith("﻿"):  # a byte-order mark is cosmetic, not an entry
        text = text[1:]
    for ch in _EXOTIC_BREAKS:
        if ch in text:
            raise ValueError(
                f"{PUBLISHERS_PATH}: contains U+{ord(ch):04X}, which some tools treat as a line "
                f"break and others do not; the file must read the same way to a reviewer as to this "
                f"parser"
            )
    ids = set()
    # `split("\n")`, never `splitlines()`: the latter breaks on the characters rejected above.
    for lineno, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        if not _ID_RE.match(token):
            raise ValueError(f"{PUBLISHERS_PATH}:{lineno}: {token!r} is not a numeric user id")
        ids.add(int(token))
    return ids


def publishers(repo=gh.ROADMAP_REPO, ref="main"):
    """Read and parse the allowlist at `ref`. Raises if it cannot be read, so callers fail closed."""
    raw = gh.gh(["api", f"repos/{repo}/contents/{PUBLISHERS_PATH}?ref={ref}", "--jq", ".content"])
    text = base64.b64decode(raw.strip()).decode("utf-8")
    return parse_publishers(text)


def viewer_id():
    """The numeric id of the identity `gh` is authenticated as."""
    return int(json.loads(gh.gh(["api", "user", "--jq", ".id"]).strip()))


def can_push(repo=gh.ROADMAP_REPO):
    """Whether that identity can push to `repo`.

    Mirrors the push preflight the review kind already uses for TauCetiData in TauCetiWorker, so the
    two read the same way.
    """
    return gh.gh(["api", f"repos/{repo}", "--jq", ".permissions.push"]).strip() == "true"


def check_can_publish(repo=gh.ROADMAP_REPO, ref="main", uid=None, allowed=None, pushable=None):
    """Return a one-line reason this identity may publish, or raise `NotAPublisher`.

    The three lookups are injectable so the decision can be tested without network access.
    """
    uid = viewer_id() if uid is None else uid
    allowed = publishers(repo, ref) if allowed is None else allowed
    if uid not in allowed:
        raise NotAPublisher(
            f"user id {uid} is not listed in {PUBLISHERS_PATH} on {repo}@{ref}, so the merge check "
            f"would refuse the pull request this round would open, and the refused pull request "
            f"would then block its area for everyone"
        )
    pushable = can_push(repo) if pushable is None else pushable
    if not pushable:
        raise NotAPublisher(
            f"user id {uid} is a listed publisher but cannot push to {repo}; `apply` pushes the "
            f"report branch to the canonical repository and the merge check refuses fork heads"
        )
    return f"user id {uid} may publish to {repo}"
