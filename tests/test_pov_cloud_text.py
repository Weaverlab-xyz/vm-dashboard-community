"""Text the POV cloud drivers send to a cloud API stays ASCII.

EC2 CreateSecurityGroup rejects any non-ASCII byte in GroupDescription, and an em dash in
the single-network group's description failed every non-zoned AWS POV until it was
routed through `_sg_description`. The Purdue rule texts reach three clouds -- AWS
through that filter, GCP firewall and OCI security-list rule descriptions as they are --
so a typographic dash added to one of them would pass review and fail at provision time,
on one cloud only.

Pinned here: the filter itself, every hard-coded description or display name passed to a
cloud SDK call in the four drivers, and every string the zone rules generate.

Runs under pytest, or standalone:  python tests/test_pov_cloud_text.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cloud-text")

_DRIVERS = ("pov_cloud_aws.py", "pov_cloud_gcp.py", "pov_cloud_azure.py", "pov_cloud_oci.py")
# SDK keyword arguments that carry free text to the cloud.
_TEXT_KWARGS = {"Description", "description", "GroupDescription", "display_name"}


def _non_ascii(text: str) -> bool:
    return any(ord(c) > 127 for c in text)


def test_the_security_group_filter_drops_non_ascii():
    from web_dashboard.services.pov_cloud_aws import _sg_description
    assert _sg_description("POV env — outbound only") == "POV env  outbound only"
    assert not _non_ascii(_sg_description("zone “plant” → dmz …"))
    assert len(_sg_description("x" * 400)) == 255


def test_no_hard_coded_cloud_text_is_non_ascii():
    """The literal parts of every description/display_name keyword in the drivers. An
    f-string's interpolated values are checked where they come from (the rule texts
    below; names are validated slugs in pov_cloud_template_service)."""
    failures = []
    for name in _DRIVERS:
        path = os.path.join(_ROOT, "web_dashboard", "services", name)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg not in _TEXT_KWARGS:
                continue
            for part in ast.walk(node.value):
                if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                        and _non_ascii(part.value)):
                    failures.append(f"{name}:{part.lineno} {node.arg}={part.value[:60]!r}")
    assert not failures, "non-ASCII text sent to a cloud API:\n  " + "\n  ".join(failures)


def test_the_zone_rule_texts_are_ascii():
    from web_dashboard.services import pov_zones
    strings = []

    def walk(o, where):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{where}.{k}")
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                walk(v, f"{where}[{i}]")
        elif isinstance(o, str):
            strings.append((where, o))

    walk(pov_zones.rules("10.40.0.0/16"), "rules")
    walk(pov_zones.layout("10.40.0.0/16"), "layout")
    assert any(w.endswith(".why") for w, _ in strings), "found no rule texts; re-check the walk"
    bad = [f"{w} = {s!r}" for w, s in strings if _non_ascii(s)]
    assert not bad, "zone rule text the clouds would receive is not ASCII:\n  " + "\n  ".join(bad)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   " + name)
