"""Every inline <script> in a template must at least be lexically well-formed.

Written after a one-character bug took out a whole page in production. A `\\n` escape in a
JS string literal was written to the template as a REAL newline:

    'Auto-delete this POV in how many hours from now?
    ' +

JavaScript forbids a raw newline inside a `'` or `"` string, so that is a SyntaxError. The
browser then discards the ENTIRE script block, `povPage()` is never defined, and Alpine's
`x-data="povPage()"` resolves to nothing — so every expression on the page throws
`ReferenceError: <prop> is not defined`. The page renders its static header and nothing
else, with no failing network request and no server-side error. Nothing in the test suite
noticed, because nothing here had ever looked at the JavaScript.

This is a LEXER, not a parser. It cannot tell you the code is correct; it tells you the
quotes and brackets close, which is precisely the class of damage a template edit does. A
real parser would need Node, which this environment does not have.

Runs under pytest, or standalone:
    python tests/test_template_scripts.py
"""
import os
import pathlib
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

TEMPLATES = pathlib.Path(_ROOT) / "web_dashboard" / "templates"

# `<script>` with no src. A `<script src=...>` has no body to check.
#
# `</script\s*>` rather than `</script>`: HTML permits whitespace before the `>` of an end
# tag, so `</script >` closes the element. Matching only the tight form would make the
# non-greedy body run PAST that tag to the next one, swallowing the markup between them and
# lexing it as JavaScript — which would either invent failures or, worse, hide a real one
# by burying it in noise. CodeQL flags this pattern as a bad tag filter, and for a scanner
# whose whole job is to be trusted, it is right to.
_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script\s*>", re.S | re.I)


def _blocks():
    """(path, first_line_number, source) for every inline script in every template."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _SCRIPT.finditer(text):
            yield path, text[:m.start(1)].count("\n") + 1, m.group(1)


def _scan(src: str):
    """Lex one script body. Returns (unterminated_line_offsets, bracket_error).

    Tracks the four JS string forms plus both comment forms, because a `'` inside a
    comment or a backtick literal is not a quote — the naive count-the-quotes check that
    first found this bug flags `${env.name}? The customer's URL` as broken when it is
    fine.
    """
    unterminated, stack = [], []
    line, i, n = 1, 0, len(src)
    pairs = {")": "(", "]": "[", "}": "{"}
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif c in "'\"":
            # A raw newline before the closing quote is the bug this file exists for.
            start_line, i, closed = line, i + 1, False
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "\n":
                    break
                if src[i] == c:
                    closed, i = True, i + 1
                    break
                i += 1
            if not closed:
                unterminated.append(start_line)
                # Resynchronise at the newline rather than bailing, so one bad string does
                # not hide a second one further down.
                while i < n and src[i] != "\n":
                    i += 1
        elif c == "`":
            # Backticks MAY span lines, so only an EOF-unterminated one is an error.
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "\n":
                    line += 1
                elif src[i] == "`":
                    i += 1
                    break
                i += 1
            else:
                unterminated.append(line)
        elif c in "([{":
            stack.append((c, line))
            i += 1
        elif c in ")]}":
            if stack and stack[-1][0] == pairs[c]:
                stack.pop()
            else:
                return unterminated, f"unmatched {c!r} on line {line}"
            i += 1
        else:
            i += 1
    if stack:
        c, ln = stack[-1]
        return unterminated, f"unclosed {c!r} opened on line {ln}"
    return unterminated, ""


def test_no_string_literal_spans_a_newline():
    """The exact shape of the bug: a `'` or `"` string broken across two lines."""
    bad = []
    for path, offset, src in _blocks():
        lines, _ = _scan(src)
        bad += [f"{path.relative_to(_ROOT)}:{offset + ln - 1}" for ln in lines]
    assert not bad, (
        "unterminated string literal(s) in an inline <script> — the browser discards the "
        "whole block and every Alpine expression on the page throws:\n  "
        + "\n  ".join(bad))


def test_brackets_balance_in_every_inline_script():
    """A dropped brace is the other way a template edit kills a script block."""
    bad = []
    for path, offset, src in _blocks():
        _, err = _scan(src)
        if err:
            bad.append(f"{path.relative_to(_ROOT)} (block at line {offset}): {err}")
    assert not bad, "unbalanced brackets in an inline <script>:\n  " + "\n  ".join(bad)


def test_the_scanner_actually_catches_the_bug_it_was_written_for():
    """A guard that cannot fail is not a guard. This is the literal shipped defect."""
    broken = """
      const answer = window.prompt(
        'Auto-delete this POV in how many hours from now?
' +
        'Blank cancels.',
        '');
"""
    lines, _ = _scan(broken)
    assert lines, "the scanner would not have caught the bug that motivated it"

    # …and does not fire on the things that merely look like it: an apostrophe inside a
    # backtick literal, an escaped quote, and a quote inside a comment.
    fine = """
      if (!confirm(`Revoke ${env.name}? The customer's URL stops working.`)) return;
      const s = 'it\\'s fine';
      // don't trip on this apostrophe
      /* nor this one: won't */
      const t = "a \\"quoted\\" word";
"""
    lines, err = _scan(fine)
    assert not lines and not err, f"false positive: {lines} {err}"


def test_the_block_extractor_honours_a_spaced_end_tag():
    """`</script >` closes the element in HTML. If the extractor missed it the body would
    run on to the NEXT closing tag, and the markup in between would be lexed as
    JavaScript — inventing failures, or burying a real one in them."""
    html = "<script>\n  var a = 1;\n</script >\n<p>not js: it's fine</p>\n"
    bodies = [m.group(1) for m in _SCRIPT.finditer(html)]
    assert bodies == ["\n  var a = 1;\n"], bodies
    # The apostrophe in the paragraph would look like an unterminated string if the
    # extractor had swallowed it, so this doubles as a check that it did not.
    assert not _scan(bodies[0])[0]


def test_every_x_data_component_is_defined_somewhere():
    """`x-data="foo()"` with no `foo` anywhere is the same failure by another route — the
    component resolves to nothing and every expression on the page throws.

    Searches the static JS as well as the template itself: shared components such as
    `responsiveNav()` live in `static/js/app.js`, not inline.
    """
    static_dir = pathlib.Path(_ROOT) / "web_dashboard" / "static" / "js"
    static_js = "\n".join(p.read_text(encoding="utf-8")
                          for p in static_dir.rglob("*.js"))
    missing = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        haystack = text + "\n" + static_js
        for fn in set(re.findall(r'x-data="(\w+)\(\)"', text)):
            if not re.search(rf"function\s+{fn}\s*\(|{fn}\s*[:=]\s*(?:function|\()",
                             haystack):
                missing.append(f"{path.relative_to(_ROOT)}: x-data=\"{fn}()\"")
    assert not missing, (
        "x-data names a function nothing defines:\n  " + "\n  ".join(missing))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
