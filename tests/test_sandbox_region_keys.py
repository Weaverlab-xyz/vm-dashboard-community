"""Guard: every `<cloud>_region.<region>.<field>` key the sandbox scripts emit
must be a field the import parser accepts — and the bash and PowerShell twins must
agree on which fields they emit and what each one points at.

`/api/setup/import` validates each region key against that cloud's config model
and **silently drops** anything unrecognized (only a log line). So a script
emitting a field the resolver doesn't know produces a sandbox that looks
multi-region and isn't — with no error anywhere. This test fails loudly instead.

Twin parity is hand-maintained (no shellcheck, no PSScriptAnalyzer, no parity job),
and every failure shape below has actually shipped: `Setup-AwsSandbox.ps1` omitted the
two DB parameter groups entirely and pointed `db_security_group_id` at the VM security
group instead of the DB one, and `Setup-AzureSandbox.ps1` emitted neither gallery key
because it had never carried the external image-gallery block at all.

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

from web_dashboard.services.region_config import (
    REGION_CONFIG_CLOUDS, field_fallbacks, region_fields)

_SCRIPTS = os.path.join(_ROOT, "scripts", "sandbox")
_LINUX = os.path.join(_SCRIPTS, "Linux")
_WINDOWS = os.path.join(_SCRIPTS, "Windows")

# `aws_region.$REGION.field=` (bash) or `aws_region.$($Region).field=` /
# `aws_region.$Region.field=` (PowerShell). Only the field name matters here.
_KEY = re.compile(
    r'\b(' + '|'.join(REGION_CONFIG_CLOUDS) + r')_region\.'
    r'(?:\$\{?\w+\}?|\$\(\$?\w+\)|[a-z0-9-]+)\.'
    r'(\w+)\s*='
)

# The bash/PowerShell setup pair per cloud. OCI has no region-config support.
_PAIRS = {
    "aws": (os.path.join(_LINUX, "setup-aws.sh"),
            os.path.join(_WINDOWS, "Setup-AwsSandbox.ps1")),
    "azure": (os.path.join(_LINUX, "setup-azure.sh"),
              os.path.join(_WINDOWS, "Setup-AzureSandbox.ps1")),
    "gcp": (os.path.join(_LINUX, "setup-gcp.sh"),
            os.path.join(_WINDOWS, "Setup-GcpSandbox.ps1")),
}

# Known, separately-tracked twin gaps. Listed explicitly so a NEW gap fails loudly
# rather than hiding behind an old one, and so the list shrinks as they close.
#
# Empty, and worth keeping that way: the azure gallery_* pair lived here until
# Setup-AzureSandbox.ps1 gained the external image-gallery block its bash twin had
# always carried. The stale-exemption check below is what forced this to be cleaned
# up rather than left behind.
_KNOWN_TWIN_GAPS: set = set()

# One `key=value` line out of a config array, in either dialect. The value keeps its
# variable reference ($DB_SG / $DbSg) so it can be compared with the flat key's.
_ASSIGN = re.compile(r'^([\w.${}()]+)=(.*)$')

_REGION_KEY = re.compile(
    r'(\w+)_region\.(?:\$\{?\w+\}?|\$\(\$?\w+\)|[a-z0-9-]+)\.(\w+)')


def _script_files():
    for root, _dirs, files in os.walk(_SCRIPTS):
        for f in files:
            if f.endswith((".sh", ".ps1")):
                full = os.path.join(root, f)
                yield os.path.relpath(full, _ROOT).replace("\\", "/"), full


def _emitted(path):
    """Every ``key=value`` the script prints, as {key: value-expression}."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip().rstrip(",").strip('"').strip("'")
            if not s or s.startswith("#"):
                continue
            m = _ASSIGN.match(s)
            if m:
                out[m.group(1)] = m.group(2).split("#")[0].strip()
    return out


def _per_region(path, cloud):
    """{field: value-expression} for this cloud's ``<cloud>_region.<r>.<field>``."""
    out = {}
    for key, val in _emitted(path).items():
        m = _REGION_KEY.fullmatch(key)
        if m and m.group(1) == cloud:
            out[m.group(2)] = val
    return out


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


