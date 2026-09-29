"""The OT tab's two image pickers, on every cloud page, split images by NAME.

`OT_ROLE` is a bake-time env var and nothing records it on the image, so the name is
the only signal the page has. The broker picker used to require the exact substring
"ot" + "-broker", while the README tells you to name the bake `ot-sim` — so a broker
named `ot-sim-broker` was offered as a CELL image (a cell with no PLCs) and never as
the broker. These are the rules that keep the two pickers apart:

* the broker picker matches `broker` anywhere in the name, case-insensitively;
* the cell picker matches `ot-sim` but hides anything carrying `broker`.

Run: python tests/test_ot_image_pickers.py   (or under pytest)
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGES = [os.path.join(_ROOT, "web_dashboard", "templates", c, "index.html")
          for c in ("azure", "aws", "gcp")]


def _method(src, name):
    m = re.search(r"\n\s*" + name + r"\(\)\s*\{(.*?)\n\s*\},\n", src, re.S)
    assert m, "%s() not found" % name
    return m.group(1)


def test_broker_picker_matches_broker_anywhere():
    for page in _PAGES:
        body = _method(open(page, encoding="utf-8").read(), "otBrokerImages")
        assert ".toLowerCase().includes('broker')" in body, page
        assert "includes('ot" + "-broker')" not in body, page


def test_cell_picker_hides_broker_images():
    for page in _PAGES:
        body = _method(open(page, encoding="utf-8").read(), "otImages")
        assert "includes('ot-sim')" in body, page
        assert "!n.includes('broker')" in body, page
        assert "toLowerCase()" in body, page


if __name__ == "__main__":
    test_broker_picker_matches_broker_anywhere()
    test_cell_picker_hides_broker_images()
    print("ok")
