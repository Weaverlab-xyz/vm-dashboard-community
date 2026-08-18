"""Guard: the Cloud Functions wiring the sandbox scripts emit stays coherent and
stays TWINNED between the bash and PowerShell variants.

Parity between `scripts/sandbox/Linux/*.sh` and `scripts/sandbox/Windows/*.ps1` is
maintained BY HAND — there is no shellcheck, PSScriptAnalyzer, or parity job in CI,
and `test_sandbox_region_keys.py` only guards region-key *names* (it would happily
pass with a whole section missing from one twin). These assertions cover the failure
modes that actually bite:

* a JSON typo in either copy of the AWS managed policy,
* the two copies of that policy drifting apart,
* the policy blowing AWS's hard 6144-byte managed-policy quota,
* a config key emitted by one twin and not the other,
* a mistyped key name — `config_service.set_many` persists arbitrary keys verbatim,
  so `function_packages_s3_bucket` would be stored forever and silently do nothing,
* a half-landed `network_mode=vpc` story (a config key with no subnet, or vice versa),
* OCI growing Cloud Functions keys it has no support for.

Reads the scripts as text: no cloud account, no fastapi, no sqlalchemy.

Runs under pytest, or standalone:  python tests/test_sandbox_cloudfn_keys.py
"""
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.config import Settings

_LINUX = os.path.join(_ROOT, "scripts", "sandbox", "Linux")
_WINDOWS = os.path.join(_ROOT, "scripts", "sandbox", "Windows")

# The per-cloud setup pairs. Rollback scripts emit no config, so they're not here.
_PAIRS = [
    ("aws", os.path.join(_LINUX, "setup-aws.sh"), os.path.join(_WINDOWS, "Setup-AwsSandbox.ps1")),
    ("azure", os.path.join(_LINUX, "setup-azure.sh"), os.path.join(_WINDOWS, "Setup-AzureSandbox.ps1")),
    ("gcp", os.path.join(_LINUX, "setup-gcp.sh"), os.path.join(_WINDOWS, "Setup-GcpSandbox.ps1")),
]
_OCI = [os.path.join(_LINUX, "setup-oci.sh"), os.path.join(_WINDOWS, "Setup-OciSandbox.ps1")]

# Every Cloud Functions config key the scripts may emit, in either dialect.
_FN_KEY = re.compile(
    r'\b(cloud_functions_enabled'
    r'|function_package_\w+'
    r'|aws_functions_\w+'
    r'|azure_functions_\w+'
    r'|gcp_functions_\w+'
    r'|secrets_azure_kv_url)\s*='
)

# Read through `_cfg()` / the secrets backend rather than a typed settings model, so
# they are legitimately absent from Settings.model_fields.
_UNDECLARED_BY_DESIGN = {"secrets_azure_kv_url"}

# The four shell/PowerShell variables interpolated into the AWS policy document.
# Substituted with identical placeholders so the two twins compare equal.
_POLICY_VARS = {
    "ACCOUNT_ID": "111122223333", "AccountId": "111122223333",
    "SANDBOX_NAME_PREFIX": "dashboard-sandbox", "Name": "dashboard-sandbox",
    "VMIMPORT_ROLE_NAME": "vmimport", "VmImportRoleName": "vmimport",
    "PROMOTE_TASK_ROLE_NAME": "promote-runner", "PromoteTaskRoleName": "promote-runner",
}

# AWS's hard cap on a customer-managed policy document.
_POLICY_QUOTA = 6144

_BASH_POLICY = re.compile(r'DASHBOARD_POLICY_DOC="\$\(jq -c \. <<JSON\n(.*?)\nJSON\n\)"', re.S)
_PS_POLICY = re.compile(r'\$DashboardPolicy = @"\n(.*?)\n"@', re.S)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _keys(path):
    return set(_FN_KEY.findall(_read(path)))


def _normalize_policy(raw):
    return re.sub(r'\$\{(\w+)\}', lambda m: _POLICY_VARS.get(m.group(1), m.group(1)), raw)


def _extract_policy(path, pattern):
    m = pattern.search(_read(path))
    assert m, f"{os.path.basename(path)}: could not find the dashboard policy document"
    return _normalize_policy(m.group(1))


def test_aws_policy_twins_match_and_fit_quota():
    """Both copies of dashboard-app-policy must be valid JSON, identical, and fit."""
    bash_raw = _extract_policy(os.path.join(_LINUX, "setup-aws.sh"), _BASH_POLICY)
    ps_raw = _extract_policy(os.path.join(_WINDOWS, "Setup-AwsSandbox.ps1"), _PS_POLICY)

    try:
        bash = json.loads(bash_raw)
    except ValueError as exc:
        raise AssertionError(f"setup-aws.sh policy is not valid JSON: {exc}") from exc
    try:
        ps = json.loads(ps_raw)
    except ValueError as exc:
        raise AssertionError(f"Setup-AwsSandbox.ps1 policy is not valid JSON: {exc}") from exc

    if bash != ps:
        b = {s.get("Sid"): s for s in bash.get("Statement", [])}
        p = {s.get("Sid"): s for s in ps.get("Statement", [])}
        detail = []
        only_bash = sorted(set(b) - set(p))
        only_ps = sorted(set(p) - set(b))
        if only_bash:
            detail.append("only in setup-aws.sh: " + ", ".join(only_bash))
        if only_ps:
            detail.append("only in Setup-AwsSandbox.ps1: " + ", ".join(only_ps))
        differing = sorted(k for k in set(b) & set(p) if b[k] != p[k])
        if differing:
            detail.append("statements differ: " + ", ".join(str(d) for d in differing))
        raise AssertionError(
            "the bash and PowerShell copies of dashboard-app-policy have drifted — "
            + "; ".join(detail or ["same Sids, different document shape"]))

    size = len(json.dumps(bash, separators=(",", ":")).encode())
    assert size <= _POLICY_QUOTA, (
        f"dashboard-app-policy compacts to {size} bytes, over AWS's {_POLICY_QUOTA}-byte "
        f"managed-policy quota. Collapse a statement to a service wildcard (see "
        f"DashboardLogs / DashboardEKS) or split the policy.")


