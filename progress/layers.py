"""A roadmap's layers, and the per-layer coverage block a status report ends with.

A roadmap README names the units of its own plan as headings: `### Layer 3: ...`, `## Lane G: grid
homology`, `### Part A — ...`, `### Stage 2: ...`, or a short label such as `### L0A — sheaves of
modules` or `### S1: the twenty-six sporadic presentations`. Those headings are the units a status
report is asked to assess, one state each, so that the assessment can be read by a script -- the
prose of `STATUS.md` says the same things, but nothing can aggregate prose across forty roadmaps.

The extraction here is deliberately the same rule the consumer uses (`scripts/roadmap_progress.py`
in the TauCeti repository, which builds the site's Progress page): worded headings win outright,
because a roadmap that has `## Part A` also tends to have `### A1` sub-headings beneath it and those
are milestones, not further layers; short labels count only in a roadmap that names its layers that
way throughout; bold bullets only when there are no layer headings at all. A layer's id is the label
before the first separator, `Layer 3` or `Lane G` or `L0A`, which is what the block and the marker
refer to.

Pure: text in, values out. Nothing here reads a file or runs a command.
"""

import hashlib
import re

from . import files

_WORD_LEAD = r"(?:Layer|Lane|Part|Stage|Milestone)\b"
_SHORT_LEAD = r"[A-Z]\d+[A-Za-z]?(?=\s*[:—–,])"
_LAYER_LEAD = rf"(?:{_WORD_LEAD}|{_SHORT_LEAD})"
_WORD_HEADING_RE = re.compile(rf"^#{{2,4}}\s+({_WORD_LEAD}.*?)\s*$", re.M)
_SHORT_HEADING_RE = re.compile(rf"^#{{2,4}}\s+({_SHORT_LEAD}.*?)\s*$", re.M)
_BULLET_RE = re.compile(rf"^- \*\*({_LAYER_LEAD}[^*]*?)\*\*", re.M)
_ID_RE = re.compile(rf"^({_LAYER_LEAD}[^:—–,(]*?)\s*(?:[:—–,(]|$)")

# The block the model appends to its status prose. Fenced so it cannot be mistaken for prose, and
# named so a plain code block in the prose cannot be mistaken for it.
BLOCK_OPEN_RE = re.compile(r"^```coverage[ \t]*$", re.M)
BLOCK_CLOSE_RE = re.compile(r"^```[ \t]*$", re.M)
# `<id>: <state>` with an optional ` — <remaining>` (an em dash, or ` -- `, or ` - `).
LINE_RE = re.compile(r"\A(?P<id>[^:]+?)\s*:\s*(?P<state>[A-Za-z]+)(?:\s+(?:—|–|--|-)\s+(?P<remaining>.+?))?\s*\Z")


def readme_sha(text):
    """The identity of a specification: a SHA-256 of the README's text.

    A layer's requirements can change under an unchanged heading, so layer ids alone do not say
    which specification an assessment was made against. The consumer refuses a marker whose hash
    does not match the README it read the layers from.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def layer_id(title):
    m = _ID_RE.match(title)
    return (m.group(1) if m else title).strip()


def headings(readme):
    """`[{id, title, line}]` for a README's layer headings, in order, 1-based lines.

    Trailing parentheticals (`(AEC III.6)`) are dropped from titles. A README with no recognisable
    layer headings gives an empty list, and its status reports then carry no coverage block: that
    says nothing about the roadmap's work, only that its milestones are named some other way.
    """
    def scan(rx):
        return [(m.group(1), readme.count("\n", 0, m.start()) + 1) for m in rx.finditer(readme)]

    found = scan(_WORD_HEADING_RE) or scan(_SHORT_HEADING_RE) or scan(_BULLET_RE)
    out, seen = [], set()
    for title, line in found:
        title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip().rstrip(".")
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"id": layer_id(title), "title": title, "line": line})
    return out


def split_block(body):
    """Separate a status body into `(prose, entries)`.

    `entries` is `[{id, state, remaining}]` from the trailing ```` ```coverage ```` block, or None
    when the body has no block. The block must be the last thing in the body: a block followed by
    more prose is refused, so a model cannot bury an assessment in the middle of a report where a
    reader would take it for prose.
    """
    opens = list(BLOCK_OPEN_RE.finditer(body))
    if not opens:
        return body, None
    if len(opens) > 1:
        raise files.FormatError("the status body has more than one ```coverage block")
    start = opens[0]
    close = BLOCK_CLOSE_RE.search(body, start.end())
    if close is None:
        raise files.FormatError("the ```coverage block is not closed")
    if body[close.end():].strip():
        raise files.FormatError("the ```coverage block must be the last thing in the status body")
    entries = []
    for raw in body[start.end():close.start()].splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            raise files.FormatError(
                f"coverage line {line!r} is not '<layer id>: <state>' or "
                f"'<layer id>: <state> — <what remains>'"
            )
        entries.append({
            "id": m.group("id").strip(),
            "state": m.group("state").lower(),
            "remaining": (m.group("remaining") or "").strip(),
        })
    return body[:start.start()].rstrip() + "\n", entries


def coverage(area, to_sha, readme_hash, layers, entries):
    """The validated `tauceti-coverage:v1` payload for a report, or raise.

    Every layer id the plan lists must appear exactly once with a legal state, and nothing else
    may appear: an assessment that names a layer the README does not have, or skips one it does,
    is refused whole rather than half-applied. Order follows the README, whatever order the model
    wrote.
    """
    ids = [layer["id"] for layer in layers]
    by_id = {}
    for entry in entries:
        if entry["id"] in by_id:
            raise files.FormatError(f"the coverage block names {entry['id']!r} twice")
        by_id[entry["id"]] = entry
    unknown = [i for i in by_id if i not in ids]
    missing = [i for i in ids if i not in by_id]
    if unknown:
        raise files.FormatError(
            f"the coverage block names layer(s) the README does not have: {unknown}; "
            f"the README's layer ids are {ids}"
        )
    if missing:
        raise files.FormatError(f"the coverage block says nothing about layer(s) {missing}")
    out = []
    for i in ids:
        entry = {"id": i, "state": by_id[i]["state"]}
        if by_id[i]["remaining"]:
            entry["remaining"] = by_id[i]["remaining"]
        out.append(entry)
    # `files` owns the wire format and refuses anything it would not accept from a pull request.
    return files.require_coverage(
        {"roadmap": area, "to_sha": to_sha, "readme_sha": readme_hash, "layers": out}, area, to_sha
    )
