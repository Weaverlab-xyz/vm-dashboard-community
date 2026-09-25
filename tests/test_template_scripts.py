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

Two halves, and the distinction matters. Extracting the `<script>` bodies uses a REAL
parser (`html.parser`), because finding the end of a script element correctly is genuinely
hard and getting it wrong makes the scanner lie. Checking the JavaScript inside is a LEXER:
it cannot tell you the code is correct, only that the quotes and brackets close — which is
precisely the class of damage a template edit does. A real JS parser would need Node, which
this environment does not have.

Runs under pytest, or standalone:
    python tests/test_template_scripts.py
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

TEMPLATES = pathlib.Path(_ROOT) / "web_dashboard" / "templates"


class _ScriptExtractor(HTMLParser):
    """Collect `(body_start_line, source)` for every inline `<script>`.

    A real parser rather than a regex, and that is not fussiness. Finding the end of a
    script element correctly is genuinely hard: HTML lets an end tag carry whitespace AND
    ignored attributes, so `</script >` and `</script\\t\\n bar>` both close the element.
    Two rounds of CodeQL findings here were each a real hole — a missed end tag makes the
    body run on to the NEXT one and lex intervening markup as JavaScript, so the scanner
    either invents failures or buries a real one. For a guard whose whole value is being
    believed when it says the page is fine, that is the worst way to be wrong.

    `HTMLParser` already implements the spec's CDATA handling for script content, so this
    delegates instead of re-deriving it. Verified to extract byte-identical bodies to the
    regex it replaced across all 36 inline scripts in this repo's templates — Jinja tags
    included, which it treats as ordinary text.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks: list = []
        self._buf: list | None = None
        self._line = 0

    def handle_starttag(self, tag, attrs):
        # A `<script src=...>` has no body to check.
        if tag == "script" and not dict(attrs).get("src"):
            self._buf, self._line = [], self.getpos()[0]

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._buf is not None:
            self.blocks.append((self._line, "".join(self._buf)))
            self._buf = None


def _extract(text: str) -> list:
    """`[(body_start_line, source)]` for one document."""
    parser = _ScriptExtractor()
    parser.feed(text)
    parser.close()
    return parser.blocks


def _blocks():
    """(path, first_line_number, source) for every inline script in every template."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        for line, src in _extract(path.read_text(encoding="utf-8")):
            yield path, line, src


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
                # not hide a second one further down. The cost is a follow-on report on the
                # continuation line — a string broken across two lines leaves an orphaned
                # closing quote that also reads as unterminated. The FIRST line reported is
                # always the real one; that is worth more than a tidy single-line report
                # that could conceal a second fault.
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


def test_no_inline_script_contains_a_nested_script_open_tag():
    """A doubled script tag kills the block exactly the way the raw newline above does.

    Found live, 2026-09-16, while consolidating the Users and Groups pages into tabs: the
    generated partial ended up with two nested open tags and two closes. HTML has no nested
    script element -- the parser ends the first one at the FIRST close tag -- so the block's
    body was the real code with a literal open tag glued to the front of it:

        SyntaxError: Unexpected token '<'

    Whole block discarded, `usersPage` never defined, `x-data="usersPage()"` resolves to
    nothing, and the tab renders its table headers and 128 `ReferenceError`s. Identical
    symptom to the bug this file was written for, from a different cause -- and NOTHING here
    caught it: the bracket lexer sees balanced brackets, the quote lexer sees closed quotes,
    and the block-extraction test only checks the END tag forms.

    It is not reachable by hand-editing one template, which is why it went unnoticed: you
    have to ASSEMBLE a template, and the assembling is what a page merge does.
    """
    bad = []
    for path, offset, src in _blocks():
        # The extractor hands back the element's TEXT, so an open tag in here is a tag the
        # HTML parser never opened -- i.e. a nested one. Checked case-insensitively and
        # allowing whitespace, since `< script` and `<SCRIPT` parse the same.
        m = re.search(r"<\s*script[\s>]", src, re.I)
        if m:
            bad.append("%s (block at line %d): a script open tag inside a script body, at "
                       "offset %d" % (path.relative_to(_ROOT), offset, m.start()))
    assert not bad, (
        "a nested script open tag discards the whole block and leaves its x-data "
        "undefined:\n  " + "\n  ".join(bad))


