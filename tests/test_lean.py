"""Tests for the Lean declaration scanner.

The cases that matter are the ones a naive line scan gets wrong: declaration keywords appearing as
ordinary words inside docstrings, nested block comments, and anonymous instances.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from progress import lean  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok   {name}")


def test_basic_kinds_and_modifiers():
    src = """
theorem foo_bar (x : Nat) : x = x := rfl
lemma baz : True := trivial
private lemma hidden : True := trivial
noncomputable def qux : Nat := 0
private noncomputable def quux : Nat := 0
@[simp] lemma simped : True := trivial
@[grind =>] theorem grinded : True := trivial
public theorem exposed : True := trivial
structure MyStruct where
  n : Nat
class MyClass (a : Type) where
inductive MyInd where
  | a
abbrev MyAbbrev := Nat
opaque MyOpaque : Nat
"""
    d = lean.declarations(src)
    assert set(d) == {
        "foo_bar", "baz", "hidden", "qux", "quux", "simped", "grinded", "exposed",
        "MyStruct", "MyClass", "MyInd", "MyAbbrev", "MyOpaque",
    }, sorted(d)
    assert d["qux"]["kind"] == "def"
    assert d["simped"]["kind"] == "lemma"
    assert d["MyStruct"]["kind"] == "structure"


def test_keywords_inside_docstrings_are_not_declarations():
    """The real failure this scanner exists to avoid: prose that begins a line with a keyword.

    Taken from the shape of an actual TauCeti docstring, where a line begins "structure on its
    functor of points ..." and a line scan would invent a `structure` named `on`.
    """
    src = """
/-- The comultiplication is by convolution, with the counit as identity, and the
structure on its functor of points evaluated at `A`. Also mentions
instance; the public pointwise characterization is `convInv_apply`.
def not_a_real_decl -/
theorem the_only_decl : True := trivial
"""
    d = lean.declarations(src)
    assert set(d) == {"the_only_decl"}, sorted(d)


def test_nested_block_comments():
    src = """
/- outer /- inner -/ still outer
theorem hidden_by_comment : True := trivial
-/
theorem visible : True := trivial
"""
    d = lean.declarations(src)
    assert set(d) == {"visible"}, sorted(d)


def test_line_comments():
    src = """
-- theorem commented_out : True := trivial
theorem real_one : True := trivial   -- trailing note
"""
    d = lean.declarations(src)
    assert set(d) == {"real_one"}, sorted(d)


def test_module_docstring_is_not_attached_as_a_doc():
    src = """
/-! # A module header
This is a module docstring, not a declaration docstring.
-/

/-- The actual docstring: `f` is continuous. -/
theorem f_continuous : True := trivial
"""
    d = lean.declarations(src)
    assert set(d) == {"f_continuous"}, sorted(d)
    assert d["f_continuous"]["doc"] == "The actual docstring: `f` is continuous."


def test_docstring_first_sentence():
    src = """
/-- **Atkinson's theorem.** An operator is Fredholm exactly when it is invertible modulo
compacts. The proof goes via parametrices, which we do not state here. -/
theorem atkinson : True := trivial
"""
    d = lean.declarations(src)
    doc = d["atkinson"]["doc"]
    assert doc.startswith("**Atkinson's theorem.**"), doc
    # Only the first sentence; the "The proof goes via" tail is dropped.
    assert "parametrices" not in doc, doc


def test_docstring_separated_by_blank_line_still_attaches():
    src = """
/-- Doc for later decl. -/

theorem later : True := trivial
"""
    assert lean.declarations(src)["later"]["doc"] == "Doc for later decl."


def test_anonymous_instance():
    src = """
instance : Inhabited Nat := ⟨0⟩
instance named_inst : Inhabited Bool := ⟨true⟩
"""
    d = lean.declarations(src)
    assert lean.ANONYMOUS in d, sorted(d)
    assert "named_inst" in d, sorted(d)


def test_indented_terms_are_not_declarations():
    """`have`-style inner structure and indented continuation lines must not register."""
    src = """
theorem outer : True := by
  have inner : True := trivial
  exact inner
"""
    d = lean.declarations(src)
    assert set(d) == {"outer"}, sorted(d)


def test_added_declarations_is_a_set_difference():
    before = "theorem a : True := trivial\n"
    after = "theorem a : True := trivial\ntheorem b : True := trivial\ninstance : Foo := f\n"
    added = lean.added_declarations(before, after)
    assert set(added) == {"b"}, sorted(added)   # `a` pre-existed; anonymous instance dropped


def test_added_declarations_from_nothing():
    added = lean.added_declarations(None, "/-- Doc. -/\ntheorem x : True := trivial\n")
    assert set(added) == {"x"}
    assert added["x"]["doc"] == "Doc."


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
