"""An x-show gate on a null object does not protect the bindings inside it.

Alpine evaluates every x-text, x-model, :attr and x-for in an element whether or not an
x-show has hidden it -- x-show only toggles `display`. So

    <div x-show="cloudForm">  ...  <input x-model="cloudForm.name">

throws "Cannot read properties of null" on every render until something sets cloudForm,
and the page's console fills with errors that bury the real ones. Four blocks did exactly
that (POV Templates' cloud editor; Workload Lab's lease and consumer panels and its SPIRE
"not linked" block). The fix for a block of reads is `<template x-if="v">`, which does not
build its children at all; for one read, `v?.field`.

This pins the rule for every template: inside an element gated by `x-show="v"`, where the
same file initialises `v: null`, each binding that dereferences `v` must guard it
(`v?.`, `v && ...`, `v ? ... :`) or be an event handler (@click and friends run only when
someone clicks a visible element).

Text checks, no app imports. Runs under pytest or standalone:
    python tests/test_null_gated_blocks.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")

# A key initialised to null anywhere in the file, top-level (`cloudForm: null,`) or inside
# an object literal (`sshKeyModal: { ..., data: null, ... }`).
_NULL_INIT = re.compile(r"\b(\w+):\s*null\b")
_GATE = re.compile(r'<(\w+)\b[^>]*\bx-show="([^"]*)"')
_PATH = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
# An Alpine binding: x-text="..", x-model="..", x-for="..", :class="..", x-show="..".
# Event handlers (@click, x-on:) are left out on purpose -- see the docstring.
_BINDING = re.compile(r'\s((?:x-(?!on:)[\w.-]+)|:[\w.-]+)="([^"]*)"')


def _element(src: str, start: int, tag: str) -> str:
    """The element opening at `start`, through its matching close tag."""
    depth = 0
    for m in re.compile(r"<(/?)%s\b[^>]*?(/?)>" % tag).finditer(src, start):
        if m.group(1):
            depth -= 1
        elif not m.group(2):
            depth += 1
        if depth == 0:
            return src[start:m.end()]
    return src[start:]


def _guarded(expr: str, var: str) -> bool:
    """`v && v.x`, `v ? v.x : ''`, or `v?.x ? v.x : ''` (the test is itself null-safe)."""
    v = re.escape(var)
    return bool(re.search(r"\b%s(\?\.\w+)*\s*(&&|\?(?!\.))" % v, expr))


def _gated_paths(expr: str, nulls: set) -> list:
    """The null-initialised paths an x-show requires to be truthy: each `&&`-joined part
    that is a plain path (`cloudForm`, `sshKeyModal.data`) ending in a null key. A negated
    or compared part (`!x`, `x === 1`) shows the element while x is null, so it gates
    nothing."""
    out = []
    for part in expr.split("&&"):
        part = part.strip()
        if _PATH.fullmatch(part) and part.rsplit(".", 1)[-1] in nulls:
            out.append(part)
    return out


def _violations():
    for root, _dirs, files in os.walk(_TEMPLATES):
        for f in files:
            if not f.endswith(".html"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, _TEMPLATES).replace("\\", "/")
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            nulls = set(_NULL_INIT.findall(src))
            for gate in _GATE.finditer(src):
                tag = gate.group(1)
                for var in _gated_paths(gate.group(2), nulls):
                    block = _element(src, gate.start(), tag)
                    bare = re.compile(r"(?<![\w?.])%s\.\w" % re.escape(var))
                    for b in _BINDING.finditer(block):
                        expr = b.group(2)
                        if bare.search(expr) and not _guarded(expr, var):
                            line = src.count("\n", 0, gate.start() + b.start()) + 1
                            yield (f"{rel}:{line} {b.group(1)}=\"{expr[:70]}\" "
                                   f"(gated by x-show on {var})")


def test_no_binding_dereferences_a_null_object_behind_an_x_show():
    found = list(_violations())
    assert not found, (
        "bindings read a null object behind an x-show (use <template x-if> or ?.):\n  "
        + "\n  ".join(found))


def test_the_check_still_sees_the_pattern():
    # A guard on the check itself: if the regexes stop matching, the test above passes by
    # finding nothing at all.
    src = ('<div x-show="form"><input x-model="form.name">'
           '<span x-text="form && form.x"></span><b @click="go(form.y)"></b>'
           '<i x-text="form?.z ? form.z : \'-\'"></i><u x-text="form.w || \'\'"></u></div>')
    m = _GATE.search(src)
    block = _element(src, m.start(), m.group(1))
    exprs = [b.group(2) for b in _BINDING.finditer(block)]
    flagged = [e for e in exprs if "form." in e and not _guarded(e, "form")]
    assert flagged == ["form.name", "form.w || ''"], flagged

    # A nested object behind a compound gate: `modal: { ..., data: null }` shown on
    # `!modal.loading && modal.data` (the Azure SSH-key panel's shape). `!modal.loading`
    # gates nothing, since it is true while data is still null.
    nulls = set(_NULL_INIT.findall("modal: { loading: false, data: null, copied: false },"))
    assert nulls == {"data"}, nulls
    assert _gated_paths("!modal.loading && modal.data", nulls) == ["modal.data"]
    assert _gated_paths("!modal.data", nulls) == []
    assert _gated_paths("modal.data === 1", nulls) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   " + name)
