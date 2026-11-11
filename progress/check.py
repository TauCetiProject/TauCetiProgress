"""Check a report's two prose bodies before anything is committed: `tauceti-progress check`.

`apply` refuses a report whose files are malformed. This holds a well-formed one to what the writing
prompt asks and to the material the model was given:

* **The prompt's limits.** 750 and 300 words, three paragraphs, the two fixed `##` headings. `files`
  enforces only the hard ceilings of 900 and 450 words.
* **Links the model built.** Every documentation URL must be copied from the facts file or from the
  roadmap's previous STATUS.md or PROGRESS.md.
* **Links that died.** A URL carried over from an old STATUS.md can name a declaration that has since
  been renamed or moved, so each one is looked up in the plan's pinned documentation facts.
* **Assessments lost.** A layer the previous report judged `done`, `partial` or `untouched` must not
  become `unassessed` against the same README, and a report must assess at least one layer.

The writing model can run this itself and fix what it reports. Warnings do not fail the check.
"""

import json
import pathlib
import re
import urllib.parse

from . import apply as apply_mod
from . import files, layers as layers_mod
from .docs import DOCS_BASE

# The limits `prompts/progress.md` states, stricter than the ceilings in `files`.
STATUS_WORDS = 750
SECTION_WORDS = 300
SECTION_PARAGRAPHS = 3
STATUS_HEADINGS = ["## Where this roadmap stands", "## The frontier"]

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"`]*[^\s<>()\[\]\"`.,;:!?]")
# A link destination ends at its `)`, so a trailing `?` or `!` is part of a declaration name there.
LINK_RE = re.compile(r"\]\((https?://[^\s()<>]+)\)")
ASSESSED = ("done", "partial", "untouched")


def urls(text):
    """The URLs in Markdown `text`: each link destination as written, and any other URL without the
    punctuation that ends a sentence."""
    return set(LINK_RE.findall(text)) | set(URL_RE.findall(LINK_RE.sub("]()", text)))


def supplied_urls(facts, old_status, old_progress):
    """The URLs the model was given: each string in the facts that is a URL, the URLs in the facts'
    docstrings, and those in the previous report."""
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            if re.fullmatch(r"https?://\S+", value):
                found.add(value)
            found.update(urls(value))

    walk(facts)
    return found | urls(old_status or "") | urls(old_progress or "")


def link_problem(url, docs):
    """Why a declaration link is dead in the pinned facts, or None.

    The module is found by the URL's path suffix, so a link on a host the documentation used to be
    served from is checked like any other. A link to some other site matches no module and passes.
    """
    parsed = urllib.parse.urlsplit(url)
    if not parsed.fragment:
        return None
    parts = urllib.parse.unquote(parsed.path).split("/")
    modules = set(docs.modules)
    for start in range(1, len(parts)):
        page = "/".join(parts[start:])
        if page.endswith(".html") and page[:-len(".html")] + ".lean" in modules:
            names = docs.declarations(page)
            if parsed.fragment in names or urllib.parse.unquote(parsed.fragment) in names:
                return None
            return "no such declaration in its module"
    if url.startswith(docs.base + "/") and parsed.path.endswith(".html"):
        return "no such module in the documented build"
    return None


def coverage_states(status_text):
    """`{roadmap: (readme_sha, {layer_id: state})}` from a STATUS.md's coverage headers."""
    return {
        header.get("roadmap"): (header.get("readme_sha"),
                                {entry.get("id"): entry.get("state") for entry in header.get("layers") or []})
        for header in files.parse_headers(status_text or "", files.COVERAGE_MARKER)
    }


