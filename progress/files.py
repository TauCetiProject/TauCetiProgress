"""The two generated file formats, and the validators the merge gate runs on them.

`STATUS.md` is a snapshot: rewritten whole on every update, headed by the commit it describes.
`PROGRESS.md` is an append-only log of windows, each section headed by the commit range it covers.

Both carry a machine-readable HTML-comment header followed by prose, following the
`tauceti-<kind>:v1 {json}` convention the rest of the project already uses for scoreboards and
target markers.

Everything here is pure: it takes and returns text, touches no network and no filesystem. That
matters because the merge gate in CI runs these same functions on an untrusted PR's blobs, and it
is the only thing standing between a model and a human-owned repository. The gate proves *shape*,
never truth -- see the trust-boundary section of README.md.
"""

import json
import re

# Marker names are part of the wire format; the gate rejects a model that emits any of them
# inside its prose, so bumping a version here is a coordinated change with the gate.
STATUS_MARKER = "tauceti-status:v1"
PROGRESS_MARKER = "tauceti-progress:v1"

# Any `tauceti-*:vN` marker at all. Model prose is checked against this, not just against the two
# markers above: prose that forges a *scoreboard* or *target* marker is equally unwanted, and a
# file that grows a second status header would confuse every later parse of it.
RESERVED_MARKER_RE = re.compile(r"<!--\s*tauceti-[a-z-]+:v\d+")

_HEADER_RE = re.compile(r"<!--\s*(tauceti-[a-z-]+:v\d+)\s*(\{.*?\})\s*-->", re.S)

# A short SHA is ambiguous and a 40-hex SHA is not, so the formats store full ones and abbreviate
# only for display.
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# Caps exist so a runaway model cannot commit a megabyte of prose, and so the gate's own work is
# bounded. A window's section is meant to be a few paragraphs; STATUS is a page.
MAX_STATUS_BYTES = 64 * 1024
MAX_SECTION_BYTES = 32 * 1024
MAX_PROGRESS_BYTES = 4 * 1024 * 1024


class FormatError(ValueError):
    """A generated file does not conform. Always fail closed on one of these: the gate refuses
    the PR rather than merging something it could not fully parse."""


def _require_sha(value, field):
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise FormatError(f"{field} must be a full 40-character lowercase hex SHA, got {value!r}")
    return value


def _require_area(value, field):
    # Areas are directory names in the roadmap repo; keep this strict so an area can never
    # contain a path separator and escape its directory.
    if not isinstance(value, str) or not re.match(r"\A[A-Za-z0-9]+\Z", value):
        raise FormatError(f"{field} must be an alphanumeric roadmap area name, got {value!r}")
    return value


