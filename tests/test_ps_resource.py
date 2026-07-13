"""Unit tests for ps_resource_service HCL generation + state scrubbing.

Covers the optional Password Safe VM registration (managed system + SSH-key-managed
account):
- the provider header targets BeyondTrust/passwordsafe and supplies the required
  api_account_name run-as user;
- the managed-system HCL emits both resources with the required fields, the account
  password + private_key arriving via sensitive TF_VARs (never in the HCL), and SSH
  key management (dss flag + remote_client_type=ssh + enforcement mode);
- application_host_id is opt-in (broker route);
- the cloud-native plugin shapes (ssm = AWS Systems Manager, azurevm = Azure VM SSH
  Rotation, gcpvm = GCP VM SSH Rotation) emit the plugin address in dns_name, a
  placeholder ip, no SSH-only fields, and no pushed private key (Password Safe mints
  the key);
- _scrub_state redacts password + private_key so neither lands in stashed state.

Imports ps_resource_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_ps_resource.py
"""
import json
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

# AWS Systems Manager custom-plugin shape: dns_name = {instance-id}:{region}, placeholder
# ip, the account name already carrying its ;suffix, and NO private key pushed.
_SSM = dict(name="se-lab-vm", host_name="se-lab-vm", ip_address="127.0.0.1", port=22,
            functional_account_id=42, platform_id=9, entity_type_id=1,
            workgroup_id="55", managed_account_name="adminuser;local",
            ssh_key_enforcement_mode=2, method="ssm",
            dns_name="i-0eaa6a10886717ed:us-east-1", emit_private_key=False)

# Azure VM SSH Rotation custom-plugin shape: dns_name = tenantId/subscriptionId/resourceGroup/vmName,
# placeholder ip, a PLAIN account name (no ;suffix), and NO private key pushed.
_AZ_ADDR = ("11111111-2222-3333-4444-555555555555/"
            "22222222-3333-4444-5555-666666666666/my-rg/web01")
_AZUREVM = dict(name="se-lab-vm", host_name="se-lab-vm", ip_address="127.0.0.1", port=22,
                functional_account_id=42, platform_id=11, entity_type_id=1,
                workgroup_id="55", managed_account_name="adminuser",
                ssh_key_enforcement_mode=2, method="azurevm",
                dns_name=_AZ_ADDR, emit_private_key=False)

# GCP VM SSH Rotation custom-plugin shape: dns_name = projectId/zone/instanceName,
# placeholder ip, a PLAIN account name (no ;suffix), and NO private key pushed.
_GCP_ADDR = "my-project-123/us-central1-a/web-server-01"
_GCPVM = dict(name="se-lab-vm", host_name="se-lab-vm", ip_address="127.0.0.1", port=22,
              functional_account_id=42, platform_id=12, entity_type_id=1,
              workgroup_id="55", managed_account_name="adminuser",
              ssh_key_enforcement_mode=2, method="gcpvm",
              dns_name=_GCP_ADDR, emit_private_key=False)


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


def test_ssm_system_block_uses_dns_name_and_placeholder_ip():
    hcl = ps._generate_managed_system_hcl(**_SSM)
    assert ps._line("dns_name", '"i-0eaa6a10886717ed:us-east-1"') in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
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


def test_azurevm_system_block_uses_slash_address_and_placeholder_ip():
    hcl = ps._generate_managed_system_hcl(**_AZUREVM)
    assert ps._line("dns_name", '"%s"' % _AZ_ADDR) in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 11) in hcl
    # SSH-only fields must NOT appear on the Azure VM SSH Rotation custom-plugin managed system.
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_azurevm_account_block_is_plain_name_with_no_private_key():
    hcl = ps._generate_managed_system_hcl(**_AZUREVM)
    assert ps._line("account_name", '"adminuser"') in hcl
    assert "adminuser;" not in hcl  # plain Linux user, no SSM-style ;suffix
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    assert ps._line("dss_auto_management_flag", "true") in hcl
    assert ps._line("api_enabled", "true") in hcl


def test_azurevm_header_omits_private_key_variable():
    # A declared-but-unset required var fails `terraform apply` under TF_INPUT=0.
    hcl = ps._generate_managed_system_hcl(**_AZUREVM)
    assert 'variable "ps_account_private_key"' not in hcl
    assert 'variable "ps_account_password"' in hcl


def test_azurevm_register_rejects_non_four_part_address():
    # The address validation fires synchronously (before any terraform call), so we can
    # assert it without a live provider.
    import asyncio
    for bad in ("", "tenant/sub/rg", "tenant/sub/rg/vm/extra", "no-slashes"):
        try:
            asyncio.run(ps.register_managed_system(
                name="web01", host_name="web01", functional_account_id=1, platform_id=11,
                workgroup_id="wg", method="azurevm", dns_name=bad))
            raise AssertionError("expected PSResourceError for dns_name=%r" % bad)
        except ps.PSResourceError:
            pass


# ── GCP VM SSH Rotation shape (gcpvm) — SSH-key-managed via GCE ssh-keys metadata,
# 3-part address projectId/zone/instanceName, no pushed private key. ─────────────

