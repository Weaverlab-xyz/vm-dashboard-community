"""Users read "Workstream"; nothing they read says "workgroup" unless it means Password Safe's.

The dashboard's ownership scope was called a workgroup, and so is Password Safe's container
for managed systems. Both appeared in the same Settings forms, so the dashboard's was renamed
Workstream in everything a person reads: page text, labels, tooltips, toasts and the messages
the API returns. The code, the API (/api/workgroups and every `workgroup` field), the
`workgroups` scope and the cloud `Workgroup` tag keep the old spelling on purpose.

This test stops the old word creeping back into what users read. It scans:

  * templates -- text nodes, title/placeholder/aria-label/alt, and string literals with a
    space in them (prose, not keys) inside <script> and Alpine attributes. Jinja, HTML
    comments and <code> are skipped;
  * Python -- every string literal inside a `raise` or an `HTTPException(...)`, outside the
    {...} of an f-string.

A hit near a mention of Password Safe, BeyondTrust or PRA is that product's workgroup and is
allowed; so are the few strings listed in ALLOWED.

Runs standalone:
    python tests/test_workstream_wording.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, "web_dashboard")

WORD = re.compile(r"(?<![\w./{`-])(workgroups?|Workgroups?)(?![\w(}`-])")
# A hit within a few lines of one of these is Password Safe's (or PRA's) workgroup.
OTHER_PRODUCT = re.compile(
    r"password\s*safe|passwordsafe|_ps_|\bps\b|beyondinsight|beyondtrust|bips|\bpra\b"
    r"|managed (system|account)|secrets? safe", re.I)
# (file relative to web_dashboard/, words in the string) that legitimately keep the word.
ALLOWED = (
    ("services/aws_service.py", "Workgroup tag"),      # names the cloud tag, which kept it
    ("services/azure_service.py", "workgroup tag"),
    ("services/ps_api_service.py", ""),                # Password Safe's API client
)
VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "alt")


def _allowed(rel, text, ctx):
    if OTHER_PRODUCT.search(ctx):
        return True
    return any(rel == f and words in text for f, words in ALLOWED)


# ── templates ─────────────────────────────────────────────────────────────────

def _prose_in_js(code):
    pat = re.compile(r"""//[^\n]*|('(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)""")
    for m in pat.finditer(code):
        if m.group(1):
            body = m.group(1)[1:-1]
            if " " in body and "/api/" not in body and not body.startswith("/"):
                yield m.start(1), re.sub(r"\$\{[^}]*\}", "", body)


def _template_hits(src):
    """[(offset, text)] of visible text in a template that says workgroup."""
    hidden = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}|<!--.*?-->|<code\b[^>]*>.*?</code>", re.S)
    src = hidden.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), src)
    hits = []
    script = re.compile(r"(<script\b[^>]*>)(.*?)</script>", re.S | re.I)
    tag = re.compile(r"<[a-zA-Z][^<>]*?>", re.S)
    attr = re.compile(r"""([:@\w.-]+)\s*=\s*("([^"]*)"|'([^']*)')""", re.S)
    scripts = [(m.start(2), m.end(2)) for m in script.finditer(src)]
    for s, e in scripts:
        for off, text in _prose_in_js(src[s:e]):
            if WORD.search(text):
                hits.append((s + off, text))

    def in_script(pos):
        return any(s <= pos < e for s, e in scripts)

    last = 0
    for m in tag.finditer(src):
        if in_script(m.start()):
            continue
        between = src[last:m.start()]
        if not in_script(last) and WORD.search(between):
            hits.append((last, between.strip()))
        for a in attr.finditer(m.group(0)):
            name, val = a.group(1), a.group(2)[1:-1]
            if name.lower() in VISIBLE_ATTRS and WORD.search(val):
                hits.append((m.start(), val))
            elif name.startswith((":", "@", "x-")):
                for _off, text in _prose_in_js(val):
                    if WORD.search(text):
                        hits.append((m.start(), text))
        last = m.end()
    return hits


def test_no_template_shows_workgroup():
    bad = []
    tdir = os.path.join(_APP, "templates")
    for base, _d, files in os.walk(tdir):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            lines = src.split("\n")
            rel = os.path.relpath(path, _APP)
            for off, text in _template_hits(src):
                n = src.count("\n", 0, off)
                ctx = "\n".join(lines[max(0, n - 8):n + 4])
                if not _allowed(rel, text, ctx):
                    bad.append(f"{rel}:{n + 1}: {text[:100]!r}")
    assert not bad, ("visible text says workgroup -- users read Workstream (Password Safe "
                     "text is allowed when it says so nearby):\n  " + "\n  ".join(bad))


# ── Python messages ───────────────────────────────────────────────────────────

def _literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _message_strings(tree):
    for n in ast.walk(tree):
        roots = []
        if isinstance(n, ast.Raise) and n.exc is not None:
            roots.append(n.exc)
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == "HTTPException":
                roots.append(n)
        for r in roots:
            for s in ast.walk(r):
                if isinstance(s, (ast.Constant, ast.JoinedStr)):
                    text = _literal_text(s)
                    if text and WORD.search(text):
                        yield s.lineno, text


def test_no_user_facing_python_message_says_workgroup():
    bad = []
    for base, _d, files in os.walk(_APP):
        if os.sep + "tests" in base:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            lines = src.split("\n")
            rel = os.path.relpath(path, _APP).replace(os.sep, "/")
            for lineno, text in set(_message_strings(ast.parse(src))):
                ctx = "\n".join(lines[max(0, lineno - 7):lineno + 2])
                if not _allowed(rel, text, ctx):
                    bad.append(f"{rel}:{lineno}: {text[:100]!r}")
    assert not bad, ("a message users read says workgroup -- it is Workstream now:\n  "
                     + "\n  ".join(sorted(bad)))


def test_the_page_answers_on_its_new_name_and_its_old_one():
    with open(os.path.join(_APP, "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert '@app.get("/workstreams"' in src
    assert '@app.get("/workgroups"' in src, "the old path is printed in runbooks; keep it"


def test_the_scanner_catches_the_old_word():
    """The guard itself: a label, a tooltip and a toast with the old word are all found."""
    src = ('<label>Workgroup</label><button title="Pick a workgroup">x</button>'
           "<script>toast('Workgroup saved.');</script>")
    assert len(_template_hits(src)) == 3, _template_hits(src)
    clean = ('<input x-model="form.workgroup"><script>API.get("/api/workgroups");'
             "const workgroup = 'x';</script><code>Workgroup</code>")
    assert _template_hits(clean) == [], _template_hits(clean)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