def parse_headers(text, marker):
    """Every `marker` header in `text`, in order, as a list of dicts.

    Used both to read one status header and to read every section header of a progress log.
    """
    out = []
    for found_marker, payload in _HEADER_RE.findall(text):
        if found_marker != marker:
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FormatError(f"malformed {marker} header JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise FormatError(f"{marker} header must be a JSON object, got {type(obj).__name__}")
        out.append(obj)
    return out


# ----- STATUS.md -------------------------------------------------------------------------------


def render_status(area, to_sha, ts, body):
    """A whole `STATUS.md`. `body` is the model's prose, without any heading of its own.

    The prose is deliberately preceded by a standing note that the file may be out of date: it is
    updated asynchronously from the PRs it describes, so a reader must never take it as
    authoritative about the current tip.
    """
    header = json.dumps(
        {"roadmap": _require_area(area, "roadmap"), "to_sha": _require_sha(to_sha, "to_sha"), "ts": ts},
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"<!--{STATUS_MARKER} {header}-->\n"
        f"# Status: {area}\n\n"
        f"This file documents the status of the {area} roadmap up until "
        f"`{to_sha[:7]}` ({ts}). There may have been subsequent updates.\n\n"
        f"It is generated, and its prose is not security-validated; see\n"
        f"https://github.com/TauCetiProject/TauCetiProgress for what that means.\n\n"
        f"{body.strip()}\n"
    )


def parse_status(text):
    """The header of a `STATUS.md`. Raises unless there is exactly one."""
    headers = parse_headers(text, STATUS_MARKER)
    if len(headers) != 1:
        raise FormatError(f"expected exactly one {STATUS_MARKER} header, found {len(headers)}")
    h = headers[0]
    return {
        "roadmap": _require_area(h.get("roadmap"), "roadmap"),
        "to_sha": _require_sha(h.get("to_sha"), "to_sha"),
        "ts": h.get("ts"),
    }


# ----- PROGRESS.md -----------------------------------------------------------------------------


def render_section(area, from_sha, to_sha, prs, window_label, body):
    """One `PROGRESS.md` section, ready to append.

    `prs` is the full PR-number list for the window. It is recorded so that a later run can refuse
    to report a PR twice even if its `roadmap/<Area>` label is changed after the fact -- labels are
    mutable metadata, and re-attribution must not silently double-count or drop work.
    """
    nums = sorted({int(n) for n in prs})
    if not nums:
        raise FormatError("a progress section must record at least one PR")
    header = json.dumps(
        {
            "roadmap": _require_area(area, "roadmap"),
            "from_sha": _require_sha(from_sha, "from_sha"),
            "to_sha": _require_sha(to_sha, "to_sha"),
            "prs": nums,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"\n<!--{PROGRESS_MARKER} {header}-->\n"
        f"## {area}: {window_label} (`{from_sha[:7]}` to `{to_sha[:7]}`)\n\n"
        f"{body.strip()}\n"
    )


def new_progress_file(area):
    """The preamble a fresh `PROGRESS.md` starts with, before its first section.

    Sections are appended below, oldest first, so that "this update only added text at the end" is
    checkable as a byte-prefix comparison. See `check_append_only`.
    """
    return (
        f"# Progress log: {area}\n\n"
        f"An append-only record of what landed on the {area} roadmap, one section per window of\n"
        f"merged pull requests, oldest first. Generated; the prose is not security-validated.\n"
        f"For a current snapshot instead, read `STATUS.md` beside this file.\n"
    )


def parse_sections(text):
    """Every section header of a `PROGRESS.md`, oldest first."""
    out = []
    for h in parse_headers(text, PROGRESS_MARKER):
        out.append(
            {
                "roadmap": _require_area(h.get("roadmap"), "roadmap"),
                "from_sha": _require_sha(h.get("from_sha"), "from_sha"),
                "to_sha": _require_sha(h.get("to_sha"), "to_sha"),
                "prs": [int(n) for n in h.get("prs") or []],
            }
        )
    return out


def cursor(text):
    """The reporting cursor: the `to_sha` of the newest section, or None for an empty log.

    This single value is the cursor for an area. `STATUS.md` carries a `to_sha` too, but only as a
    snapshot label -- treating it as a second cursor is what would let a STATUS-only update advance
    past a window whose prose was never written, leaving an unreportable gap.
    """
    sections = parse_sections(text)
    return sections[-1]["to_sha"] if sections else None


def reported_prs(text):
    """Every PR number any section of this log has already reported."""
    seen = set()
    for s in parse_sections(text):
        seen.update(s["prs"])
    return seen


# ----- validators the merge gate runs ----------------------------------------------------------


def check_append_only(old_text, new_text):
    """`new_text` must be `old_text` plus trailing bytes, and must actually add some.

    This is the whole reason sections append at the bottom: the property is one byte-prefix
    comparison, with no reasoning about diff hunks, line endings or whitespace. Newest-first
    ordering would be checkable too (old bytes as an unchanged suffix), but this is the version it
    is hardest to get subtly wrong.
    """
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise FormatError("append-only check needs text on both sides")
    if not new_text.startswith(old_text):
        raise FormatError("PROGRESS.md was modified above the end; only appending is allowed")
    if len(new_text) == len(old_text):
        raise FormatError("PROGRESS.md is unchanged; an update must add a section")
    return new_text[len(old_text):]


def check_no_reserved_markers(body, allow=()):
    """Refuse model prose that contains any `tauceti-*:vN` marker.

    A model that emits one could forge a second status header, a fake scoreboard, or a target
    marker, and every later parse of the file would then see something the generator never
    intended. `allow` exempts the headers this update legitimately introduces.
    """
    for m in RESERVED_MARKER_RE.finditer(body):
        start = m.start()
        if any(body[start:].startswith(f"<!--{name}") for name in allow):
            continue
        raise FormatError(f"model prose contains a reserved marker at offset {start}: {m.group(0)!r}")


def check_size(name, text, cap):
    n = len(text.encode("utf-8"))
    if n > cap:
        raise FormatError(f"{name} is {n} bytes, over the {cap}-byte cap")
    return n


def check_utf8(name, data):
    """`data` is bytes as GitHub returned them; reject anything that is not valid UTF-8."""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError(f"{name} is not valid UTF-8: {exc}") from exc


def validate_update(area, old_status, new_status, old_progress, new_progress, expect_from_sha=None):
    """The full content gate for one generated update. Returns the new section's header.

    Checks, in order: both files parse; the status snapshot and the new section agree on area and
    `to_sha`; the progress log is a byte-exact append that adds exactly one section; the new
    section's `from_sha` continues the log (and matches `expect_from_sha` when the caller knows
    it); no reserved markers appear in the added prose; sizes are within caps.

    `old_status` may be None for an area's first update; `old_progress` may be None likewise, in
    which case `new_progress` must begin with a fresh preamble rather than a section.
    """
    check_size("STATUS.md", new_status, MAX_STATUS_BYTES)
    check_size("PROGRESS.md", new_progress, MAX_PROGRESS_BYTES)

    status = parse_status(new_status)
    if status["roadmap"] != area:
        raise FormatError(f"STATUS.md is for {status['roadmap']}, expected {area}")

    if old_progress is None:
        old_progress = new_progress_file(area)
    added = check_append_only(old_progress, new_progress)

    before = parse_sections(old_progress)
    after = parse_sections(new_progress)
    if len(after) != len(before) + 1:
        raise FormatError(
            f"expected exactly one new section, went from {len(before)} to {len(after)}"
        )
    section = after[-1]

    if section["roadmap"] != area:
        raise FormatError(f"new section is for {section['roadmap']}, expected {area}")
    if section["to_sha"] != status["to_sha"]:
        raise FormatError(
            f"STATUS.md is at {status['to_sha'][:7]} but the new section ends at "
            f"{section['to_sha'][:7]}; a snapshot must describe the window it ships with"
        )
    prior_cursor = before[-1]["to_sha"] if before else None
    if prior_cursor is not None and section["from_sha"] != prior_cursor:
        raise FormatError(
            f"new section starts at {section['from_sha'][:7]} but the log's cursor is "
            f"{prior_cursor[:7]}; windows must tile with no gap"
        )
    if expect_from_sha is not None and section["from_sha"] != expect_from_sha:
        raise FormatError(
            f"new section starts at {section['from_sha'][:7]}, expected {expect_from_sha[:7]}"
        )
    if section["from_sha"] == section["to_sha"]:
        raise FormatError("a window must be non-empty (from_sha equals to_sha)")
    if not section["prs"]:
        raise FormatError("new section records no PRs")

    check_size("the new section", added, MAX_SECTION_BYTES)
    check_no_reserved_markers(added, allow=(PROGRESS_MARKER,))
    # The status file is rewritten whole, so its one legitimate header is the status marker.
    check_no_reserved_markers(new_status, allow=(STATUS_MARKER,))

    if old_status is not None:
        old = parse_status(old_status)
        if old["to_sha"] == status["to_sha"]:
            raise FormatError(f"STATUS.md still describes {status['to_sha'][:7]}; nothing advanced")

    return section
