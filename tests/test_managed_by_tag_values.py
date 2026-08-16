"""Source-level guard: which `managed-by` tag VALUE the dashboard writes.

Two values exist and they mean different things:

* ``vm-dashboard``      — resources the DASHBOARD provisions. `/costs` reports these as
                          the "dashboard" scope (cost_service.get_aws_managed_breakdown).
* ``dashboard-sandbox`` — resources the SANDBOX bootstrapper creates
                          (scripts/sandbox/Linux/lib/common.sh). Reported as "sandbox".

A billable dashboard-created resource tagged ``dashboard-sandbox`` is invisible in the
dashboard scope — that was the bug behind the SSM interface VPC endpoints (~$7/mo each)
being missing from `/costs`. So there is exactly ONE place in the service layer that may
legitimately write ``dashboard-sandbox``, and this test pins it.

Pure text inspection — no imports, so it runs without any cloud SDK or app dependency.
Runs under pytest, or standalone:  python tests/test_managed_by_tag_values.py
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# The single sanctioned exception. The SSM-endpoint SECURITY GROUP keeps the sandbox
# value on purpose: rollback.sh:48 builds Name=tag:managed-by,Values=dashboard-sandbox
# and its step 2b deletes security groups with exactly that filter. AWS refuses DeleteVpc
# while any non-default SG remains, so retagging this would strand the SG and wedge the
# whole sandbox teardown with DependencyViolation. Security groups are free and never
# appear in Cost Explorer, so leaving it costs no cost attribution.
_ALLOWED = {
    "aws_service.py": 1,   # _ensure_ssm_vpce_security_group_sync
}

# Matches a boto3-style tag dict writing the sandbox value, e.g.
#   {"Key": "managed-by", "Value": "dashboard-sandbox"}
_SANDBOX_TAG = re.compile(
    r"""["']managed[-_]by["']\s*[,:]\s*["']Value["']?\s*:?\s*["']dashboard[-_]sandbox["']"""
)
# Broader net: the literal sandbox value anywhere near a managed-by key on one line.
_SANDBOX_VALUE = re.compile(r"""["']dashboard-sandbox["']""")


def _service_files():
    for name in sorted(os.listdir(_SERVICES)):
        if name.endswith(".py"):
            yield name, os.path.join(_SERVICES, name)


def _sandbox_value_sites():
    """{filename: [(lineno, line)]} for every line writing the literal sandbox value.

    Skips lines that merely *read* or *compare* it — cost_service defines it as a
    constant to filter on, which is the whole point of the feature."""
    hits: dict = {}
    for name, path in _service_files():
        if name == "cost_service.py":
            continue  # defines _SANDBOX_TAG_VALUE deliberately, to query with
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # a comment explaining the choice isn't a write
                if _SANDBOX_VALUE.search(line):
                    hits.setdefault(name, []).append((i, stripped))
    return hits


def test_sandbox_tag_value_only_written_at_sanctioned_sites():
    hits = _sandbox_value_sites()
    counts = {f: len(v) for f, v in hits.items()}
    unexpected = {f: hits[f] for f in hits if f not in _ALLOWED}
    assert not unexpected, (
        "web_dashboard/services writes managed-by=dashboard-sandbox in unexpected "
        f"file(s): {unexpected}. A dashboard-created resource tagged with the SANDBOX "
        "value is excluded from the dashboard scope on /costs — if it bills, that is a "
        "cost-attribution bug (see the SSM VPC endpoint regression). Use vm-dashboard, "
        "or add a justified entry to _ALLOWED."
    )
    for fname, allowed in _ALLOWED.items():
        assert counts.get(fname, 0) == allowed, (
            f"{fname}: expected {allowed} sanctioned managed-by=dashboard-sandbox "
            f"site(s), found {counts.get(fname, 0)}: {hits.get(fname)}"
        )


def test_ssm_vpc_endpoint_itself_is_dashboard_scoped():
    """The endpoint BILLS (~$7/mo each) and the dashboard owns its lifecycle, so it must
    carry vm-dashboard. rollback.sh sweeps endpoints by vpc-id, not by tag, so this does
    not affect teardown."""
    path = os.path.join(_SERVICES, "aws_service.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    fn = src.split("def _create_ssm_endpoint_sync", 1)
    assert len(fn) == 2, "_create_ssm_endpoint_sync not found in aws_service.py"
    body = fn[1].split("\ndef ", 1)[0]
    assert '"vm-dashboard"' in body, (
        "_create_ssm_endpoint_sync must tag the endpoint managed-by=vm-dashboard so its "
        "hourly cost shows in the dashboard scope on /costs."
    )
    assert '"dashboard-sandbox"' not in body


def test_sandbox_scripts_still_use_the_sandbox_value():
    """The other half of the contract: cost_service's sandbox scope only works because
    the bootstrapper tags dashboard-sandbox. If common.sh changes, /costs goes quiet."""
    common = os.path.join(_ROOT, "scripts", "sandbox", "Linux", "lib", "common.sh")
    with open(common, encoding="utf-8") as fh:
        src = fh.read()
    assert 'SANDBOX_TAG_KEY="managed-by"' in src
    assert 'SANDBOX_TAG_VALUE="dashboard-sandbox"' in src


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                fails += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if fails else 0)