def test_per_region_fields_match_across_twins():
    """Both variants must emit the SAME per-region fields.

    A field in one twin and not the other silently degrades that platform's
    multi-region support: the missing one falls back to the flat key, which always
    describes the *default* region. `Setup-AwsSandbox.ps1` shipped without
    db_parameter_group_name / db_mysql_parameter_group_name this way.
    """
    failures = []
    for cloud, (sh, ps1) in _PAIRS.items():
        a, b = set(_per_region(sh, cloud)), set(_per_region(ps1, cloud))
        for fld in sorted(a - b):
            if (cloud, fld) not in _KNOWN_TWIN_GAPS:
                failures.append(
                    f"{cloud}: {os.path.basename(sh)} emits {fld} but "
                    f"{os.path.basename(ps1)} does not")
        for fld in sorted(b - a):
            if (cloud, fld) not in _KNOWN_TWIN_GAPS:
                failures.append(
                    f"{cloud}: {os.path.basename(ps1)} emits {fld} but "
                    f"{os.path.basename(sh)} does not")
        # A gap that has since been closed should leave the exemption list, or it
        # goes on silently excusing a future regression on the same field.
        for gap_cloud, fld in sorted(_KNOWN_TWIN_GAPS):
            if gap_cloud == cloud and fld in a and fld in b:
                failures.append(
                    f"{cloud}: {fld} is twinned now — remove it from "
                    f"_KNOWN_TWIN_GAPS")
    assert not failures, (
        "sandbox per-region twin drift:\n  " + "\n  ".join(failures))


def test_per_region_value_matches_its_flat_fallback():
    """A per-region field and its flat fallback must name the SAME resource.

    ``resolve_region()`` takes the region entry when set and the flat key otherwise,
    so the two describe one thing — the flat key just carries the default region's
    copy. A per-region line pointing at a *different* variable is therefore always a
    bug, and a quiet one: it only bites in a NON-default region, where the wrong
    value shadows the right flat key instead of falling back to it.

    `Setup-AwsSandbox.ps1` emitted ``db_security_group_id=$VmSg`` against a flat
    ``aws_db_security_group_id=$DbSg``, which attaches the VM security group to a
    second region's RDS instance — no ingress on 5432/3306/1433 from the Gateway SG,
    so the PRA tunnel cannot reach the database.
    """
    failures = []
    for cloud, paths in _PAIRS.items():
        fallbacks = field_fallbacks(cloud)
        for path in paths:
            emitted, per = _emitted(path), _per_region(path, cloud)
            for fld, val in sorted(per.items()):
                flat = fallbacks.get(fld)
                # Only comparable when this script also emits the flat key.
                if not flat or flat not in emitted:
                    continue
                if emitted[flat] != val:
                    failures.append(
                        f"{os.path.basename(path)}: {cloud}_region.<r>.{fld}={val} "
                        f"but {flat}={emitted[flat]} — same resource, two values")
    assert not failures, (
        "per-region value disagrees with its flat fallback:\n  "
        + "\n  ".join(failures))


def test_flat_emitted_field_has_a_per_region_line():
    """A field the script emits FLAT must also be emitted per-region.

    The mirror of the check above, and the half nothing covered: that one verifies a
    per-region line agrees with its flat fallback, this one verifies the line exists at
    all. ``resolve_region()`` falls back to the flat key, which always describes the
    *configured default* region — so a script emitting ``azure_db_sqlserver_subnet_id``
    with no ``azure_region.<r>.db_sqlserver_subnet_id`` silently hands every other region
    the default region's private-endpoint subnet. Both azure twins shipped the SQL Server
    subnet, both engine-specific DNS zones and the Gateway subnet exactly that way.

    Gating on "this script also emits the flat key" is what makes the rule
    exemption-free. A region field naming something the sandbox does not create is
    operator-supplied, emits neither half, and is skipped — `aws ssm_instance_profile`,
    `gcp ecs_subnetwork` and `azure default_vm_size` are the three today. Anything the
    sandbox does create must emit both halves.
    """
    failures = []
    for cloud, paths in _PAIRS.items():
        fallbacks = field_fallbacks(cloud)
        for path in paths:
            emitted, per = _emitted(path), _per_region(path, cloud)
            for fld, flat in sorted(fallbacks.items()):
                if flat in emitted and fld not in per:
                    failures.append(
                        f"{os.path.basename(path)}: emits {flat} but no "
                        f"{cloud}_region.<r>.{fld} — every non-default region "
                        f"inherits the default region's value")
    assert not failures, (
        "flat sandbox key with no per-region twin:\n  " + "\n  ".join(failures))


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
