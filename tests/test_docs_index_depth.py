"""The two indexes link one level into the docs, and no deeper.

``README.md`` and ``docs/README.md`` are the way in, so each names the sections -- a hub
page per group of the app's menu and Settings (``docs/<section>.md``), and the
``README.md`` of each collection (``docs/profiles/``, ``docs/design/``, ...) -- and lets the
section's own hub name its pages. An index that also links pages inside a section goes
stale the next time that section is reorganised, and turns the front page into a second,
partial copy of every hub. (Each hub linking every page of its own folder is
tests/test_docs_conventions.py's job.)

Run: python tests/test_docs_index_depth.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_ROOT, "docs")
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def too_deep(index_path):
    """Links in ``index_path`` that resolve inside docs/ deeper than one level."""
    base = os.path.dirname(index_path)
    with open(index_path, encoding="utf-8") as fh:
        text = fh.read()
    bad = []
    for target in _LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
            continue
        path = os.path.normpath(os.path.join(base, target.split("#")[0]))
        rel = os.path.relpath(path, _DOCS).replace("\\", "/")
        if rel.startswith(".."):
            continue                         # outside docs/: CONTRIBUTING, runners/, ...
        parts = rel.split("/")
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == "README.md"):
            continue                         # a hub, the docs index, or a collection's index
        bad.append(target)
    return bad


def test_the_readme_links_only_the_sections():
    bad = too_deep(os.path.join(_ROOT, "README.md"))
    assert not bad, ("README.md links pages inside a docs section; link the section's "
                     f"hub instead: {bad}")


def test_the_docs_index_links_only_the_sections():
    bad = too_deep(os.path.join(_DOCS, "README.md"))
    assert not bad, ("docs/README.md links pages inside a section; the section's hub "
                     f"lists them: {bad}")


def test_the_check_sees_a_deep_link():
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "README.md")
    docs = os.path.relpath(_DOCS, d).replace("\\", "/")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(f"[a]({docs}/clouds.md) [b]({docs}/clouds/vms.md#x) [c]({docs}/profiles/README.md)\n"
                 f"[d]({docs}/profiles/demo/README.md) [e](https://example.com/x/y.md)\n")
    assert too_deep(p) == [f"{docs}/clouds/vms.md#x", f"{docs}/profiles/demo/README.md"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