def test_the_nested_tag_scanner_catches_the_bug_it_was_written_for():
    """The guard above must fail on the real shape, or it is decoration."""
    doubled = ("<script>\n<script>\nfunction page() { return {}; }\n"
               "</script>\n</script>")
    blocks = list(_extract(doubled))
    assert blocks, "the extractor found no block in the doubled sample"
    assert any(re.search(r"<\s*script[\s>]", src, re.I) for _, src in blocks), (
        "the extractor no longer surfaces a nested open tag, so the guard above is blind")


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


def test_no_string_literal_spans_a_newline_in_static_js():
    """The same defect, in the file this whole module never looked at.

    Shipped live in 26.10.20 and it took the ENTIRE UI down, on every page at once:
    two `confirm()` prompts in `static/js/app.js` carried a real newline inside a `'`
    string, so the browser discarded the whole file. Every global it defines went with
    it -- `responsiveNav`, `statusBadge`, the `auth` store -- and the dashboard rendered
    its heading and one empty card, with 88 `ReferenceError`s in the console and not one
    server-side symptom to find.

    Nothing could catch it. `_blocks()` walks `templates/**/*.html` and yields INLINE
    script bodies, so the one JavaScript file that every page loads was the single
    largest blind spot in the suite -- and the riskiest, because a template break costs
    you one page while this costs you all of them.

    **Only the string check runs here, not `_scan`'s bracket half.** `_scan` has no
    regex-literal state (it never needed one: inline template scripts have no regexes),
    and app.js is full of `.replace(/\\//g, '_')`, which it reads as a comment or a stray
    delimiter and then reports an unbalanced brace 800 lines later. Applying the bracket
    check here would mean a guard that cries wolf from the day it lands, and a guard
    nobody believes is worse than no guard. The string half is exactly the half that
    catches this bug class.
    """
    bad = []
    for path in sorted((pathlib.Path(_ROOT) / "web_dashboard" / "static" / "js")
                       .rglob("*.js")):
        lines, _ = _scan(path.read_text(encoding="utf-8"))
        bad += [f"{path.relative_to(_ROOT)}:{ln}" for ln in lines]
    assert not bad, (
        "unterminated string literal(s) in a static JS file -- the browser discards the "
        "WHOLE file, so every page that loads it loses every global it defines:\n  "
        + "\n  ".join(bad))


def test_static_js_actually_parses():
    """`node --check` on every static JS file — a real parser, not a lexer.

    The scanner above catches the ONE shape that has now shipped twice, and it runs
    everywhere, which is why it exists. This catches everything else: a stray brace, a
    dropped paren, a reserved word as an identifier — any of which discards the whole
    file exactly as the raw newline did.

    Skipped where node is absent (the Windows dev machine), which is the same bargain
    `test_templates_parse._run_node` already makes. That is acceptable ONLY because the
    lexer above is unconditional: the two together mean the common defect is caught at
    the desk and every other defect is caught before merge. CI is ubuntu-latest, where
    node is always present, so nothing reaches an image unparsed.
    """
    node = shutil.which("node")
    if not node:
        print("   (skipped: node not installed)")
        return
    bad = []
    for path in sorted((pathlib.Path(_ROOT) / "web_dashboard" / "static" / "js")
                       .rglob("*.js")):
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            bad.append(f"{path.relative_to(_ROOT)}:\n{proc.stderr.strip()}")
    assert not bad, (
        "static JS does not parse -- the browser discards the WHOLE file and every page "
        "that loads it loses every global it defines:\n" + "\n".join(bad))


def test_the_static_js_scanner_catches_the_bug_it_was_written_for():
    """A guard that cannot fail is not a guard — the shipped 26.10.20 defect, verbatim."""
    broken = """
            if (question && this.bulkPowerScheduled && this.bulkPowerRunAt) {
                question += '

This will be SCHEDULED for '
                          + this.bulkPowerRunAt.replace('T', ' ');
            }
"""
    lines, _ = _scan(broken)
    assert lines, "the static-JS scanner would not have caught the bug that motivated it"

    # The fixed form, which is what shipped in its place, must stay clean.
    fixed = """
            if (question && this.bulkPowerScheduled && this.bulkPowerRunAt) {
                question += '\\n\\nThis will be SCHEDULED for '
                          + this.bulkPowerRunAt.replace('T', ' ');
            }
"""
    lines, _ = _scan(fixed)
    assert not lines, f"false positive on the fixed form: {lines}"


