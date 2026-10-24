"""The Settings slide-over panels: one file each, every one of them included.

settings.html includes templates/settings/panels/<key>.html for each integration's
slide-over form. Two ways that comes apart without any other test noticing:

  * a panel file nobody includes renders nowhere, and the tests that read the page
    through tests/_settings_src.py never see it either,
  * a file that holds some other key's panel, or a second panel, is the wrong form
    opening under the right label.

So this pins: every include names a file that exists, every file is included exactly
once, and each file holds exactly one top-level panel, for its own key. And
settings_source() leaves no include behind, since every test reading the page relies
on that.

Templates are read as text; no app, no DB.

Run: python tests/test_settings_panels.py   (or under pytest)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _settings_src import settings_source  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")
_PANELS = os.path.join(_TEMPLATES, "settings", "panels")
_INCLUDE = re.compile(r'\{%-? include "settings/panels/([a-z0-9_]+)\.html" %\}')
_PANEL_OPEN = re.compile(r"^      <template x-if=\"panel\?\.key === '([a-z0-9_]+)'", re.M)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _included():
    return _INCLUDE.findall(_read(os.path.join(_TEMPLATES, "settings.html")))


def _files():
    return sorted(f[:-5] for f in os.listdir(_PANELS) if f.endswith(".html"))


def test_every_include_names_a_panel_file():
    missing = sorted(set(_included()) - set(_files()))
    assert not missing, f"settings.html includes panels with no file: {missing}"


def test_every_panel_file_is_included_exactly_once():
    included = _included()
    orphans = sorted(set(_files()) - set(included))
    assert not orphans, f"panel files settings.html never includes (they render nowhere): {orphans}"
    twice = sorted({k for k in included if included.count(k) > 1})
    assert not twice, f"panels included more than once: {twice}"


def test_each_file_holds_its_own_panel_and_only_that():
    for key in _files():
        keys = _PANEL_OPEN.findall(_read(os.path.join(_PANELS, f"{key}.html")))
        assert keys == [key], f"settings/panels/{key}.html opens panels {keys}, expected [{key!r}]"


def test_the_inlined_page_has_no_include_left():
    src = settings_source()
    assert not _INCLUDE.search(src), "settings_source() left a panel include in place"
    assert sorted(_PANEL_OPEN.findall(src)) == _files()


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