def test_cloudfn_config_keys_are_twinned():
    """A Cloud Functions key in one variant must be in its twin."""
    failures = []
    for cloud, sh, ps1 in _PAIRS:
        only_sh = sorted(_keys(sh) - _keys(ps1))
        only_ps = sorted(_keys(ps1) - _keys(sh))
        if only_sh:
            failures.append(
                f"{cloud}: {os.path.basename(sh)} emits {only_sh} but "
                f"{os.path.basename(ps1)} does not")
        if only_ps:
            failures.append(
                f"{cloud}: {os.path.basename(ps1)} emits {only_ps} but "
                f"{os.path.basename(sh)} does not")
    assert not failures, "sandbox twin drift:\n  " + "\n  ".join(failures)


def test_cloudfn_config_keys_are_known():
    """Every emitted key must be one something actually reads."""
    known = set(Settings.model_fields) | _UNDECLARED_BY_DESIGN
    failures = []
    for _cloud, sh, ps1 in _PAIRS:
        for path in (sh, ps1):
            for key in sorted(_keys(path) - known):
                failures.append(
                    f"{os.path.basename(path)} emits {key!r}, which nothing reads — typo? "
                    f"(config_service.set_many persists it verbatim, so it fails silently)")
    assert not failures, "unknown Cloud Functions config keys:\n  " + "\n  ".join(failures)


def test_cloudfn_keys_are_flat():
    """Function keys must be flat, never per-region.

    The region-config models declare no `functions_*` fields, so a
    `<cloud>_region.<region>.functions_*` key is dropped by /api/setup/import. Flat is
    also right on the merits: one package store serves every region.
    """
    bad = []
    for _cloud, sh, ps1 in _PAIRS:
        for path in (sh, ps1):
            for line in _read(path).splitlines():
                if _FN_KEY.search(line) and re.search(r'_region\.', line):
                    bad.append(f"{os.path.basename(path)}: {line.strip()}")
    assert not bad, "Cloud Functions keys must be flat, not per-region:\n  " + "\n  ".join(bad)


def test_vpc_mode_wiring_is_complete():
    """network_mode=vpc needs BOTH halves — the grant/resource and the config key."""
    failures = []

    for path in (os.path.join(_LINUX, "setup-aws.sh"),
                 os.path.join(_WINDOWS, "Setup-AwsSandbox.ps1")):
        src, keys = _read(path), _keys(path)
        if "lambda:" not in src:
            failures.append(f"{os.path.basename(path)}: policy grants no lambda: actions")
        for key in ("aws_functions_subnet_ids", "aws_functions_security_group_ids"):
            if key not in keys:
                failures.append(f"{os.path.basename(path)}: does not emit {key}")
        # The Lambda reads its bearer secret from Secrets Manager at cold start, and the
        # private subnet has no NAT route unless a VM happens to be up.
        if "secretsmanager" not in src:
            failures.append(
                f"{os.path.basename(path)}: no Secrets Manager interface endpoint — "
                f"vpc-mode functions would deploy clean then 500 on every invoke")

    for path in (os.path.join(_LINUX, "setup-azure.sh"),
                 os.path.join(_WINDOWS, "Setup-AzureSandbox.ps1")):
        src, keys = _read(path), _keys(path)
        if "azure_functions_subnet_id" not in keys:
            failures.append(f"{os.path.basename(path)}: does not emit azure_functions_subnet_id")
        if "Microsoft.Web/serverFarms" not in src:
            failures.append(
                f"{os.path.basename(path)}: creates no subnet delegated to "
                f"Microsoft.Web/serverFarms")

    for path in (os.path.join(_LINUX, "setup-gcp.sh"),
                 os.path.join(_WINDOWS, "Setup-GcpSandbox.ps1")):
        src, keys = _read(path), _keys(path)
        for key in ("gcp_functions_network", "gcp_functions_subnetwork"):
            if key not in keys:
                failures.append(f"{os.path.basename(path)}: does not emit {key}")
        # gen2 targets the Cloud Functions v2 API and Cloud Build pushes to gcf-artifacts.
        for api in ("cloudfunctions.googleapis.com", "artifactregistry.googleapis.com"):
            if api not in src:
                failures.append(f"{os.path.basename(path)}: does not enable {api}")
        for role in ("roles/cloudfunctions.developer", "roles/secretmanager.admin",
                     "roles/artifactregistry.writer"):
            if role not in src:
                failures.append(f"{os.path.basename(path)}: does not grant {role}")

    assert not failures, "incomplete vpc-mode wiring:\n  " + "\n  ".join(failures)


def test_oci_emits_no_cloudfn_keys():
    """Cloud Functions supports aws/azure/gcp only — there is no OCI module."""
    failures = []
    for path in _OCI:
        found = sorted(_keys(path))
        if found:
            failures.append(f"{os.path.basename(path)}: emits {found}")
    assert not failures, (
        "OCI has no Cloud Functions support (no terraform/cloud_function/oci_*, no 'oci' "
        "branch in cloud_function_service) — these keys would configure nothing:\n  "
        + "\n  ".join(failures))


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