def coverage_problems(old_status, new_status):
    """`(failures, warnings)` comparing the new coverage headers with the previous report's."""
    failures, warnings = [], []
    new = coverage_states(new_status)
    try:
        old = coverage_states(old_status)
    except files.FormatError:
        old = {}
    states = [state for _sha, entries in new.values() for state in entries.values()]
    if states and all(state == "unassessed" for state in states):
        failures.append(
            f"every one of the {len(states)} layers is `unassessed`; judge each layer from the facts, "
            f"the previous reports and the README (`untouched` when nothing for it has landed)")
    for roadmap, (sha, entries) in sorted(new.items()):
        before = old.get(roadmap)
        if before is None or before[0] != sha:
            continue  # a changed README may have changed its layers; nothing carries forward
        for layer, state in entries.items():
            previous = before[1].get(layer)
            if previous in ASSESSED and state == "unassessed":
                failures.append(
                    f"{roadmap} {layer} was `{previous}` in the previous report and is now `unassessed`; "
                    f"carry the earlier verdict forward unless this window gives a reason to change it")
            elif previous == "done" and state in ("partial", "untouched"):
                warnings.append(f"{roadmap} {layer} moves from `done` to `{state}`; make sure the prose says why")
    return failures, warnings


def run_checks(plan, facts, status_body, section_body, old_status, old_progress, docs=None):
    """`(failures, warnings, info)`, each a list of lines. `docs` is the plan's pinned `Docs`; without
    it links are checked for provenance only."""
    failures, warnings, info = [], [], []
    try:
        status_text, _progress_text, _header = apply_mod.render_update(
            plan, status_body, section_body, old_status, old_progress)
    except files.FormatError as exc:
        return [f"the report does not render: {exc}"], warnings, info

    prose, _block = layers_mod.split_block(status_body)
    status_words, section_words = len(prose.split()), len(section_body.split())
    paragraphs = len([p for p in re.split(r"\n\s*\n", section_body.strip()) if p.strip()])
    info.append(f"status prose: {status_words} words (at most {STATUS_WORDS})")
    info.append(f"section: {section_words} words in {paragraphs} paragraph(s) "
                f"(at most {SECTION_WORDS} in {SECTION_PARAGRAPHS})")
    if status_words > STATUS_WORDS:
        failures.append(f"the status prose is {status_words} words; the limit is {STATUS_WORDS}")
    if section_words > SECTION_WORDS:
        failures.append(f"the section is {section_words} words; the limit is {SECTION_WORDS}")
    if paragraphs > SECTION_PARAGRAPHS:
        failures.append(f"the section has {paragraphs} paragraphs; the limit is {SECTION_PARAGRAPHS}")
    headings = [h.strip() for h in re.findall(r"^## .*$", prose, re.M)]
    if headings != STATUS_HEADINGS:
        failures.append(f"the status prose's `##` headings are {headings}; they must be exactly {STATUS_HEADINGS}")
    if re.search(r"^# ", status_body + "\n" + section_body, re.M):
        failures.append("a body has a top-level `# ` heading; the scripts add those")

    base = ((plan.get("documentation") or {}).get("docs_base") or DOCS_BASE).rstrip("/") + "/"
    supplied = supplied_urls(facts, old_status, old_progress)
    for url in sorted(urls(status_body + "\n" + section_body)):
        if url not in supplied:
            if url.startswith(base):
                failures.append(f"documentation URL not copied from the facts or the previous report: {url}")
        elif docs is not None and (why := link_problem(url, docs)):
            failures.append(f"dead documentation link ({why}): {url}")

    coverage_failures, coverage_warnings = coverage_problems(old_status, status_text)
    return failures + coverage_failures, warnings + coverage_warnings, info


def run(plan, facts_file, status_body_file, section_body_file, roadmap_dir, docs=None):
    """Print every finding and return the exit code: 0 when the report passes, 1 otherwise."""
    def read(path):
        path = pathlib.Path(path)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    facts = json.loads(pathlib.Path(facts_file).read_text(encoding="utf-8"))
    status_body, section_body = read(status_body_file), read(section_body_file)
    missing = [str(path) for path, text in ((status_body_file, status_body), (section_body_file, section_body))
               if not (text or "").strip()]
    if missing:
        failures, warnings, info = [f"missing or empty: {', '.join(missing)}"], [], []
    else:
        roadmap = pathlib.Path(roadmap_dir)
        failures, warnings, info = run_checks(
            plan, facts, status_body, section_body, read(roadmap / plan["status_path"]),
            read(roadmap / plan["progress_path"]), docs=docs)
    for line in info:
        print(line)
    for line in warnings:
        print(f"WARN: {line}")
    for line in failures:
        print(f"FAIL: {line}")
    print("NOT OK" if failures else "OK")
    return 1 if failures else 0
