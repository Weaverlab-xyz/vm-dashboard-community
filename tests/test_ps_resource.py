"""Unit tests for ps_resource_service HCL generation + state scrubbing.

Covers the optional Password Safe VM registration (managed system + SSH-key-managed
account):
- the provider header targets BeyondTrust/passwordsafe and supplies the required
  api_account_name run-as user;
- the managed-system HCL emits both resources with the required fields, the account
  password + private_key arriving via sensitive TF_VARs (never in the HCL), and SSH
  key management (dss flag + remote_client_type=ssh + enforcement mode);
- application_host_id is opt-in (broker route);
- _scrub_state redacts password + private_key so neither lands in stashed state.

Imports ps_resource_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_ps_resource.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import ps_resource_service as ps  # noqa: E402

_COMMON = dict(name="se-lab-vm", host_name="se-lab-vm", ip_address="10.0.0.5", port=22,
               functional_account_id=42, platform_id=2, entity_type_id=1,
               workgroup_id="55", managed_account_name="adminuser",
               ssh_key_enforcement_mode=2)

# AWS Systems Manager custom-plugin shape: dns_name = {instance-id}:{region}, NO ip (the
# plugin targets by instance-id:region; a stray IP becomes an invalid 2nd SSM target),
# the account name already carrying its ;suffix, and NO private key pushed.
_SSM = dict(name="se-lab-vm", host_name="se-lab-vm", ip_address="", port=22,
            functional_account_id=42, platform_id=9, entity_type_id=1,
            workgroup_id="55", managed_account_name="adminuser;local",
            ssh_key_enforcement_mode=2, method="ssm",
            dns_name="i-0eaa6a10886717ed:us-east-1", emit_private_key=False)


def test_provider_header_targets_passwordsafe_with_run_as_user():
    hcl = ps._provider_header()
    assert 'source  = "BeyondTrust/passwordsafe"' in hcl
    assert "api_account_name = var.ps_api_account_name" in hcl
    assert "api_version" in hcl


def test_managed_system_hcl_has_both_resources_and_required_fields():
    hcl = ps._generate_managed_system_hcl(**_COMMON)
    assert 'resource "passwordsafe_managed_system_by_workgroup"' in hcl
    assert 'resource "passwordsafe_managed_account"' in hcl
    # workgroup_id is a string per the provider schema.
    assert 'workgroup_id             = "55"' in hcl
    assert "entity_type_id           = 1" in hcl
    assert "platform_id              = 2" in hcl
    assert "functional_account_id    = 42" in hcl
    # SSH-key management, not password auth.
    assert 'remote_client_type       = "ssh"' in hcl
    assert "ssh_key_enforcement_mode = 2" in hcl
    assert "dss_auto_management_flag = true" in hcl
    assert 'account_name             = "adminuser"' in hcl


def test_secrets_arrive_via_tf_vars_not_in_hcl():
    hcl = ps._generate_managed_system_hcl(**_COMMON)
    assert 'variable "ps_account_password"' in hcl and "sensitive = true" in hcl
    assert 'variable "ps_account_private_key"' in hcl
    assert "password                 = var.ps_account_password" in hcl
    assert "private_key              = var.ps_account_private_key" in hcl


def test_application_host_id_is_opt_in():
    assert "application_host_id" not in ps._generate_managed_system_hcl(**_COMMON)
    withhost = ps._generate_managed_system_hcl(application_host_id=7, **_COMMON)
    assert "application_host_id      = 7" in withhost
    assert "is_application_host      = false" in withhost


def test_scrub_redacts_password_and_private_key():
    state = (
        '{"resources":[{"type":"passwordsafe_managed_account","instances":'
        '[{"attributes":{"password":"placeholder","private_key":"-----BEGIN KEY-----",'
        '"account_name":"adminuser"}}]}]}'
    )
    scrubbed = ps._scrub_state(state)
    assert "placeholder" not in scrubbed
    assert "BEGIN KEY" not in scrubbed
    assert ps._REDACTED in scrubbed
    assert "adminuser" in scrubbed  # non-secret survives


def test_ssh_is_the_default_method_unchanged():
    # No method kwarg → the traditional SSH shape (regression guard for the refactor).
    hcl = ps._generate_managed_system_hcl(**_COMMON)
    assert 'remote_client_type       = "ssh"' in hcl
    assert "private_key              = var.ps_account_private_key" in hcl
    assert 'variable "ps_account_private_key"' in hcl
    assert "dns_name" not in hcl


def test_ssm_system_block_uses_dns_name_and_no_ip():
    hcl = ps._generate_managed_system_hcl(**_SSM)
    assert ps._line("dns_name", '"i-0eaa6a10886717ed:us-east-1"') in hcl
    # SSM sets no IP — the plugin targets the instance via dns_name only; a stray IP
    # (e.g. a 127.0.0.1 placeholder) is treated as a second, invalid SSM target.
    assert "ip_address" not in hcl
    assert ps._line("platform_id", 9) in hcl
    # SSH-only fields must NOT appear on the SSM custom-plugin managed system.
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_ssm_account_block_has_suffix_name_and_no_private_key():
    hcl = ps._generate_managed_system_hcl(**_SSM)
    assert ps._line("account_name", '"adminuser;local"') in hcl
    assert "private_key" not in hcl
    assert "password                 = var.ps_account_password" in hcl
    assert "dss_auto_management_flag = true" in hcl


def test_ssm_header_omits_private_key_variable():
    # A declared-but-unset required var fails `terraform apply` under TF_INPUT=0.
    hcl = ps._generate_managed_system_hcl(**_SSM)
    assert 'variable "ps_account_private_key"' not in hcl
    assert 'variable "ps_account_password"' in hcl


def test_ssm_account_name_helper():
    assert ps._ssm_account_name("adminuser", "local") == "adminuser;local"
    assert ps._ssm_account_name("svc", "arn:aws:iam::123:role/Cross") == "svc;arn:aws:iam::123:role/Cross"
    assert ps._ssm_account_name("", "") == "adminuser;local"  # blanks fall back


def test_scrub_handles_ssm_account_without_private_key():
    state = (
        '{"resources":[{"type":"passwordsafe_managed_account","instances":'
        '[{"attributes":{"password":"placeholder","account_name":"adminuser;local"}}]}]}'
    )
    scrubbed = ps._scrub_state(state)
    assert "placeholder" not in scrubbed
    assert ps._REDACTED in scrubbed
    assert "adminuser;local" in scrubbed  # non-secret survives


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
    sys.exit(1 if failures else 0)
