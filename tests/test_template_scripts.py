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
               "/api/auth/webauthn/login/")

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