def test_gcpvm_system_block_uses_slash_address_and_placeholder_ip():
    hcl = ps._generate_managed_system_hcl(**_GCPVM)
    assert ps._line("dns_name", '"%s"' % _GCP_ADDR) in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 12) in hcl
    # SSH-only fields must NOT appear on the GCP VM SSH Rotation custom-plugin managed system.
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_gcpvm_account_block_is_plain_name_with_no_private_key():
    hcl = ps._generate_managed_system_hcl(**_GCPVM)
    assert ps._line("account_name", '"adminuser"') in hcl
    assert "adminuser;" not in hcl  # plain Linux user, no SSM-style ;suffix
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    assert ps._line("dss_auto_management_flag", "true") in hcl
    assert ps._line("api_enabled", "true") in hcl


def test_gcpvm_header_omits_private_key_variable():
    # A declared-but-unset required var fails `terraform apply` under TF_INPUT=0.
    hcl = ps._generate_managed_system_hcl(**_GCPVM)
    assert 'variable "ps_account_private_key"' not in hcl
    assert 'variable "ps_account_password"' in hcl


def test_gcpvm_register_rejects_non_three_part_address():
    # The address validation fires synchronously (before any terraform call), so we can
    # assert it without a live provider.
    import asyncio
    for bad in ("", "proj/zone", "proj/zone/vm/extra", "no-slashes"):
        try:
            asyncio.run(ps.register_managed_system(
                name="web01", host_name="web01", functional_account_id=1, platform_id=12,
                workgroup_id="wg", method="gcpvm", dns_name=bad))
            raise AssertionError("expected PSResourceError for dns_name=%r" % bad)
        except ps.PSResourceError:
            pass


# ── Cloud-DB onboarding shapes (dbssm = "{engine} SSM Custom Plugin"; pravault =
# "PRA Vault Username Password") — password-managed (no SSH key, dss flag off). ─

_DB_DNS = "i-0eaa6a10886717ed;us-east-1;db.abc.us-east-1.rds.amazonaws.com;appdb;C:\\Utils\\public_ssm.pem;local"
_DBSSM = dict(name="clouddb-pg", host_name="db.abc.us-east-1.rds.amazonaws.com",
              ip_address="127.0.0.1", port=5432, functional_account_id=42, platform_id=20,
              entity_type_id=1, workgroup_id="55", managed_account_name="psafe_ab12cd34ef56",
              ssh_key_enforcement_mode=2, method="dbssm", dns_name=_DB_DNS,
              emit_private_key=False, dss_auto_management=False)

_PRAVAULT = dict(name="clouddb-pg-pravault", host_name="https://pra.example.com",
                 ip_address="127.0.0.1", port=443, functional_account_id=7, platform_id=21,
                 entity_type_id=1, workgroup_id="55", managed_account_name="clouddb-pg-admin",
                 ssh_key_enforcement_mode=2, method="pravault", dns_name="",
                 emit_private_key=False, dss_auto_management=False)


def test_dbssm_system_block_uses_dns_name_placeholder_ip_and_no_ssh():
    hcl = ps._generate_managed_system_hcl(**_DBSSM)
    assert ps._line("dns_name", json.dumps(_DB_DNS)) in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 20) in hcl
    assert ps._line("port", 5432) in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_dbssm_account_is_password_managed_no_key_no_dss():
    hcl = ps._generate_managed_system_hcl(**_DBSSM)
    assert ps._line("account_name", '"psafe_ab12cd34ef56"') in hcl
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    # Password-managed, so DSS (SSH-key) auto-management is OFF but auto-management is ON.
    assert ps._line("dss_auto_management_flag", "false") in hcl
    assert ps._line("auto_management_flag", "true") in hcl
    assert 'variable "ps_account_private_key"' not in hcl


def test_dbssm_register_rejects_dns_name_without_six_parts():
    import asyncio
    for bad in ("", "a;b;c", "a;b;c;d;e;f;g", "no-semicolons"):
        try:
            asyncio.run(ps.register_managed_system(
                name="pg", host_name="pg", functional_account_id=1, platform_id=20,
                workgroup_id="wg", method="dbssm", dns_name=bad))
            raise AssertionError("expected PSResourceError for dns_name=%r" % bad)
        except ps.PSResourceError:
            pass


def test_pravault_system_uses_host_url_no_dns_no_ssh():
    hcl = ps._generate_managed_system_hcl(**_PRAVAULT)
    assert ps._line("host_name", '"https://pra.example.com"') in hcl
    assert "dns_name" not in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_pravault_account_is_the_vault_account_name_password_managed():
    hcl = ps._generate_managed_system_hcl(**_PRAVAULT)
    assert ps._line("account_name", '"clouddb-pg-admin"') in hcl
    assert "private_key" not in hcl
    assert ps._line("dss_auto_management_flag", "false") in hcl
    assert ps._line("auto_management_flag", "true") in hcl


def test_pravault_register_rejects_empty_host_name():
    import asyncio
    try:
        asyncio.run(ps.register_managed_system(
            name="pv", host_name="", functional_account_id=1, platform_id=21,
            workgroup_id="wg", method="pravault"))
        raise AssertionError("expected PSResourceError for empty host_name")
    except ps.PSResourceError:
        pass


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
