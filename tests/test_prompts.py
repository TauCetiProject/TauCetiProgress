"""Tests for prompt ownership: one copy, shipped with the code that checks its output."""

import io
import contextlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from progress import cli, files  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def test_the_progress_prompt_prints():
    rc, out = run("prompt", "progress")
    assert rc == 0 and "roadmap of Tau Ceti" in out


def test_the_status_prompt_prints():
    rc, out = run("prompt", "status")
    assert rc == 0 and out.strip()


def test_a_missing_prompt_is_an_error_not_an_empty_success():
    rc, out = run("prompt", "nope")
    assert rc == 1 and out == ""


def test_prompts_live_inside_the_package():
    """They are fetched from an INSTALLED build, not a checkout.

    `package-data` once pointed at `../prompts/*.md`, which setuptools does not reliably ship from
    outside the package directory. A prompt that does not install is a prompt that does not exist.
    """
    assert cli.PROMPT_DIR == pathlib.Path(cli.__file__).resolve().parent / "prompts"
    assert (cli.PROMPT_DIR / "progress.md").is_file()
    assert not (ROOT / "prompts").exists(), "the old top-level copy must be gone, not duplicated"


def test_generated_package_artifacts_are_not_tracked():
    """A tracked ``build/lib`` can silently override newer source when setuptools builds a wheel."""
    tracked = subprocess.run(
        ["git", "ls-files", "build", "tauceti_progress.egg-info"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert not tracked.strip(), tracked


def test_the_worker_placeholders_are_all_present():
    """The worker substitutes these after fetching; a renamed one would silently ship as literal."""
    text = (cli.PROMPT_DIR / "progress.md").read_text()
    for key in ("__ROADMAP__", "__ROADMAP_DIR__", "__PLAN_FILE__", "__FACTS_FILE__",
                "__STATUS_OUT__", "__SECTION_OUT__"):
        assert key in text, key


def test_the_prompt_asks_for_no_more_than_the_checked_limit():
    """The prompt's ceiling and `MAX_SECTION_WORDS` must not drift apart: asking for more than the
    check allows would refuse every report."""
    text = (cli.PROMPT_DIR / "progress.md").read_text()
    assert "At most 300 words" in text
    assert files.MAX_SECTION_WORDS >= 300


def test_both_status_prompts_are_voyager_shaped_and_bounded():
    """Two prompt interfaces write a STATUS body: the worker's `progress` prompt (both files, driven
    by a plan) and the standalone `status` prompt (one body, no plan). Both are held to the same
    shape and the same ceiling."""
    for name in ("progress.md", "status.md"):
        text = " ".join((cli.PROMPT_DIR / name).read_text().split())
        assert "At most 750 words" in text, name
        assert "### Named results" in text, name
        assert "### Notable definitions and infrastructure" in text, name
        assert "plain language first, references last" in text, name
    assert files.MAX_STATUS_WORDS >= 750


def test_the_progress_prompt_asks_for_the_coverage_block_in_the_checked_shape():
    """The worker's prompt is handed the plan's `layers`, so it asks for the block; its states and
    its note bound must not drift from what `files` accepts."""
    text = (cli.PROMPT_DIR / "progress.md").read_text()
    assert "```coverage" in text and '"state"' in text and '"remaining"' in text
    for state in files.LAYER_STATES:
        assert f"`{state}`" in text, state
    assert "at most 200" in text and files.REMAINING_RE.pattern.endswith("{1,200}\\Z")
    assert "`layers` list" in text and "__PLAN_FILE__" in text
    # An umbrella's plan lists sub-roadmaps; the prompt says where their READMEs are and what shape
    # the block takes for them.
    assert "`sub_roadmaps`" in text and "`readme`" in text and "JSON object" in text
    assert '"__ROADMAP__/SchurWeyl": [' in text


def test_the_progress_prompt_carries_earlier_assessments_forward():
    """The facts file covers one window; the block covers the whole roadmap. Read naively, "if a
    result is not in here, it did not land" would turn every layer finished in an earlier window
    `unassessed` on the Progress page. The prompt must scope the facts file to the window and say
    that earlier verdicts stand unless there is a reason to revise them."""
    text = " ".join((cli.PROMPT_DIR / "progress.md").read_text().split())
    assert "ground truth for this window" in text
    assert "it did not land in this window" in text and "If a result is not in here, it did not land. " not in text
    assert "evidence for what landed in earlier windows" in text
    assert "Carry their assessments forward unless" in text
    assert "not this window" in text and "does not become `unassessed` merely because nothing for it landed" in text
    assert "Do not claim anything the declaration list does not support." not in text


def test_the_standalone_status_prompt_is_prose_only_and_says_so():
    """`prompt status` gets `__CONTEXT__` and no plan, so no layer list to assess against."""
    text = (cli.PROMPT_DIR / "status.md").read_text()
    assert "__CONTEXT__" in text and "__PLAN_FILE__" not in text
    assert "```coverage" not in text and "no per-layer coverage header" in text


def test_both_status_prompts_prefer_readable_names_and_documentation():
    for name in ("progress.md", "status.md"):
        text = " ".join((cli.PROMPT_DIR / name).read_text().split())
        assert "De Finetti-Ryll-Nardzewski equivalence" in text, name
        assert "at most two in the whole snapshot" in text, name
        assert "documentation" in text, name


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