def test_the_extractor_honours_every_end_tag_form():
    """HTML lets an end tag carry whitespace and ignored attributes, so all three of these
    close the element. Missing one makes the body run on to the NEXT closing tag and lex
    the markup in between as JavaScript — inventing failures, or burying a real one.

    Each case puts an apostrophe in the trailing markup, so a regression shows up as an
    unterminated-string report rather than a silent miss.
    """
    for end in ("</script>", "</script >", "</script\t\n bar>"):
        html = f"<script>\n  var a = 1;\n{end}\n<p>not js: it's fine</p>\n"
        blocks = _extract(html)
        assert len(blocks) == 1, f"{end!r} -> {blocks}"
        assert blocks[0][1] == "\n  var a = 1;\n", f"{end!r} -> {blocks[0][1]!r}"
        assert not _scan(blocks[0][1])[0], f"{end!r} swallowed the trailing markup"


def test_the_reported_line_is_the_real_one():
    """A guard that points at the wrong line costs the next person the time it just saved,
    so pin the arithmetic that turns a body offset back into a file line."""
    doc = "\n".join(["<html>", "<body>", "<script>", "  var a = 1;",
                     "  var b = 'oops", "';", "</script>"])
    (start, src), = _extract(doc)
    lines, _ = _scan(src)
    reported = [start + ln - 1 for ln in lines]
    # Line 5 is the broken string. Line 6 is the follow-on described in `_scan` — the
    # orphaned closing quote. The FIRST one is what matters and must be exact.
    assert reported[0] == 5, reported


def test_a_script_with_src_is_skipped():
    """It has no body here to check, and its content is not in the template."""
    assert _extract('<script src="/static/js/app.js"></script>') == []


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


# Routes that are deliberately reachable without a token: the setup wizard runs before a
# user exists, login (password or passkey) is how a token is obtained, and the feature map
# and health check sit on _SETUP_BYPASS_PREFIXES in main.py for the same reason. Listed as
# individual prefixes rather than `/api/auth/`, because `/api/auth/me` DOES need the header
# and login.html duly sends one.
_PUBLIC_API = ("/api/features", "/api/health", "/api/setup/", "/api/auth/login",
               "/api/auth/webauthn/login/",
               # The persona is curation -- ordering hints and demo-script copy. It is
               # ungated on purpose (a persona may never gate anything) and sits on
               # _SETUP_BYPASS_PREFIXES because the wizard's Focus step reads the catalog
               # before setup is complete. It discloses nothing /api/features does not:
               # install_profile is already public there and in the /docs shell, and a
               # card's ready/needs_flag state is derived from the same feature map.
               "/api/persona")

_BARE_FETCH = re.compile(r"""(?<![\w.])fetch\(\s*(['"`])(/api/[^'"`]*)\1""")


def test_no_unauthenticated_api_fetch():
    """A template must not call an authenticated /api/ route with a bare `fetch()`.

    This dashboard authenticates every API route off the `Authorization` header --
    `get_current_user` depends on `OAuth2PasswordBearer` and the app sets no cookie
    anywhere -- so `fetch('/api/...')` with no headers is an ANONYMOUS request. It comes
    back 401 `{"detail":"Not authenticated"}` however healthy the integration behind the
    route is.

    The failure is nasty because it does not look like an auth failure. The POV page made
    six of these, and the first one it fired rendered "Could not read the lab platform
    registry" -- so a Skytap credential that had passed Settings -> Verify seconds earlier
    read as a broken Skytap. Nothing 500s, nothing is logged as an error, and the page's
    own error text names the wrong system.

    A `fetch()` whose URL is a variable is not checked: that is the wrapper pattern
    (`window.API.request`, and the per-page `apiFetch` helpers), which is exactly where a
    bare `fetch` is supposed to end up.
    """
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _BARE_FETCH.finditer(text):
            url = m.group(2)
            if url.startswith(_PUBLIC_API):
                continue
            # The options object, if there is one, follows the URL argument. The header
            # has to be in it.
            if "Authorization" in text[m.end():m.end() + 400]:
                continue
            line = text.count("\n", 0, m.start()) + 1
            offenders.append("%s:%d: fetch('%s')"
                             % (path.relative_to(_ROOT), line, url))
    assert not offenders, (
        "bare fetch() of an authenticated API route -- these return 401 "
        "'Not authenticated', not the integration error the page will show:\n  "
        + "\n  ".join(offenders))


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
