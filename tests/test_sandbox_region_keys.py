"""Guard: every `<cloud>_region.<region>.<field>` key the sandbox scripts emit
must be a field the import parser accepts.

`/api/setup/import` validates each region key against that cloud's config model
and **silently drops** anything unrecognized (only a log line). So a script
emitting a field the resolver doesn't know produces a sandbox that looks
multi-region and isn't — with no error anywhere. This test fails loudly instead.

It reads the shell + PowerShell scripts as text, so it needs neither a cloud
account nor fastapi.

Runs under pytest, or standalone:  python tests/test_sandbox_region_keys.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services.region_config import REGION_CONFIG_CLOUDS, region_fields

_SCRIPTS = os.path.join(_ROOT, "scripts", "sandbox")

# `aws_region.$REGION.field=` (bash) or `aws_region.$($Region).field=` /
# `aws_region.$Region.field=` (PowerShell). Only the field name matters here.
_KEY = re.compile(
    r'\b(' + '|'.join(REGION_CONFIG_CLOUDS) + r')_region\.'
    r'(?:\$\{?\w+\}?|\$\(\$?\w+\)|[a-z0-9-]+)\.'
    r'(\w+)\s*='
)


def _script_files():
    for root, _dirs, files in os.walk(_SCRIPTS):
        for f in files:
            if f.endswith((".sh", ".ps1")):
                full = os.path.join(root, f)
                yield os.path.relpath(full, _ROOT).replace("\\", "/"), full


def test_emitted_region_keys_are_accepted_fields():
    failures = []
    for rel, full in _script_files():
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        for cloud, field in set(_KEY.findall(src)):
            if field not in region_fields(cloud):
                failures.append(
                    f"{rel}: emits {cloud}_region.<region>.{field}, which "
                    f"/api/setup/import would drop (not in region_fields({cloud!r}))")
    assert not failures, "Unimportable sandbox region keys:\n  " + "\n  ".join(failures)


def test_each_cloud_with_region_config_emits_something():
    """A cloud whose scripts emit no namespaced keys at all still clobbers on a
    second-region run — catch that rather than silently shipping it."""
    seen = set()
    for _rel, full in _script_files():
        with open(full, encoding="utf-8") as fh:
            for cloud, _field in _KEY.findall(fh.read()):
                seen.add(cloud)
    missing = [c for c in REGION_CONFIG_CLOUDS if c not in seen]
    assert not missing, (
        "no sandbox script emits per-region keys for: " + ", ".join(missing))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
