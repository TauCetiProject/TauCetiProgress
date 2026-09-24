"""A roadmap's layers, and the coverage block a status body ends with.

A roadmap README names the units of its plan as headings (`### Layer 3: ...`, `## Lane G: ...`,
`### Part A — ...`, `### Stage 2: ...`, or short labels such as `### L0A — ...`). A status report
assesses each of them, and `apply` turns that assessment into the `tauceti-coverage:v1` header so a
script can aggregate it across roadmaps. The extraction rule is the consumer's
(`scripts/roadmap_progress.py` in the TauCeti repository); the schema is `files.require_coverage`.

Pure: text in, values out.
"""

import hashlib
import json
import re

from . import files

# Worded headings win outright: a roadmap with `## Part A` tends to have `### A1` sub-headings that
# are milestones, not layers. Short labels count only where the README names layers that way
# throughout; bold bullets only when there are no layer headings at all.
_WORD_LEAD = r"(?:Layer|Lane|Part|Stage|Milestone)\b"
_SHORT_LEAD = r"[A-Z]\d+[A-Za-z]?(?=\s*[:—–,])"
_LAYER_LEAD = rf"(?:{_WORD_LEAD}|{_SHORT_LEAD})"
_WORD_HEADING_RE = re.compile(rf"^#{{2,4}}\s+({_WORD_LEAD}.*?)\s*$", re.M)
_SHORT_HEADING_RE = re.compile(rf"^#{{2,4}}\s+({_SHORT_LEAD}.*?)\s*$", re.M)
_BULLET_RE = re.compile(rf"^- \*\*({_LAYER_LEAD}[^*]*?)\*\*", re.M)
_ID_RE = re.compile(rf"^({_LAYER_LEAD}[^:—–,(]*?)\s*(?:[:—–,(]|$)")

# The block the model appends to its status prose: a named fence, so plain code in the prose is
# never mistaken for it, holding a JSON array of `{"id", "state", "remaining"?}` objects.
BLOCK_OPEN_RE = re.compile(r"^```coverage[ \t]*$", re.M)
BLOCK_CLOSE_RE = re.compile(r"^```[ \t]*$", re.M)


def readme_sha(text):
    """SHA-256 of the README's text: the identity of the specification an assessment was made
    against. Layer ids alone are not one; requirements change under unchanged headings."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def layer_id(title):
    """`Layer 3` from `Layer 3: the widget`; the label before the first separator."""
    m = _ID_RE.match(title)
    return (m.group(1) if m else title).strip()


def headings(readme):
    """`[{id, title, line}]` for a README's layer headings, in order, 1-based lines, trailing
    parentheticals dropped. Empty when the README names its milestones some other way."""
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
    """`(prose, entries)`: the status body without its trailing ```` ```coverage ```` block, and the
    block's decoded JSON, or `(body, None)` when there is no block.

    The block must be closed, the only one, and the last thing in the body, so an assessment can
    never sit where a reader takes it for prose. A block that is present but not JSON is an error,
    never "no coverage"; what the JSON must contain is `files.require_coverage`'s business.
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
    try:
        entries = json.loads(body[start.end():close.start()])
    except json.JSONDecodeError as exc:
        raise files.FormatError(f"the ```coverage block is not valid JSON: {exc}") from exc
    # `None` is how this function says "no block", so a block holding JSON `null` must not decode
    # to it: the caller would take a present, malformed block for an absent one and drop it.
    if entries is None:
        raise files.FormatError("the ```coverage block holds JSON null, not a list of layers")
    return body[:start.start()].rstrip() + "\n", entries


def coverage(area, to_sha, readme_hash, layers, entries):
    """The validated `tauceti-coverage:v1` payload for a report, or raise.

    The model supplied only `entries`; the roadmap, commit and README hash come from the plan, and
    `files.require_coverage` (the gate's validator) decides the shape. What the gate cannot check,
    because it has no README, is checked here: every layer the plan lists appears exactly once and
    nothing else does. Entries are returned in the README's order, whatever order the model wrote.
    """
    validated = files.require_coverage(
        {"roadmap": area, "to_sha": to_sha, "readme_sha": readme_hash, "layers": entries}, area, to_sha
    )
    ids = [layer["id"] for layer in layers]
    by_id = {entry["id"]: entry for entry in validated["layers"]}  # ids are unique: validated above
    unknown = [i for i in by_id if i not in ids]
    missing = [i for i in ids if i not in by_id]
    if unknown:
        raise files.FormatError(
            f"the coverage block names layer(s) the README does not have: {unknown}; "
            f"the README's layer ids are {ids}"
        )
    if missing:
        raise files.FormatError(f"the coverage block says nothing about layer(s) {missing}")
    validated["layers"] = [by_id[i] for i in ids]
    return validated
