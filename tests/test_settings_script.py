"""The Settings page's script, split by feature into templates/settings/script/.

``settingsPage()`` in settings.html spreads one function per feature (``...settingsAccount()``,
``...settingsPanels()``, ...), each defined in its own file and included into the page's
one ``<script>``. These pin the seams that a split like that can break without any
template or script failing to parse:

  * every file is included exactly once, and every include names a file that exists,
  * every feature file defines the function the page spreads, and the page spreads each
    one exactly once,
  * no name is defined twice across the page and its feature files. Spreading keeps the
    LAST duplicate without complaint, so a second ``openPanel`` in another file would
    silently replace the first -- the bug test_template_duplicate_methods.py guards
    against inside one object, which it cannot see across files.

Run: python tests/test_settings_script.py   (or under pytest)
"""
import collections
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_DIR = os.path.join(_TPL, "settings", "script")

_INCLUDE = re.compile(r'^\{% include "settings/script/([a-z0-9_]+)\.js" %\}$', re.M)
_SPREAD = re.compile(r"^    \.\.\.([A-Za-z_$][\w$]*)\(\),$", re.M)
_DEFINES = re.compile(r"^function ([A-Za-z_$][\w$]*)\(\) \{\n  return \{$", re.M)
_MEMBER = re.compile(r"^    (?:async +)?([A-Za-z_$][\w$]*)\s*(?:\(|:)", re.M)

#: Bound by its own x-data in the Entitle panel, not spread into settingsPage().
_OWN_COMPONENT = {"entitle_bundles"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _page():
    return _read(os.path.join(_TPL, "settings.html"))


def _files():
    return sorted(f[:-3] for f in os.listdir(_DIR) if f.endswith(".js"))


def _object_body(src, fn):
    """The text of ``function fn() { return { ... }; }``'s returned object."""
    start = src.index(f"function {fn}() {{\n  return {{\n")
    return src[start:src.index("\n  };\n}", start)]


def members(src, fn):
    """The top-level names of ``fn``'s returned object."""
    return _MEMBER.findall(_object_body(src, fn))


def duplicates(named_sources):
    """{name: [where, ...]} for every name defined more than once across the sources."""
    where = collections.defaultdict(list)
    for label, names in named_sources:
        for n in names:
            where[n].append(label)
    return {n: w for n, w in where.items() if len(w) > 1}


def _feature_sources():
    """[(label, member names)] for the page and each spread file."""
    out = [("settings.html", members(_page(), "settingsPage"))]
    for name in _files():
        if name in _OWN_COMPONENT:
            continue
        src = _read(os.path.join(_DIR, name + ".js"))
        (fn,) = _DEFINES.findall(src)
        out.append((f"settings/script/{name}.js", members(src, fn)))
    return out


def test_every_script_file_is_included_once():
    included = _INCLUDE.findall(_page())
    counts = collections.Counter(included)
    assert not [n for n, c in counts.items() if c > 1], counts
    assert sorted(included) == _files(), (
        f"settings.html includes {sorted(included)}, the directory holds {_files()}")


def test_the_page_spreads_every_feature_once():
    spread = _SPREAD.findall(_object_body(_page(), "settingsPage"))
    assert len(spread) == len(set(spread)), spread
    defined = {}
    for name in _files():
        fns = _DEFINES.findall(_read(os.path.join(_DIR, name + ".js")))
        if name in _OWN_COMPONENT:
            continue
        assert len(fns) == 1, f"settings/script/{name}.js defines {fns}, not one feature"
        defined[fns[0]] = name
    assert sorted(spread) == sorted(defined), (
        f"settingsPage() spreads {sorted(spread)}; the feature files define "
        f"{sorted(defined)}")


def test_no_name_is_defined_twice_across_the_split():
    dup = duplicates(_feature_sources())
    assert not dup, (
        "defined more than once across settingsPage() and its feature files; the later "
        f"spread silently replaces the earlier: {dup}")


def test_the_check_sees_a_duplicate_across_files():
    a = "function settingsA() {\n  return {\n    panel: null,\n    async openPanel(i) {\n    },\n  };\n}\n"
    b = "function settingsB() {\n  return {\n    openPanel() {\n    },\n  };\n}\n"
    found = duplicates([("a.js", members(a, "settingsA")), ("b.js", members(b, "settingsB"))])
    assert found == {"openPanel": ["a.js", "b.js"]}, found


def test_the_split_left_every_member_somewhere():
    names = [n for _, ns in _feature_sources() for n in ns]
    # The page's script had 148 members before the split; a member dropped while moving
    # code between files would leave a binding with nothing behind it.
    for must in ("init", "openPanel", "savePanelConfig", "loadFeatureFlags", "integrations",
                 "saveBranding", "startRegistration", "saveRegionSets", "loadDbops",
                 "showToast"):
        assert must in names, f"{must} is defined nowhere in the Settings script"
    assert len(names) == 148, len(names)


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
