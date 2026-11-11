"""Tests for `tauceti-progress check`: the prompt's limits, link provenance, dead links, and
carried-forward assessments. Links are looked up in the producer fixture bundle, read by the real
`Docs`."""

import contextlib
import gzip
import io
import json
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import apply as apply_mod, check, cli  # noqa: E402
from progress.docs import Docs  # noqa: E402

failures = []


def run_test(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


TMP = tempfile.TemporaryDirectory(prefix="check-")
ROOT = pathlib.Path(TMP.name)
BASE = "https://taucetiproject.org/docs"
BUNDLE = gzip.compress((pathlib.Path(__file__).parent / "fixtures/pages-facts/facts.json").read_bytes(), mtime=0)
DOCS = Docs(base=BASE, cache_dir=ROOT / "cache", raw=BUNDLE)

Z, A, B = "0" * 40, "a1b2c3d" + "0" * 33, DOCS.source_commit()
LAYERS = [{"id": "Layer 0", "title": "Layer 0: curves", "line": 10},
          {"id": "Layer 1", "title": "Layer 1: cycles", "line": 20}]
README_SHA = "1" * 64
URL = f"{BASE}/Demo/A.html#Demo.alpha"
OLD_URL = f"{BASE}/Demo/Sub/B.html#Demo.Sub.beta"
FACTS = {"declarations": [{"name": "Demo.alpha", "url": URL}]}


def plan(from_sha, to_sha, readme_sha=README_SHA):
    return {"roadmap": "Demo", "rel_dir": "TauCetiRoadmap/Demo", "from_sha": from_sha, "to_sha": to_sha,
            "prs": [966, 1244], "from_date": "2026-07-28T09:11:00+00:00", "to_date": "2026-07-30T11:34:41+00:00",
            "status_path": "TauCetiRoadmap/Demo/STATUS.md", "progress_path": "TauCetiRoadmap/Demo/PROGRESS.md",
            "layers": LAYERS, "readme_sha": readme_sha, "documentation": DOCS.pin()}


def block(first, second):
    return ('\n\n```coverage\n[{"id": "Layer 0", "state": "%s"}, {"id": "Layer 1", "state": "%s"}]\n```\n'
            % (first, second))


def status(link=URL, extra=""):
    return (
        "## Where this roadmap stands\n\n**At a glance.** Harnack's inequality for nonnegative harmonic "
        f"functions on a disc is proved, as [the Harnack inequality]({link}). The mean-value machinery "
        "it rests on is in place, and the homological Cauchy theorem has not begun.\n\n"
        "## The frontier\n\n- **The homological version** remains: cycles and winding numbers.\n" + extra
    )


SECTION = (f"Harnack's inequality landed, as [the Harnack inequality]({URL}), in both the two-sided "
           "comparison with the centre value and the pairwise form on a closed subdisc. The supporting "
           "mean-value lemmas were restated for closed discs along the way.\n")


def previous(first="done", second="partial", readme_sha=README_SHA, link=OLD_URL):
    """The previous report's STATUS.md and PROGRESS.md, rendered by the real renderer."""
    status_text, progress_text, _ = apply_mod.render_update(
        plan(Z, A, readme_sha), status(link) + block(first, second), SECTION, None, None)
    return status_text, progress_text


def run(status_body, section=SECTION, old=(None, None), docs=DOCS, readme_sha=README_SHA, facts=FACTS):
    return check.run_checks(plan(Z if old[0] is None else A, B, readme_sha), facts, status_body, section,
                            *old, docs=docs)


def docs_with(*names):
    """The fixture bundle with further declarations in `Demo/A.lean`, each at its literal URL."""
    data = json.loads((pathlib.Path(__file__).parent / "fixtures/pages-facts/facts.json").read_text())
    alpha = data["modules"]["Demo/A.lean"]["Demo.alpha"]
    for name in names:
        data["modules"]["Demo/A.lean"][name] = {**alpha, "url": f"{BASE}/Demo/A.html#{name}"}
    raw = gzip.compress(json.dumps(data).encode("utf-8"), mtime=0)
    return Docs(base=BASE, cache_dir=tempfile.mkdtemp(dir=ROOT), raw=raw)


def test_a_sound_report_passes():
    fails, warns, info = run(status() + block("done", "partial"), old=previous())
    assert fails == [] and warns == [], (fails, warns)
    assert any("status prose" in line for line in info)


def test_the_limits_are_the_ones_the_prompt_states():
    prompt = (cli.PROMPT_DIR / "progress.md").read_text(encoding="utf-8")
    assert f"At most {check.SECTION_WORDS} words, in at most three paragraphs" in prompt
    assert f"At most {check.STATUS_WORDS} words" in prompt and check.SECTION_PARAGRAPHS == 3
    assert all(f"`{heading}`" in prompt for heading in check.STATUS_HEADINGS)


def test_the_prompts_limits_are_enforced():
    fails, *_ = run(status() + block("done", "partial"), section=" ".join(["word"] * 320) + "\n")
    assert any("the section is 320 words" in f for f in fails), fails
    fails, *_ = run(status() + block("done", "partial"), section="One.\n\nTwo.\n\nThree.\n\nFour.\n" * 20)
    assert any("paragraphs; the limit is 3" in f for f in fails), fails
    long_status = status(extra="\n".join(["- more " * 20] * 19) + "\n")
    fails, *_ = run(long_status + block("done", "partial"))
    assert any("the status prose is" in f for f in fails), fails
    fails, *_ = run(status().replace("## The frontier", "## What is next") + block("done", "partial"))
    assert any("`##` headings" in f for f in fails), fails


def test_a_built_url_is_refused():
    made_up = f"{BASE}/Demo/A.html#Demo.alpha_strong"
    fails, *_ = run(status(link=made_up) + block("done", "partial"))
    assert any("not copied" in f and made_up in f for f in fails), fails
    elsewhere = "https://example.org/paper.html#section-2"
    fails, *_ = run(status(link=elsewhere) + block("done", "partial"))
    assert fails == [], "a link to another site is not a documentation link"


def test_a_link_destination_keeps_a_trailing_question_or_exclamation_mark():
    docs = docs_with("Demo.alpha?", "Demo.alpha!")
    for suffix in "?!":
        fails, *_ = run(status(link=URL + suffix) + block("done", "partial"), docs=docs)
        assert any("not copied" in f and URL + suffix in f for f in fails), fails
        canonical = {"declarations": [{"name": "Demo.alpha" + suffix, "url": URL + suffix}]}
        assert run(status(link=URL + suffix) + block("done", "partial"), docs=docs, facts=canonical)[0] == []
        fails, *_ = run(status(link=URL + suffix) + block("done", "partial"), facts=canonical)
        assert any("dead documentation link" in f and URL + suffix in f for f in fails), fails
    prose = f"See {URL}? It is the main result.\n"
    assert check.urls(prose) == {URL} and check.urls(f"[a]({URL}?)") == {URL + "?"}


def test_a_url_from_the_previous_report_is_allowed_but_must_still_exist():
    assert run(status(link=OLD_URL) + block("done", "partial"), old=previous())[0] == []
    renamed = f"{BASE}/Demo/Sub/B.html#Demo.Sub.beta_old"
    fails, *_ = run(status(link=renamed) + block("done", "partial"), old=previous(link=renamed))
    assert any("dead documentation link (no such declaration" in f and renamed in f for f in fails), fails
    moved = f"{BASE}/Demo/Gone.html#Demo.Sub.beta"
    fails, *_ = run(status(link=moved) + block("done", "partial"), old=previous(link=moved))
    assert any("dead documentation link (no such module" in f for f in fails), fails
    assert run(status(link=moved) + block("done", "partial"), old=previous(link=moved), docs=None)[0] == [], \
        "without the pinned facts only provenance is checked"


def test_a_link_on_a_former_documentation_host_is_checked_by_its_module_path():
    former = OLD_URL.replace(BASE, "https://docs.example.org/Project/docs")
    assert run(status(link=former) + block("done", "partial"), old=previous(link=former))[0] == []
    renamed = former + "_old"
    fails, *_ = run(status(link=renamed) + block("done", "partial"), old=previous(link=renamed))
    assert any("dead documentation link" in f and renamed in f for f in fails), fails


def test_an_assessed_layer_may_not_become_unassessed():
    fails, *_ = run(status() + block("done", "unassessed"), old=previous())
    assert any("Layer 1 was `partial`" in f for f in fails), fails
    assert run(status() + block("done", "unassessed"), old=previous(readme_sha="2" * 64))[0] == [], \
        "a changed README frees the assessments"
    fails, *_ = run(status() + block("unassessed", "unassessed"))
    assert any("every one of the 2 layers" in f for f in fails), fails


def test_a_done_layer_moving_down_is_a_warning():
    fails, warns, _ = run(status() + block("partial", "partial"), old=previous())
    assert fails == [] and any("Layer 0 moves from `done` to `partial`" in w for w in warns), (fails, warns)


def test_a_report_that_does_not_render_says_so():
    fails, *_ = run(status())  # the plan lists layers, and there is no coverage block
    assert len(fails) == 1 and "does not render" in fails[0], fails


def test_the_command_reads_the_bundle_retained_beside_the_plan_and_exits_1_on_a_finding():
    work, roadmap = ROOT / "work", ROOT / "roadmap"
    area = roadmap / "TauCetiRoadmap/Demo"
    area.mkdir(parents=True)
    work.mkdir()
    for name, text in zip(("STATUS.md", "PROGRESS.md"), previous()):
        (area / name).write_text(text, encoding="utf-8")
    (work / "bundle.json.gz").write_bytes(BUNDLE)
    saved = {**plan(A, B), "documentation": {**DOCS.pin(), "bundle_file": "bundle.json.gz"}}
    (work / "plan.json").write_text(json.dumps(saved), encoding="utf-8")
    (work / "facts.json").write_text(json.dumps(FACTS), encoding="utf-8")
    (work / "section.md").write_text(SECTION, encoding="utf-8")

    def command(status_body, *extra):
        (work / "status.md").write_text(status_body, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
             patch.dict(os.environ, {"TAUCETI_FACTS_CACHE": str(ROOT / "empty-cache")}):
            rc = cli.main(["check", "--plan", str(work / "plan.json"), "--facts", str(work / "facts.json"),
                           "--status-body", str(work / "status.md"), "--section-body", str(work / "section.md"),
                           "--roadmap-dir", str(roadmap), *extra])
        return rc, out.getvalue()

    def offline(url):
        raise AssertionError(f"unexpected fetch of {url}")

    original, Docs._fetch = Docs._fetch, staticmethod(offline)
    try:
        rc, out = command(status(link=OLD_URL) + block("done", "partial"))
        assert rc == 0 and out.endswith("\nOK\n") and "FAIL" not in out, out
        rc, out = command(status() + block("done", "unassessed"))
        assert rc == 1 and "FAIL: Demo Layer 1 was `partial`" in out and out.endswith("\nNOT OK\n"), out
        rc, out = command("")
        assert rc == 1 and "FAIL: missing or empty" in out, out
        (work / "bundle.json.gz").write_bytes(b"not the pinned bundle")
        assert command(status() + block("done", "partial"), "--no-links")[0] == 0
    finally:
        Docs._fetch = original


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        run_test(_name, _fn)

TMP.cleanup()
print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
