"""The Settings page's source as one text, with its slide-over panels inlined.

settings.html includes one file per slide-over panel from templates/settings/panels/.
Tests that read the page as text (a binding, a label, the order of two fields) read it
through ``settings_source()`` so they see every panel wherever it now lives. Each
``{%- include "settings/panels/<key>.html" %}`` line is replaced by that file minus its
header (the ``{# … #}`` comment and any ``{%- from … %}`` import), which gives back the
page exactly as it was before the split.

Not a test file (no ``test_`` prefix): imported by tests, run by none.
"""
import os
import re

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "web_dashboard", "templates")
_INCLUDE = re.compile(r'^\{%- include "(settings/panels/[a-z0-9_]+\.html)" %\}$', re.M)
_HEADER = re.compile(r'\A(?:\{#.*?#\}\n|\{%- from [^%]*%\}\n)*', re.S)


def panel_body(rel):
    """One panel file as it reads inside the page: the header lines dropped."""
    with open(os.path.join(_TEMPLATES, rel), encoding="utf-8") as fh:
        return _HEADER.sub("", fh.read(), count=1).removesuffix("\n")


def settings_source():
    """settings.html with every panel include replaced by the panel itself."""
    with open(os.path.join(_TEMPLATES, "settings.html"), encoding="utf-8") as fh:
        src = fh.read()
    return _INCLUDE.sub(lambda m: panel_body(m.group(1)), src)


def page_for(rel, text):
    """The page a template renders in, for checks that look names up page-wide: the
    whole Settings page for one of its panels (``rel`` relative to templates/), else
    the template's own ``text``."""
    return settings_source() if rel.replace("\\", "/").startswith("settings/panels/") else text
