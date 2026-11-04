#!/usr/bin/env python3
"""Run `node --check` over every inline <script> in the templates, and over our own JS.

tests/test_template_scripts.py already catches the two commonest template-edit mistakes
(a string broken across a line, an unbalanced bracket) with a hand-written lexer that needs
no Node. This is the other half: a real JavaScript parser, so the rest of what a parser
rejects -- a stray comma, `else` with no `if`, a reserved word as a name, a missing `)` the
bracket count cannot see -- fails here, in CI, instead of in the browser smoke job or in
production, where one syntax error discards the WHOLE script block and every Alpine
expression on the page throws.

Syntax only: `--check` parses and never runs anything.

Jinja is not JavaScript, so before parsing, `{{ ... }}` becomes the literal `null`, and
`{% ... %}` and `{# ... #}` are removed. Newlines inside them are kept, so a reported line
is the template's own line. Both branches of an `{% if %}` therefore stay in, which can
parse worse than either branch alone; a block that only fails because of that belongs in
scripts/ci/template_js_allow.txt with the reason, the same shape as the test skip list.

Usage:  python scripts/ci/check_template_js.py        (needs `node` on PATH)
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from test_template_scripts import _ScriptExtractor  # noqa: E402  (the one extractor)

ALLOW = ROOT / "scripts" / "ci" / "template_js_allow.txt"
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_JINJA_TAG = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)
_ERR_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)$")


def _neutralise(src: str) -> str:
    keep_nl = lambda m: "\n" * m.group(0).count("\n")  # noqa: E731
    src = _JINJA_TAG.sub(keep_nl, src)
    return _JINJA_EXPR.sub(lambda m: "null" + keep_nl(m), src)


def _blocks():
    """(label, first_line, source, module?) for every script we own."""
    tdir = ROOT / "web_dashboard" / "templates"
    for path in sorted(tdir.rglob("*.html")):
        p = _ScriptExtractor_with_types()
        p.feed(path.read_text(encoding="utf-8"))
        p.close()
        for line, src, kind in p.blocks3:
            if kind == "skip":
                continue
            yield str(path.relative_to(ROOT)).replace("\\", "/"), line, _neutralise(src), kind == "module"
    for path in sorted(tdir.rglob("*.js")):
        yield (str(path.relative_to(ROOT)).replace("\\", "/"), 1,
               _neutralise(path.read_text(encoding="utf-8")), False)
    static = ROOT / "web_dashboard" / "static" / "js"
    for path in sorted(static.glob("*.js")):   # not vendor/: third-party, minified
        yield str(path.relative_to(ROOT)).replace("\\", "/"), 1, path.read_text(encoding="utf-8"), False


class _ScriptExtractor_with_types(_ScriptExtractor):
    """The shared extractor plus the script's `type`, so JSON and template blocks are not
    parsed as JavaScript."""

    def __init__(self):
        super().__init__()
        self.blocks3: list = []
        self._kind = "js"

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            t = (dict(attrs).get("type") or "").lower()
            self._kind = ("module" if t == "module" else
                          "js" if t in ("", "text/javascript", "application/javascript") else "skip")
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        had = tag == "script" and self._buf is not None
        super().handle_endtag(tag)
        if had:
            line, src = self.blocks[-1]
            self.blocks3.append((line, src, self._kind))


def _allowed() -> set:
    if not ALLOW.exists():
        return set()
    return {ln.split("#")[0].strip() for ln in ALLOW.read_text(encoding="utf-8").splitlines()
            if ln.split("#")[0].strip()}


def main() -> int:
    allowed, failures, checked = _allowed(), [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, (label, line, src, is_module) in enumerate(_blocks()):
            key = f"{label}:{line}"
            f = pathlib.Path(tmp) / f"b{i}.{'mjs' if is_module else 'js'}"
            f.write_text(src, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            checked += 1
            if r.returncode == 0 or key in allowed or label in allowed:
                continue
            first = (r.stderr.strip().splitlines() or ["(no output)"])
            m = _ERR_LINE.match(first[0].replace("\\", "/").replace(str(f).replace("\\", "/"), "F"))
            at = f"{label}:{line + int(m.group('line')) - 1}" if m else key
            detail = "\n    ".join(l for l in first[1:6] if l.strip())
            failures.append(f"{at}  (block starting {key})\n    {detail}")
    if failures:
        print(f"node --check: {len(failures)} of {checked} script(s) do not parse:\n")
        print("\n".join(failures))
        return 1
    print(f"node --check: {checked} script(s) parse ({len(allowed)} allowlisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
