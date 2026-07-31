"""The generated API documentation, read as the authority on what declarations exist.

This module replaced a hand-written Lean scanner, and the reason is worth recording so nobody
reintroduces one.

Deciding what a Lean file declares cannot be done by reading the text. Names are qualified by an
enclosing `namespace`, which interacts with `section`, with `end` closing either, with `open ... in`,
and with `_root_`; and many real declarations are never written down at all -- structure projections,
constructors, instances and `deriving` output are produced during elaboration. A Python
approximation of that gets *most* names right, which is the worst possible outcome: the wrong ones
are indistinguishable from the right ones, and a documentation link built from a wrong name is a
plausible-looking dead link. The first version of this code produced
`ContinuousLinearMap.IsFredholm.of_continuousLinearEquiv` for a declaration the compiler calls
`TauCeti.IsFredholm.of_continuousLinearEquiv`.

doc-gen4 already publishes the answer, computed from the elaborated environment:

* `declarations/declaration-data.bmp` -- every declaration, with its kind and the page it lives on.
* each module page -- for every declaration, a `gh_link` giving the exact source commit, file and
  line range.

So names, kinds, links and source positions are all read from there. What remains for this project's
own code is a question git can answer exactly -- "were these lines written during this window?" --
and reading a comment block that sits immediately above a line number the documentation supplied.
Neither requires knowing any Lean grammar.
"""

import json
import os
import pathlib
import re
import urllib.error
import urllib.request

DOCS_BASE = "https://taucetiproject.github.io/TauCeti/docs"
INDEX_PATH = "declarations/declaration-data.bmp"

# `<div class="decl" id="Full.Name">` opens a declaration; the `gh_link` inside it names the commit,
# file and lines. Both are doc-gen4's own markup, so this is reading a published format rather than
# guessing at one -- and `declarations()` fails loudly if the markup stops matching.
_DECL_RE = re.compile(r'<div class="decl" id="([^"]+)">')
_GH_LINK_RE = re.compile(
    r'<div class="gh_link"><a href="https://github\.com/[^/]+/[^/]+/blob/([0-9a-f]{40})/([^"#]+)#L(\d+)-L(\d+)"'
)
_KIND_RE = re.compile(r'<span class="decl_kind">([a-z ]+)</span>')


class DocsError(RuntimeError):
    """The documentation could not be read, or does not look like doc-gen4 output."""


class Docs:
    """A cached reader for one published documentation site."""

    def __init__(self, base=DOCS_BASE, cache_dir=None, opener=None):
        self.base = base.rstrip("/")
        self.cache_dir = pathlib.Path(
            cache_dir or os.environ.get("TAUCETI_DOCS_CACHE") or "/tmp/tauceti-docs-cache"
        )
        self._opener = opener or self._fetch
        self._index = None
        self._pages = {}
        self._source_commit = None

    # ----- transport -------------------------------------------------------------------------

    def _fetch(self, url):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise DocsError(f"fetching {url} failed: {exc}") from exc

    def _get(self, rel):
        """Fetch `rel` relative to the docs root, memoised in process and on disk.

        The published site is static, so caching is safe within a run; the cache is keyed by URL and
        is purely an optimisation for the bootstrap case, which reads many module pages.
        """
        if rel in self._pages:
            return self._pages[rel]
        path = self.cache_dir / rel.replace("/", "__")
        if path.is_file():
            text = path.read_text(encoding="utf-8")
        else:
            text = self._opener(f"{self.base}/{rel}")
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError:
                pass  # a cache that cannot be written is not an error
        self._pages[rel] = text
        return text

    # ----- the declaration index -------------------------------------------------------------

    def index(self):
        """`{full_name: {"kind", "docLink"}}` for every declaration the site documents."""
        if self._index is None:
            try:
                data = json.loads(self._get(INDEX_PATH))
            except json.JSONDecodeError as exc:
                raise DocsError(f"{INDEX_PATH} is not JSON: {exc}") from exc
            decls = data.get("declarations")
            if not isinstance(decls, dict) or not decls:
                raise DocsError(f"{INDEX_PATH} has no declarations map")
            self._index = decls
        return self._index

    def module_of(self, name):
        """The module page a declaration lives on, as a site-relative path, or None."""
        entry = self.index().get(name)
        if not entry:
            return None
        link = entry.get("docLink") or ""
        return link[2:].split("#", 1)[0] if link.startswith("./") else None

    # ----- module pages ----------------------------------------------------------------------

    def declarations(self, module_page):
        """Every declaration documented on a module page.

        Returns `{full_name: {"kind", "url", "file", "start", "end", "commit"}}`. A declaration with
        no `gh_link` (there are a few, for compiler-generated entries) is reported without a source
        position, and callers simply cannot decide whether it is new.
        """
        html = self._get(module_page)
        out = {}
        marks = [(m.start(), m.group(1)) for m in _DECL_RE.finditer(html)]
        if not marks:
            return out
        for i, (pos, name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(html)
            seg = html[pos:end]
            gh = _GH_LINK_RE.search(seg)
            kind = _KIND_RE.search(seg)
            out[name] = {
                "kind": (kind.group(1).strip() if kind else ""),
                "url": f"{self.base}/{module_page}#{name}",
                "commit": gh.group(1) if gh else None,
                "file": gh.group(2) if gh else None,
                "start": int(gh.group(3)) if gh else None,
                "end": int(gh.group(4)) if gh else None,
            }
        return out

    def source_commit(self, probe_module=None):
        """The TauCeti commit the published documentation was built from.

        Read from the site itself rather than assumed, because the docs deploy independently of the
        branch that nominates them: at the time of writing the published build was several commits
        behind the branch tip. Reporting links against the branch tip would then produce dead links
        for anything newer, so the commit stated by the documentation is what everything is anchored
        to.
        """
        if self._source_commit is None:
            module = probe_module or self._any_module()
            for info in self.declarations(module).values():
                if info["commit"]:
                    self._source_commit = info["commit"]
                    break
            else:
                raise DocsError(f"no source link found on {module}; cannot date the documentation")
        return self._source_commit

    def _any_module(self):
        """Some module page, for probing the build's source commit."""
        for name in self.index():
            page = self.module_of(name)
            if page:
                return page
        raise DocsError("the declaration index names no module pages")
