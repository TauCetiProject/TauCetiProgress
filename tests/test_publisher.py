"""Tests for the publisher preflight: who may land a report, decided before a round spends anything."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import publisher  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def expect_refusal(fn, fragment):
    try:
        fn()
    except publisher.NotAPublisher as exc:
        assert fragment in str(exc), f"expected {fragment!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {fragment!r}")


# ----- parsing -------------------------------------------------------------------------------

def test_plain_ids_parse():
    assert publisher.parse_publishers("477956\n1234\n") == {477956, 1234}


def test_login_after_the_id_is_free_text():
    assert publisher.parse_publishers("477956   kim-em\n1234 someone else\n") == {477956, 1234}


def test_comments_and_blank_lines_are_ignored():
    text = "# who may publish\n\n477956  kim-em\n\n  # trailing note\n"
    assert publisher.parse_publishers(text) == {477956}


def test_trailing_comment_on_an_entry():
    assert publisher.parse_publishers("477956 kim-em # the maintainer\n") == {477956}


def test_empty_file_is_an_empty_allowlist():
    assert publisher.parse_publishers("") == set()
    assert publisher.parse_publishers("# nobody yet\n") == set()


def test_a_login_instead_of_an_id_is_rejected():
    """Logins are renameable, so an allowlist of them would transfer rights on a rename."""
    try:
        publisher.parse_publishers("kim-em\n")
    except ValueError as exc:
        assert "not a numeric user id" in str(exc)
    else:
        raise AssertionError("a login should not parse as an id")


def test_a_malformed_line_raises_rather_than_being_skipped():
    """Skipping would silently shrink the allowlist, which fails as 'reports quietly stopped'."""
    try:
        publisher.parse_publishers("477956\n47795 6extra\nnot-an-id\n")
    except ValueError as exc:
        assert ":3:" in str(exc), f"the line number should name the bad line: {exc}"
    else:
        raise AssertionError("expected a parse error")


def test_non_ascii_digits_are_rejected():
    """`str.isdigit()` accepts these and `int()` parses them, so the check must be stricter."""
    for bad in ("٤٧٧٩٥٦", "⁴⁷⁷", "1_234", "+477956", "-1", "0x1f"):
        try:
            publisher.parse_publishers(bad + "\n")
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse as a user id")


def test_an_entry_cannot_hide_inside_an_apparent_comment():
    """The file's whole security value is that a human reviewed the diff.

    `str.splitlines` breaks on U+2028 and friends; a terminal, an editor and GitHub's diff view
    generally do not. Left in, `# comment<U+2028>999999 attacker` reads as one comment line to the
    reviewer and as a comment *plus a live entry* to the parser.
    """
    sneaky = "# looks like one comment 999999 attacker\n477956 kim-em\n"
    try:
        publisher.parse_publishers(sneaky)
    except ValueError as exc:
        assert "U+2028" in str(exc)
    else:
        raise AssertionError("an id hidden behind U+2028 must not be honoured")


def test_every_exotic_line_break_is_rejected():
    for ch in "\v\f\x1c\x1d\x1e\x85  ":
        try:
            publisher.parse_publishers(f"477956{ch}999999\n")
        except ValueError:
            continue
        raise AssertionError(f"U+{ord(ch):04X} should be rejected")


def test_ordinary_line_endings_still_work():
    assert publisher.parse_publishers("477956\r\n1234\n") == {477956, 1234}


def test_a_byte_order_mark_is_not_an_entry():
    assert publisher.parse_publishers("﻿477956 kim-em\n") == {477956}


def test_an_absurdly_long_run_of_digits_is_rejected():
    try:
        publisher.parse_publishers("9" * 21 + "\n")
    except ValueError:
        return
    raise AssertionError("an over-long id should not parse")


# ----- the decision --------------------------------------------------------------------------

def test_a_listed_publisher_with_push_may_publish():
    reason = publisher.check_can_publish(uid=477956, allowed={477956}, pushable=True)
    assert "may publish" in reason


def test_an_unlisted_author_is_refused_before_any_work():
    expect_refusal(
        lambda: publisher.check_can_publish(uid=999, allowed={477956}, pushable=True),
        "not listed",
    )


def test_the_refusal_explains_the_area_blocking_consequence():
    """The reason a non-publisher must not proceed is not merely that it fails; it poisons the area."""
    try:
        publisher.check_can_publish(uid=999, allowed={477956}, pushable=True)
    except publisher.NotAPublisher as exc:
        assert "block its area" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_a_listed_publisher_without_push_is_refused():
    expect_refusal(
        lambda: publisher.check_can_publish(uid=477956, allowed={477956}, pushable=False),
        "cannot push",
    )


def test_an_empty_allowlist_refuses_everyone():
    expect_refusal(
        lambda: publisher.check_can_publish(uid=477956, allowed=set(), pushable=True),
        "not listed",
    )


def test_listing_is_checked_before_push_access():
    """Ordering matters only for the message, but the message is what an operator acts on."""
    expect_refusal(
        lambda: publisher.check_can_publish(uid=999, allowed={477956}, pushable=False),
        "not listed",
    )


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
