"""One checklist, three surfaces, one stylesheet.

The POV use-case checklist is rendered by three templates and they are the SAME checklist:
the SE's page, the customer's page, and the read-only lead on /use-cases. The properties
here are the ones whose absence would let the customer-facing one drift without anybody
noticing -- two of the three files are never open at the same time.

  * **One stylesheet, included rather than copied.** pov/access.html does not extend
    base.html (deliberately -- see its own header), so the shell cannot reach it, and a
    partial is the only shape that serves all three. Three copies of a palette is three
    chances for the customer-facing surface to drift invisibly.
  * **Every selector is prefixed `chk-`.** The reference this format came from uses bare
    `.item`, `.section`, `.name`, `.note`, `.progress` and `.count`. Inside a Tailwind app
    those are one collision away from a layout nobody can explain, and base.html already
    owns `.progress-bar-fill` one hyphen from `.progress`.
  * **No checklist element also carries a Tailwind utility.** The Play CDN injects its
    sheet at runtime, so which of two single-class rules wins is a question about script
    timing rather than about the stylesheet. The way to never ask it is to not overlap.
  * **Only the surfaces that WRITE carry a checkbox.** /use-cases performs no writes, so a
    checkbox there would be a control that looks live and records nothing.

Runs under pytest, or standalone:
    python tests/test_checklist_format.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_STYLES = os.path.join(_TPL, "_checklist_styles.html")
_BASE = os.path.join(_TPL, "base.html")
_LEAD = os.path.join(_TPL, "use_cases.html")
_DETAIL = os.path.join(_TPL, "pov", "detail.html")
_ACCESS = os.path.join(_TPL, "pov", "access.html")
_SURFACES = (_LEAD, _DETAIL, _ACCESS)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_stylesheet_is_one_partial_all_three_surfaces_include():
    assert os.path.isfile(_STYLES), "there is no shared checklist stylesheet"
    for path in _SURFACES:
        assert "_checklist_styles.html" in _read(path), \
            f"{os.path.basename(path)} does not include the shared checklist stylesheet"


def test_the_palette_is_defined_exactly_once():
    """Three copies of a palette is three chances for the customer's page to drift from the
    SE's, and the two files are never open at the same time."""
    for path in _SURFACES + (_BASE,):
        assert "--bt-orange" not in _read(path), (
            f"{os.path.basename(path)} defines the checklist palette itself; it belongs in "
            "_checklist_styles.html and nowhere else")


def test_the_shell_offers_a_head_block_rather_than_carrying_the_rules():
    """base.html's <style> is reserved for rules that rescue EVERY page -- the table clip,
    the unbreakable token. 34 of the 36 pages that extend it have no checklist on them."""
    src = _read(_BASE)
    assert "{% block head %}" in src, "base.html has no per-page <head> block"
    assert "chk-" not in src, "checklist rules have moved into the shell's own stylesheet"


def test_every_checklist_selector_is_prefixed():
    # Jinja comments first, then the <style> element, then the CSS comments inside it. The
    # partial's own header names `.item` / `.section` / `.progress-bar-fill` while
    # explaining why they are banned, and it says "base.html's own <style>" -- so splitting
    # on the first `<style>` without stripping the comment reads the prose as a stylesheet.
    src = re.sub(r"\{#.*?#\}", "", _read(_STYLES), flags=re.S)
    css = src.split("<style>", 1)[1].split("</style>", 1)[0]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    bare = sorted({m.group(1) for m in re.finditer(r"\.([A-Za-z_][\w-]*)", css)
                   if not m.group(1).startswith("chk-")})
    assert not bare, (
        "unprefixed class selector(s) in the checklist stylesheet: %s. The reference uses "
        "bare .item/.section/.name/.note/.progress; inside a Tailwind app that is one "
        "collision from a layout nobody can explain." % bare)


_TAILWINDISH = re.compile(
    r"(?<![\w:-])(?:text|bg|border|rounded|p|px|py|pt|pb|m|mx|my|mt|mb|flex|grid|gap|w|h|"
    r"font|shadow|space|items|justify|min|max|overflow|truncate)(?:-[\w./\[\]%]+)?$")


def test_no_checklist_element_also_carries_a_tailwind_utility():
    """Which of two equal-specificity rules wins is a question about when the Play CDN
    injected its sheet. Not overlapping is how the question never comes up.

    `chk-scope` is exempt: it declares custom properties and no layout, so there is nothing
    for a utility to collide with, and on pov/access.html it rides the page's own wrapper.
    """
    offenders = []
    for path in _SURFACES:
        for m in re.finditer(r'class="([^"]*\bchk-[^"]*)"', _read(path)):
            toks = m.group(1).split()
            if "chk-scope" in toks:
                continue
            mixed = [t for t in toks
                     if not t.startswith("chk-") and _TAILWINDISH.match(t)]
            if mixed:
                offenders.append(f"{os.path.basename(path)}: {m.group(1)[:70]}")
    assert not offenders, (
        "Tailwind utilities on a checklist element:\n  " + "\n  ".join(offenders[:10]))


def test_only_the_surfaces_that_write_carry_a_checkbox():
    """The read-only lead gets a static mark. A checkbox there would be a control that
    looks live and records nothing, which is worse than no control."""
    assert 'type="checkbox"' not in _read(_LEAD), \
        "/use-cases performs no writes, so a checkbox there is a dead control"
    for path in (_DETAIL, _ACCESS):
        assert 'class="chk-box"' in _read(path), \
            f"{os.path.basename(path)} writes the checklist but offers no checkbox"


def test_the_row_is_the_same_shape_on_every_surface():
    """The format is the point of sharing the stylesheet. If one surface stops using the
    row structure the CSS is written for, the other two keep working and nobody finds out."""
    for path in _SURFACES:
        src = _read(path)
        for cls in ("chk-section", "chk-item", "chk-txt", "chk-name", "chk-note", "chk-tail"):
            assert cls in src, f"{os.path.basename(path)} does not render {cls}"


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
