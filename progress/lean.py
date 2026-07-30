"""Just enough Lean 4 lexing to list the declarations a file defines, with their docstrings.

This is not a parser and does not need to be. It answers one question -- "which declarations does
this file introduce, and what does each one's docstring say it proves" -- and it has to answer it
without a Lean toolchain, because the reporting path must stay cheap enough to run daily.

Getting comments right is the whole difficulty. Prose inside a docstring routinely begins a line
with a declaration keyword, for instance

    /-- ... the comultiplication is by convolution, with the counit as identity, and the
    structure on its functor of points evaluated at `A`. -/

so a scan that looked at raw lines (or at raw *diff* lines, which is worse, since a hunk can start
mid-comment) would invent a `structure` declaration called `on`. Lean block comments also nest, so
depth has to be tracked rather than matched.
"""

import re

# Modifiers that may precede the declaration keyword, and attribute blocks like `@[simp]`.
_PREFIX = r"(?:@\[[^\]]*\]\s*)*(?:(?:public|private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
_KINDS = "theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom"

# A declaration must start at column zero: everything in this library is top-level, and requiring
# it avoids matching `have`-style indented inner terms or continuation lines.
_DECL_RE = re.compile(rf"\A{_PREFIX}({_KINDS})\b[ \t]*([^\s:({{\[|]*)")

# `instance` may be anonymous (`instance : Foo Bar := ...`), which is legitimate and unnameable.
ANONYMOUS = "<anonymous>"


def _strip_to_segments(text):
    """Split `text` into `[(is_doc, comment_text)]` and `[(line_no, code_line)]`.

    Returns `(docs, code_lines)` where `docs` maps a line number to the docstring that ended just
    before it, and `code_lines` are the lines that lie outside any comment.
    """
    docs = {}          # line number where a doc comment ended -> its text
    code_lines = []    # (line_no, text) for lines with code outside comments
    depth = 0
    doc_depth = None   # the depth at which the current docstring opened
    buf = []
    line_no = 1
    i = 0
    n = len(text)
    cur = []           # code characters seen on the current line

    def flush_line():
        joined = "".join(cur).strip()
        if joined:
            code_lines.append((line_no, joined))
        cur.clear()

    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        three = text[i:i + 3]

        if depth == 0 and two == "--":
            # Line comment: skip to end of line, keeping any code already seen on this line.
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue

        if three in ("/--", "/-!") or two == "/-":
            if depth == 0:
                doc_depth = depth if three == "/--" else None
                buf = []
            depth += 1
            i += 3 if three in ("/--", "/-!") else 2
            continue

        if two == "-/" and depth > 0:
            depth -= 1
            i += 2
            if depth == 0:
                if doc_depth is not None:
                    # Keyed by the line the comment closed on. `declarations` looks back a few
                    # lines from a declaration, so a blank line in between is tolerated.
                    docs[line_no] = "".join(buf).strip()
                doc_depth = None
                buf = []
            continue

        if ch == "\n":
            if depth == 0:
                flush_line()
            elif doc_depth is not None:
                buf.append(ch)
            line_no += 1
            i += 1
            continue

        if depth > 0:
            if doc_depth is not None:
                buf.append(ch)
        else:
            cur.append(ch)
        i += 1

    if depth == 0:
        flush_line()
    return docs, code_lines


def _first_sentence(doc):
    """The first sentence of a docstring, flattened to one line.

    Docstrings in this project state what a lemma proves, so the first sentence is usually the whole
    useful content, and it is what a report wants to quote.
    """
    if not doc:
        return ""
    flat = " ".join(doc.split())
    # Sentence end: a period followed by a space and a capital, or end of string. Keep it simple;
    # over-long results are truncated by the caller.
    m = re.search(r"\.(?=\s+[A-Z(`*]|\Z)", flat)
    out = flat[: m.end()] if m else flat
    return out.strip()


def declarations(text):
    """`{name: {"kind", "doc"}}` for the declarations `text` introduces.

    Anonymous instances are collected under a single `<anonymous>` key and carry no useful identity;
    callers generally drop them.
    """
    docs, code_lines = _strip_to_segments(text)
    out = {}
    for line_no, line in code_lines:
        m = _DECL_RE.match(line)
        if not m:
            continue
        kind, name = m.group(1), (m.group(2) or "").strip()
        if not name:
            name = ANONYMOUS
        # The docstring is the one that closed on an earlier line with only blanks in between;
        # `docs` is keyed by the line the comment ended on, so look back a little.
        doc = ""
        for back in range(line_no, max(0, line_no - 4), -1):
            if back in docs:
                doc = docs[back]
                break
        if name not in out:
            out[name] = {"kind": kind, "doc": _first_sentence(doc)}
    return out


def added_declarations(before_text, after_text):
    """Declarations present in `after_text` but not in `before_text`."""
    before = set(declarations(before_text or ""))
    return {
        name: info
        for name, info in declarations(after_text or "").items()
        if name not in before and name != ANONYMOUS
    }
