"""Tests for reading doc-gen4's published output.

The fixture is a verbatim slice of a real module page, so these test the markup as it actually is
rather than as it was imagined. If doc-gen4 changes its markup these fail loudly, which is the point:
silently extracting nothing would mean reports that quietly stop naming results.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import docs as docs_mod  # noqa: E402
from progress.docs import Docs, DocsError  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def make(pages, cache=None):
    """A Docs whose transport serves canned responses and never touches the network."""
    def opener(url):
        rel = url.split("/docs/", 1)[1]
        if rel not in pages:
            raise DocsError(f"no such page: {rel}")
        return pages[rel]
    return Docs(base="https://example.test/docs", cache_dir=cache or "/nonexistent", opener=opener)


PAGE = (FIXTURES / "module-page.html").read_text(encoding="utf-8")
INDEX = json.dumps({
    "declarations": {
        "TauCeti.IsFredholm": {"docLink": "./TauCeti/Analysis/Fredholm/Basic.html#TauCeti.IsFredholm",
                               "kind": "structure"},
    },
    "modules": {},
})


def test_declarations_are_read_from_real_markup():
    d = make({"TauCeti/Analysis/Fredholm/Basic.html": PAGE})
    got = d.declarations("TauCeti/Analysis/Fredholm/Basic.html")
    assert "TauCeti.IsFredholm" in got, sorted(got)
    e = got["TauCeti.IsFredholm"]
    assert e["kind"] == "structure", e
    assert e["file"] == "TauCeti/Analysis/Fredholm/Basic.lean", e
    assert e["start"] == 61 and e["end"] == 73, e
    assert len(e["commit"]) == 40, e
    assert e["url"].endswith("Basic.html#TauCeti.IsFredholm"), e


def test_every_declaration_block_is_found():
    d = make({"p.html": PAGE})
    got = d.declarations("p.html")
    assert len(got) == PAGE.count('<div class="decl" id="'), (len(got), PAGE.count('<div class="decl" id="'))


def test_source_commit_comes_from_the_page():
    d = make({"TauCeti/Analysis/Fredholm/Basic.html": PAGE, docs_mod.INDEX_PATH: INDEX})
    assert d.source_commit() == "ed837d596f81c587c5b9696efed02a869f945e7e", d.source_commit()


def test_index_is_parsed_and_maps_names_to_pages():
    d = make({docs_mod.INDEX_PATH: INDEX})
    assert d.module_of("TauCeti.IsFredholm") == "TauCeti/Analysis/Fredholm/Basic.html"
    assert d.module_of("Nope.Missing") is None


def test_a_malformed_index_is_refused():
    for bad in ("not json", json.dumps({"declarations": {}}), json.dumps({"modules": {}})):
        d = make({docs_mod.INDEX_PATH: bad})
        try:
            d.index()
        except DocsError:
            continue
        raise AssertionError(f"expected a DocsError for {bad[:30]!r}")


def test_a_page_with_no_declarations_yields_nothing():
    d = make({"empty.html": "<html><body>nothing here</body></html>"})
    assert d.declarations("empty.html") == {}


def test_markup_that_stops_matching_is_visible():
    """A page whose decl blocks carry no source link still lists the declarations, without a
    position -- so the caller cannot decide they are new, rather than guessing that they are."""
    d = make({"p.html": '<div class="decl" id="A.b"><span class="decl_kind">theorem</span></div>'})
    got = d.declarations("p.html")
    assert got["A.b"]["start"] is None and got["A.b"]["commit"] is None, got


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
