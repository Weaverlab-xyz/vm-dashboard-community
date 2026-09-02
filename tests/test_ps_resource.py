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
- the three cloud-DB shapes (dbssm, dbazure, dbgcp), whose accounts are password-
  managed instead; dbssm has a per-engine positional grammar (5 fields mssql / 6 psql /
  7 mysql), a 12-character assumeRole floor and — alone among the plugin shapes — the
  packed address as its ip rather than a placeholder (the platform requires an
  IPAddress, the plugin crashes parsing a bare one); dbgcp additionally has an
  options-bearing address grammar and its own tighter 249-character limit;
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
# "PRA Vault Username Password") — password-managed (no SSH key, dss flag off). The
# dbssm address is PER-ENGINE (5 fields mssql / 6 psql / 7 mysql), its assumeRole
# segment must be ≥ 12 characters (the plugin Substring(0,12)'s it), and it rides
# DnsName ONLY: the ip is the same 127.0.0.1 placeholder every other plugin shape uses,
# because Password Safe validates IPAddress as a literal IP (live "Bad IP value")
# while refusing a create with none at all (live "The field 'IPAddress' is
# required."). ────────────────────────────────────────────────────────────────────

_DB_DNS = ("i-0eaa6a10886717ed;us-east-1;db.abc.us-east-1.rds.amazonaws.com;appdb;"
           "C:\\Utils\\public_ssm.pem;NoAssumeRole")
_DBSSM = dict(name="clouddb-pg", host_name="db.abc.us-east-1.rds.amazonaws.com",
              ip_address="127.0.0.1", port=5432, functional_account_id=42, platform_id=20,
              entity_type_id=1, workgroup_id="55", managed_account_name="psafe_ab12cd34ef56",
              ssh_key_enforcement_mode=2, method="dbssm", dns_name=_DB_DNS,
              emit_private_key=False, dss_auto_management=False)

_PRAVAULT = dict(name="clouddb-pg-pravault", host_name="https://pra.example.com",
                 ip_address="127.0.0.1", port=443, functional_account_id=7, platform_id=21,
                 entity_type_id=1, workgroup_id="55", managed_account_name="clouddb-pg-admin",
                 ssh_key_enforcement_mode=2, method="pravault",
                 dns_name="https://pra.example.com",
                 emit_private_key=False, dss_auto_management=False)


def test_dbssm_system_block_carries_the_packed_address_in_dns_name_only():
    hcl = ps._generate_managed_system_hcl(**_DBSSM)
    assert ps._line("dns_name", json.dumps(_DB_DNS)) in hcl
    # The packed address goes in DnsName, which has no validation; the ip is the same
    # 127.0.0.1 placeholder every other plugin shape uses, because Password Safe
    # validates IPAddress as a literal IP.
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 20) in hcl
    assert ps._line("port", 5432) in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_dbssm_registration_defaults_the_ip_to_the_placeholder():
    """Two live regressions, one field. Registering with NO ip is rejected with "The
    field 'IPAddress' is required." (2026-08-25); registering with the packed address
    as the ip — the fix for that one — is rejected with "Bad IP value: '<address>' in
    'IPAddress' field" (2026-08-27), each time after a multi-minute RDS apply. Password
    Safe validates IPAddress as a literal IP, so the packed address cannot live there
    and an empty ip must default to the 127.0.0.1 placeholder."""
    import asyncio
    captured = {}

    def _capture(hcl, tf_vars, tenant=None):
        captured["hcl"] = hcl
        return {"managed_system_id": "1", "managed_account_id": "2", "tf_state_json": None}

    real = ps._apply_hcl_sync
    ps._apply_hcl_sync = _capture
    try:
        asyncio.run(ps.register_managed_system(
            name="pg", host_name="pg", functional_account_id=1, platform_id=20,
            workgroup_id="wg", method="dbssm", dns_name=_DB_DNS))
    finally:
        ps._apply_hcl_sync = real
    assert ps._line("ip_address", '"127.0.0.1"') in captured["hcl"]
    assert ps._line("dns_name", json.dumps(_DB_DNS)) in captured["hcl"]


def test_dbssm_registration_sets_the_plugin_sleep_timeout():
    """The vendor article's "timeout in milliseconds is used to give more time for
    Systems Manager to provide status" note, pinned. Every SSM DB action reads
    GetCommandInvocation once, and only if the status is still "InProgress" does
    `Thread.Sleep(timeout)` and read again — where `timeout` is the managed system's
    own Timeout field. Password Safe defaults it to 30, i.e. 30 MILLISECONDS, after
    which the second read is still "InProgress" — a status the action treats as neither
    Failed nor Success, so it falls through and reports SUCCESS for a rotation it never
    confirmed. Not setting this field is therefore not a slow rotation, it is a silently
    unverified one."""
    import asyncio
    captured = {}

    def _capture(hcl, tf_vars, tenant=None):
        captured["hcl"] = hcl
        return {"managed_system_id": "1", "managed_account_id": "2", "tf_state_json": None}

    real = ps._apply_hcl_sync
    ps._apply_hcl_sync = _capture
    try:
        asyncio.run(ps.register_managed_system(
            name="pg", host_name="pg", functional_account_id=1, platform_id=20,
            workgroup_id="wg", method="dbssm", dns_name=_DB_DNS))
    finally:
        ps._apply_hcl_sync = real
    assert ps._line("timeout", ps._DBSSM_PLUGIN_TIMEOUT_MS) in captured["hcl"]
    # Milliseconds, and comfortably longer than an SSM shell round-trip plus a psql
    # ALTER USER against RDS. A seconds-shaped value here is the 30ms bug again.
    assert ps._DBSSM_PLUGIN_TIMEOUT_MS >= 10000


def test_the_ssh_shape_does_not_carry_a_plugin_timeout():
    # `timeout` exists for the custom plugin's Thread.Sleep, not for Password Safe's own
    # SSH connection, so the traditional shape must be left exactly as it was.
    assert "timeout" not in ps._generate_managed_system_hcl(**_COMMON)


def test_dbssm_account_is_password_managed_no_key_no_dss():
    hcl = ps._generate_managed_system_hcl(**_DBSSM)
    assert ps._line("account_name", '"psafe_ab12cd34ef56"') in hcl
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    # Password-managed, so DSS (SSH-key) auto-management is OFF but auto-management is ON.
    assert ps._line("dss_auto_management_flag", "false") in hcl
    assert ps._line("auto_management_flag", "true") in hcl
    assert 'variable "ps_account_private_key"' not in hcl


def test_dbssm_register_rejects_addresses_the_plugin_would_crash_on():
    # Each entry is a shape the plugin dies on mid-rotation ("Index was outside the
    # bounds of the array" / "Index and length must refer to a location within the
    # string") rather than at registration, so the validator has to catch it here.
    import asyncio
    bad = (
        "",                                                     # blank
        "no-semicolons",
        "a;b;c",                                                # no engine has 3 fields
        ("i-0eaa6a10886717ed;us-east-1;db;x;C:\\c.cer;"
         "NoAssumeRole;sslTRUE;extra"),                         # none has 8 either
        # six fields but field 1 is not an instance id (segments shifted / wrong order)
        ("db.abc.us-east-1.rds.amazonaws.com;us-east-1;i-0eaa6a10886717ed;appdb;"
         "C:\\c.cer;NoAssumeRole"),
        # the old dashboard default: assumeRole shorter than the plugin's Substring(0,12)
        ("i-0eaa6a10886717ed;us-east-1;db.abc.us-east-1.rds.amazonaws.com;appdb;"
         "C:\\c.cer;local"),
        # an empty certPath — every position is consumed, so blank is a broken command
        ("i-0eaa6a10886717ed;us-east-1;db.abc.us-east-1.rds.amazonaws.com;appdb;"
         ";NoAssumeRole"),
        # seven fields (mysql) whose ssl flag is not a canonical spelling — anything
        # but the literal sslTRUE silently disables TLS
        ("i-0eaa6a10886717ed;us-east-1;db.abc.us-east-1.rds.amazonaws.com;appdb;"
         "C:\\c.cer;NoAssumeRole;ssl"),
    )
    for addr in bad:
        try:
            asyncio.run(ps.register_managed_system(
                name="pg", host_name="pg", functional_account_id=1, platform_id=20,
                workgroup_id="wg", method="dbssm", dns_name=addr))
            raise AssertionError("expected PSResourceError for dns_name=%r" % addr)
        except ps.PSResourceError:
            pass


def test_dbssm_accepts_each_engines_layout():
    # 5 fields = mssql (NO database segment), 6 = psql, 7 = mysql (trailing ssl flag);
    # the assumeRole segment takes the placeholder or a full cross-account role ARN.
    base = ("i-0eaa6a10886717ed;us-east-2;"
            "clouddb-074c3615.cjtnhgpj0e7l.us-east-2.rds.amazonaws.com")
    cert = "C:\\BeyondTrust\\certs\\aws_public_cert.cer"
    for addr in (f"{base};{cert};NoAssumeRole",
                 f"{base};app_db;{cert};NoAssumeRole",
                 f"{base};app_db;{cert};NoAssumeRole;sslTRUE",
                 f"{base};app_db;{cert};NoAssumeRole;sslFALSE",
                 f"{base};app_db;{cert};arn:aws:iam::123456789012:role/psafe-broker"):
        ps._validate_dbssm_dns_name(addr)          # must not raise


def test_dbssm_register_refuses_an_ip_field_that_is_not_an_ip():
    # Password Safe validates IPAddress as a literal IP and fails the whole apply with
    # "Bad IP value: '<value>' in 'IPAddress' field" — after the database is already
    # built. Caught here, before Terraform runs, and specifically for the packed
    # address: that was the shape this field carried live on 2026-08-27.
    import asyncio
    for bad_ip in (_DB_DNS, "db.abc.us-east-1.rds.amazonaws.com"):
        try:
            asyncio.run(ps.register_managed_system(
                name="pg", host_name="pg", functional_account_id=1, platform_id=20,
                workgroup_id="wg", method="dbssm", dns_name=_DB_DNS, ip_address=bad_ip))
            raise AssertionError("expected PSResourceError for ip_address=%r" % bad_ip)
        except ps.PSResourceError as exc:
            assert "Bad IP value" in str(exc)


def test_a_realistic_mysql_ssm_address_still_fits():
    # The 7-field mysql form with a full cross-account ARN is the longest AWS shape;
    # guard against the 255 limit being tighter than the real thing.
    addr = ";".join(["i-0eaa6a10886717ed", "us-east-2",
                     "clouddb-074c3615.cjtnhgpj0e7l.us-east-2.rds.amazonaws.com",
                     "app_db", r"C:\BeyondTrust\certs\aws_public_cert.cer",
                     "arn:aws:iam::123456789012:role/psafe-cross-account", "sslTRUE"])
    assert len(addr) <= ps._MAX_MANAGED_SYSTEM_ADDRESS, len(addr)
    ps._validate_dbssm_dns_name(addr)              # must not raise
    ps._check_address_length(addr, "dbssm")        # must not raise


def test_pravault_system_carries_the_appliance_url_in_host_and_dns_no_ssh():
    # The "PRA Vault Username Password" platform REQUIRES a DnsName on managed-system
    # create (live 400 "DnsName is required" — the first OT cell deploy failed on it),
    # so the appliance URL rides in both host_name and dns_name.
    hcl = ps._generate_managed_system_hcl(**_PRAVAULT)
    assert ps._line("host_name", '"https://pra.example.com"') in hcl
    assert ps._line("dns_name", '"https://pra.example.com"') in hcl
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_pravault_register_defaults_dns_name_to_the_appliance_url():
    # None of the pravault callers (OT cell, cloud-DB, k8s token) pass dns_name — the
    # register branch itself must fill it, or every mirror create 400s as above.
    import asyncio
    captured = {}

    def _fake_apply(hcl, tf_vars, tenant=None):
        captured["hcl"] = hcl
        return {"tf_state_json": "{}", "managed_system_id": "1", "managed_account_id": "2"}

    real = ps._apply_hcl_sync
    ps._apply_hcl_sync = _fake_apply
    try:
        asyncio.run(ps.register_managed_system(
            name="pv", host_name="https://pra.example.com", functional_account_id=1,
            platform_id=21, workgroup_id="wg", managed_account_name="cell-adminuser",
            ip_address="127.0.0.1", port=443, method="pravault"))
    finally:
        ps._apply_hcl_sync = real
    assert ps._line("dns_name", '"https://pra.example.com"') in captured["hcl"]


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


# ── Azure cloud-DB onboarding shape (dbazure = "{engine} Azure Run Command Plugin")
# — eight ;-separated address fields, real port, password-managed (no SSH key). ──

_DBAZURE_DNS = (
    "clouddb-jumpoint;rg-net-prod;11111111-2222-3333-4444-555555555555;"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee;mydb.postgres.database.azure.com;"
    "postgres;C:\\BeyondTrust\\certs\\public_cert.cer;sslTRUE")
_DBAZURE = dict(name="clouddb-pg", host_name="mydb.postgres.database.azure.com",
                ip_address="127.0.0.1", port=5432, functional_account_id=42, platform_id=30,
                entity_type_id=1, workgroup_id="55", managed_account_name="psafe_ab12cd34ef56",
                ssh_key_enforcement_mode=2, method="dbazure", dns_name=_DBAZURE_DNS,
                emit_private_key=False, dss_auto_management=False)


def test_dbazure_system_block_uses_eight_field_dns_placeholder_ip_and_no_ssh():
    hcl = ps._generate_managed_system_hcl(**_DBAZURE)
    assert ps._line("dns_name", json.dumps(_DBAZURE_DNS)) in hcl
    assert _DBAZURE_DNS.count(";") == 7          # eight ;-separated fields
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 30) in hcl
    assert ps._line("port", 5432) in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_dbazure_account_is_password_managed_no_key_no_dss():
    hcl = ps._generate_managed_system_hcl(**_DBAZURE)
    assert ps._line("account_name", '"psafe_ab12cd34ef56"') in hcl
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    assert ps._line("dss_auto_management_flag", "false") in hcl
    assert ps._line("auto_management_flag", "true") in hcl
    assert 'variable "ps_account_private_key"' not in hcl


def test_dbazure_register_pins_the_plugin_timeout_in_seconds():
    """Live 2026-09-02: this branch registered no timeout, so the field sat at Password
    Safe's default of 30, the plugin printed "timeout 30000 msec" (the field x1000, so it
    reads SECONDS) and killed Verify Functional Account 31s in with "Thread was
    interrupted from a waiting state". One Azure Run Command round trip is 20-60s."""
    import asyncio
    captured = {}

    def _capture(hcl, tf_vars, tenant=None):
        captured["hcl"] = hcl
        return {"managed_system_id": "1", "managed_account_id": "2", "tf_state_json": None}

    real = ps._apply_hcl_sync
    ps._apply_hcl_sync = _capture
    try:
        asyncio.run(ps.register_managed_system(
            name="clouddb-pg", host_name="mydb.postgres.database.azure.com", port=5432,
            functional_account_id=42, platform_id=30, workgroup_id="55",
            managed_account_name="psafe_x", method="dbazure", dns_name=_DBAZURE_DNS))
    finally:
        ps._apply_hcl_sync = real
    assert ps._line("timeout", 180) in captured["hcl"], captured["hcl"]
    assert ps._DBAZURE_PLUGIN_TIMEOUT_SECONDS == 180
    # Seconds, like GCP -- never the AWS millisecond number, which would read as 30000
    # SECONDS here. And >= 90 so the plugin's own 409 ladder (5 retries at 15s, because
    # every database shares one jump VM) fits inside the wait instead of being cut off.
    assert ps._DBAZURE_PLUGIN_TIMEOUT_SECONDS == ps._DBGCP_PLUGIN_TIMEOUT_SECONDS
    assert ps._DBAZURE_PLUGIN_TIMEOUT_SECONDS != ps._DBSSM_PLUGIN_TIMEOUT_MS
    assert ps._DBAZURE_PLUGIN_TIMEOUT_SECONDS >= 90


def test_dbazure_register_rejects_dns_name_without_eight_parts():
    import asyncio
    for bad in ("", "a;b;c", "a;b;c;d;e;f",                    # too few
                "a;b;c;d;e;f;g;h;i", "no-semicolons"):         # too many / none
        try:
            asyncio.run(ps.register_managed_system(
                name="pg", host_name="pg", functional_account_id=1, platform_id=30,
                workgroup_id="wg", method="dbazure", dns_name=bad))
            raise AssertionError("expected PSResourceError for dns_name=%r" % bad)
        except ps.PSResourceError:
            pass


def test_an_over_long_managed_system_address_is_refused_up_front():
    # Password Safe's address column is 255 chars and the eight-field Azure address gets
    # close with two GUIDs, a flexible-server FQDN and a broker cert path. Over the limit
    # the address is rejected or truncated, and a truncated one fails later inside the
    # plugin as an unparseable field — so it must fail here, naming the number.
    import asyncio
    long_rg = "rg-" + "x" * 200
    azure = ";".join(["jump-vm", long_rg, "11111111-2222-3333-4444-555555555555",
                      "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "db.mysql.database.azure.com",
                      "appdb", r"C:\certs\public_cert.cer", "sslTRUE"])
    assert len(azure) > ps._MAX_MANAGED_SYSTEM_ADDRESS
    try:
        asyncio.run(ps.register_managed_system(
            name="pg", host_name="pg", functional_account_id=1, platform_id=30,
            workgroup_id="wg", method="dbazure", dns_name=azure))
        raise AssertionError("expected PSResourceError for an over-long address")
    except ps.PSResourceError as exc:
        assert "255" in str(exc) and str(len(azure)) in str(exc)


def test_a_realistic_azure_address_still_fits():
    # Guard against the check being so tight it rejects the real thing: this is the shape
    # the dashboard actually builds for a provisioned Azure flexible server.
    addr = ";".join(["clouddb-jumpoint", "rg-clouddb-eastus2",
                     "11111111-2222-3333-4444-555555555555",
                     "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                     "clouddb-abcdef01.postgres.database.azure.com", "appdb",
                     r"C:\BeyondTrust\certs\public_cert.cer", "sslTRUE"])
    assert len(addr) <= ps._MAX_MANAGED_SYSTEM_ADDRESS, len(addr)
    ps._check_address_length(addr, "dbazure")   # must not raise


def test_self_rotation_emits_use_own_credentials_for_the_db_plugins():
    # "Change Password Using Own Credentials" is what makes Password Safe call the DB
    # plugin's self-rotate action (ALTER USER on self / CURRENT_USER() / OLD_PASSWORD)
    # instead of the via-functional-account one, which needs CREATEROLE / CREATE USER /
    # ALTER ANY LOGIN on the target server. A provisioned server has no such login, so
    # without this flag every rotation fails long after a green provisioning job.
    for base in (_DBSSM, _DBAZURE):
        hcl = ps._generate_managed_system_hcl(**dict(base, use_own_credentials=True))
        assert ps._line("use_own_credentials", "true") in hcl


def test_use_own_credentials_is_omitted_not_false_by_default():
    # Omitted rather than emitted false, so re-running against an account an operator
    # already flipped in BeyondInsight does not silently turn self-rotation back off.
    for base in (_DBSSM, _DBAZURE):
        assert "use_own_credentials" not in ps._generate_managed_system_hcl(**base)


def test_the_vm_and_k8s_paths_never_self_rotate():
    # Their credential is minted BY the plugin (SSH key / SA token), not rotated by the
    # account itself, so register_managed_system must not thread the flag into them.
    import inspect
    src = inspect.getsource(ps.register_managed_system)
    for method in ('method="ssm"', 'method="azurevm"', 'method="gcpvm"',
                   'method="k8ssa"', 'method="pravault"'):
        i = src.index(method)
        assert "use_own_credentials" not in src[i:i + 200], method


def test_dbazure_is_a_recognised_password_managed_plugin_method():
    assert "dbazure" in ps._PLUGIN_METHODS
    assert "dbazure" in ps._PASSWORD_MANAGED_METHODS


# -- GCP cloud-DB onboarding shape (dbgcp = "GCP Cloud SQL {engine}") -- five
# positional address fields plus key=value options, real port, password-managed. The
# plugin reaches the private instance over the Cloud SQL Data API, so unlike its two
# siblings the address carries no host, no cert path and no key material at all. --

_DBGCP_DNS = "data-api;acme-data-prod:us-central1:clouddb-ab12cd34;appdb;-;-;iam=true"
_DBGCP = dict(name="clouddb-pg", host_name="10.102.0.3",
              ip_address="127.0.0.1", port=5432, functional_account_id=42, platform_id=31,
              entity_type_id=1, workgroup_id="55", managed_account_name="psafe_ab12cd34ef56",
              ssh_key_enforcement_mode=2, method="dbgcp", dns_name=_DBGCP_DNS,
              emit_private_key=False, dss_auto_management=False)


def test_dbgcp_system_block_uses_channel_address_placeholder_ip_and_no_ssh():
    hcl = ps._generate_managed_system_hcl(**_DBGCP)
    assert ps._line("dns_name", json.dumps(_DBGCP_DNS)) in hcl
    assert _DBGCP_DNS.split(";")[0] == "data-api"
    assert ps._line("ip_address", '"127.0.0.1"') in hcl
    assert ps._line("platform_id", 31) in hcl
    assert ps._line("port", 5432) in hcl
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_dbgcp_account_is_password_managed_no_key_no_dss():
    hcl = ps._generate_managed_system_hcl(**_DBGCP)
    assert ps._line("account_name", '"psafe_ab12cd34ef56"') in hcl
    assert "private_key" not in hcl
    assert ps._line("password", "var.ps_account_password") in hcl
    assert ps._line("dss_auto_management_flag", "false") in hcl
    assert 'variable "ps_account_private_key"' not in hcl


def test_dbgcp_is_a_recognised_password_managed_plugin_method():
    assert "dbgcp" in ps._PLUGIN_METHODS
    assert "dbgcp" in ps._PASSWORD_MANAGED_METHODS


def test_dbgcp_register_rejects_a_malformed_address():
    # The grammar is positional-fields-plus-options, not a fixed semicolon count like
    # dbssm/dbazure, so each rule gets its own case.
    import asyncio
    bad = (
        "",                                                     # blank
        "data-api;p:r:i;db;-",                                  # only four fields
        "magic;p:r:i;db;-;-",                                   # unknown channel
        "data-api;not-a-connection-name;db;-;-",                # field 2 not project:region:instance
        "admin-api;p:r:i;postgres;-;-",                         # db name on a channel that opens none
        "data-api;p:r:i;-;-;-",                                 # data-api needs a db name
        "data-api;p:r:i;db;-;sslTRUE",                          # SSL flag on a control-plane channel
        "data-api;p:r:i;db;https://x;-",                        # audience on a control-plane channel
        "cloud-run;p:r:i;db;https://x/path;sslTRUE",            # audience with a path
        "data-api;p:r:i;db;-;-;bogus=1",                        # unrecognised option
        "data-api;p:r:i;db;-;-;ver=1",                          # cloud-run-only option
        # fasecret= must be a REGIONAL secret version. The global form is what the
        # plugin article's own example prints and what the Secrets page produces, and
        # the Data API rejects it at rotation time -- days after the address was
        # written, from a Password Safe error that names none of this.
        "data-api;p:r:i;db;-;-;fasecret=projects/p/secrets/s/versions/latest",
        "data-api;p:r:i;db;-;-;fasecret=projects/p/locations/r/secrets/s",
        "data-api;p:r:i;db;-;-;fasecret=bt-fa-secret",
        # The two Data API database-session auth modes are alternatives, not a pair:
        # a token minted per connection, or a stored password. Both together is how a
        # SQL Server address looks when it kept the postgres/mysql default.
        ("data-api;p:r:i;db;-;-;iam=true;"
         "fasecret=projects/p/locations/r/secrets/s/versions/latest"),
    )
    for addr in bad:
        try:
            asyncio.run(ps.register_managed_system(
                name="pg", host_name="pg", functional_account_id=1, platform_id=31,
                workgroup_id="wg", method="dbgcp", dns_name=addr))
            raise AssertionError("expected PSResourceError for dns_name=%r" % addr)
        except ps.PSResourceError:
            pass


def test_dbgcp_accepts_the_shapes_the_dashboard_actually_builds():
    for addr in ("data-api;acme:us-central1:clouddb-ab12cd34;appdb;-;-;iam=true",
                 # MySQL carries the host qualifier the plugin refuses to assume.
                 "data-api;acme:us-central1:clouddb-ab12cd34;appdb;-;-;iam=true;host=%",
                 # SQL Server on the control plane: no IAM database authentication
                 # exists for it, so the session authenticates with a stored password
                 # named by a REGIONAL secret version, and iam= is absent entirely.
                 ("data-api;acme:us-central1:clouddb-ab12cd34;master;-;-;"
                  "fasecret=projects/acme/locations/us-central1/secrets/"
                  "clouddb-ab12cd34-psfa/versions/latest")):
        ps._validate_dbgcp_dns_name(addr)          # must not raise


def test_dbgcp_fasecret_error_names_the_regional_form():
    """The message is the whole value of this check: the Data API's own error names a
    format and not a fix, and the article documents the wrong one."""
    try:
        ps._validate_dbgcp_dns_name(
            "data-api;p:r:i;master;-;-;fasecret=projects/p/secrets/s/versions/latest")
        raise AssertionError("global secret form accepted")
    except ps.PSResourceError as exc:
        assert "locations" in str(exc), exc
        assert "regional" in str(exc).lower(), exc


def test_dbgcp_length_message_mentions_the_secret_version():
    """A fasecret= value is ~110 characters, so it is now one of the fields that can
    push an address over 249 — and the message that lists the long fields is the only
    place an operator learns which one to shorten."""
    over = ("data-api;acme:us-central1:" + ("i" * 150) + ";master;-;-;"
            "fasecret=projects/acme/locations/us-central1/secrets/x/versions/latest")
    assert len(over) > ps._DBGCP_MAX_ADDRESS, len(over)
    try:
        ps._validate_dbgcp_dns_name(over)
        raise AssertionError("expected the 249-character limit to reject this")
    except ps.PSResourceError as exc:
        assert "fasecret" in str(exc), exc


def test_dbgcp_uses_the_plugins_tighter_249_limit_not_password_safes_255():
    # Password Safe truncates at 255 but the plugin refuses at 249, and a truncated
    # address does not error -- it silently becomes a different, wrong address. Building
    # to the looser limit would emit addresses the plugin rejects at every rotation.
    assert ps._DBGCP_MAX_ADDRESS == 249 < ps._MAX_MANAGED_SYSTEM_ADDRESS
    over = "data-api;acme:us-central1:" + ("i" * 216) + ";appdb;-;-"
    assert 249 < len(over) <= 255, len(over)
    ps._check_address_length(over, "dbgcp")        # the shared 255 check would let it through
    try:
        ps._validate_dbgcp_dns_name(over)
        raise AssertionError("expected the 249-character limit to reject %d chars" % len(over))
    except ps.PSResourceError as exc:
        assert "249" in str(exc), exc


def test_dbgcp_rejects_verifier_on_off_the_cloud_run_channel():
    """'verifier=on' says the new password was pre-hashed on the Resource Broker so the
    plaintext never reaches the wire. Only cloud-run can honour that -- the Cloud SQL
    APIs take the password in the statement text. It used to parse on every channel and
    do nothing on the control-plane ones, which reports a protection that is not
    happening; the plugin refuses it now, and so does this."""
    for channel in ("data-api", "admin-api"):
        db = "-" if channel == "admin-api" else "appdb"
        for value in ("on", "true", "ON"):
            try:
                ps._validate_dbgcp_dns_name(
                    f"{channel};acme:us-central1:i;{db};-;-;verifier={value}")
                raise AssertionError(f"verifier={value} accepted on {channel}")
            except ps.PSResourceError as exc:
                assert "cloud-run" in str(exc), exc
    # 'off' promises nothing, so it stays legal everywhere -- including on the channels
    # that could not honour 'on'.
    ps._validate_dbgcp_dns_name("data-api;acme:us-central1:i;appdb;-;-;verifier=off")
    ps._validate_dbgcp_dns_name("admin-api;acme:us-central1:i;-;-;-;verifier=off")
    ps._validate_dbgcp_dns_name(
        "cloud-run;acme:us-central1:i;appdb;https://svc.run.app;sslTRUE;verifier=on")


def test_dbgcp_rejects_iam_false_on_the_data_api_channel():
    """It used to parse and leave the address with no way to authenticate AT ALL:
    executeSql has no plaintext-password field and fasecret= is SQL Server only. So it
    onboarded cleanly and failed at the first rotation with an opaque Google 401 --
    exactly the class of failure this validator exists to move forward in time."""
    for value in ("false", "off", "0", "FALSE"):
        try:
            ps._validate_dbgcp_dns_name(
                f"data-api;acme:us-central1:i;appdb;-;-;iam={value}")
            raise AssertionError(f"iam={value} accepted on data-api")
        except ps.PSResourceError as exc:
            # The message has to name all three escapes, or the operator's only move is
            # to flip it back to true and hope.
            assert "iam=true" in str(exc) and "fasecret" in str(exc), exc
            assert "cloud-run" in str(exc), exc
    ps._validate_dbgcp_dns_name("data-api;acme:us-central1:i;appdb;-;-;iam=true")


def test_dbgcp_fasecret_region_must_match_the_instances_region():
    """Google reads the secret through the INSTANCE'S regional endpoint, so a secret in
    the wrong region fails at the first rotation with an error naming neither region --
    and the address already carries the instance's region in field 2, so there is
    nothing to look up."""
    good = ("data-api;acme:us-central1:i;master;-;-;"
            "fasecret=projects/acme/locations/us-central1/secrets/s/versions/latest")
    ps._validate_dbgcp_dns_name(good)          # must not raise
    bad = good.replace("locations/us-central1", "locations/us-east1")
    try:
        ps._validate_dbgcp_dns_name(bad)
        raise AssertionError("a cross-region fasecret was accepted")
    except ps.PSResourceError as exc:
        assert "us-east1" in str(exc) and "us-central1" in str(exc), exc


def test_dbgcp_global_fasecret_says_moving_the_secret_will_not_help():
    """The global form is what every Secret Manager quickstart produces, so a generic
    "not a resource name" would send the operator to re-read the value rather than
    re-create the secret. Google refuses a globally-created secret even when it is
    stored in the right region, and the message has to say so."""
    try:
        ps._validate_dbgcp_dns_name(
            "data-api;acme:us-central1:i;master;-;-;"
            "fasecret=projects/acme/secrets/s/versions/latest")
        raise AssertionError("global secret form accepted")
    except ps.PSResourceError as exc:
        text = str(exc)
        assert "GLOBAL" in text or "global" in text, text
        assert "does not help" in text, text


def test_dbgcp_registers_a_timeout_in_seconds_not_milliseconds():
    """Password Safe stores ONE timeout integer and each plugin decides its unit: the
    AWS SSM plugins read milliseconds out of this field, the GCP Cloud SQL plugins read
    seconds. Leaving it unset is not neutral -- Password Safe defaults it to 30, which
    these plugins read as 30 SECONDS, and a Direct-VPC cold start on Cloud Run is
    documented at a minute or more. So the first rotation after an idle period is the one
    that times out, and a rotation that times out may already have applied the change."""
    import asyncio
    captured = {}

    def _capture(hcl, tf_vars, tenant=None):
        captured["hcl"] = hcl
        return {"managed_system_id": "1", "managed_account_id": "2", "tf_state_json": None}

    real = ps._apply_hcl_sync
    ps._apply_hcl_sync = _capture
    try:
        asyncio.run(ps.register_managed_system(
            name="clouddb-pg", host_name="10.102.0.3", port=5432,
            functional_account_id=42, platform_id=31, workgroup_id="55",
            managed_account_name="psafe_x", method="dbgcp", dns_name=_DBGCP_DNS))
    finally:
        ps._apply_hcl_sync = real
    assert ps._line("timeout", 180) in captured["hcl"], captured["hcl"]
    assert ps._DBGCP_PLUGIN_TIMEOUT_SECONDS == 180
    # The two constants must not be confusable: one is 180 seconds, the other 30000 ms.
    assert ps._DBGCP_PLUGIN_TIMEOUT_SECONDS != ps._DBSSM_PLUGIN_TIMEOUT_MS
    # And the shared generator's parameter must not be named after either unit -- that
    # name is how one caller ends up passing the other one's number.
    import inspect
    assert "timeout_ms" not in inspect.signature(
        ps._generate_managed_system_hcl).parameters


def test_dbgcp_builder_never_emits_the_two_options_the_plugin_now_refuses():
    """The other half of the pin: the validator refuses 'verifier=on' off cloud-run and
    'iam=false' on data-api, and the emitter must not produce either. Read the source --
    cloud_database_service pulls in the app."""
    path = os.path.join(_ROOT, "web_dashboard", "services", "cloud_database_service.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "iam=false" not in src, "the data-api address has no way to authenticate"
    assert "verifier=" not in src,         "the dashboard emits no verifier= option; 'on' is cloud-run only"


def test_dbgcp_builder_chooses_iam_or_fasecret_but_never_both():
    """The other half of the pin: this module owns the grammar, cloud_database_service
    builds the string, and they used to disagree about SQL Server in both directions —
    `iam=true` was appended unconditionally though the plugin rejects it on an engine
    with no IAM database authentication, and `fasecret=` (which that engine REQUIRES)
    was emitted nowhere. Read the source rather than import it: cloud_database_service
    pulls in the app.
    """
    path = os.path.join(_ROOT, "web_dashboard", "services", "cloud_database_service.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # The one fact three decisions read (the instance flag, the functional account's
    # database user, and the address option). Its absence is what let them drift.
    assert "def _iam_db_auth(" in src
    i = src.index('addr = [channel, conn_name, control_db, "-", "-"]')
    window = src[i:i + 900]
    assert 'addr.append("iam=true")' in window, window
    assert 'addr.append(f"fasecret=' in window, window
    # An if/else, not two appends: they are alternatives, and this validator refuses
    # an address that carries both.
    assert window.index('addr.append("iam=true")') < window.index('addr.append(f"fasecret='), \
        "iam=true must be the if-branch and fasecret= the else-branch"
    assert "else:" in window[window.index('addr.append("iam=true")'):], window


def test_dbgcp_self_rotation_is_narrowed_to_the_cloud_run_channel():
    # Self-rotation needs to log in AS the managed account, which only cloud-run can do;
    # the control-plane channels authenticate as the caller and refuse it at pre-flight.
    # clouddb_ps_self_rotation is one global flag that AWS/Azure "reference" mode
    # REQUIRES, so the GCP branch honours it PER CHANNEL rather than inheriting it
    # wholesale -- otherwise turning it on for AWS silently breaks every GCP data-api
    # rotation, and dropping it unconditionally would deny SQL Server the one change
    # action that needs no privilege over the target.
    # Read the source rather than import it: cloud_database_service pulls in the app.
    path = os.path.join(_ROOT, "web_dashboard", "services", "cloud_database_service.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index('if db_method == "dbgcp" and self_rotate and (')
    window = src[i:i + 700]
    assert '!= "cloud-run"' in window, window
    assert "self_rotate = False" in window, window
    # and the register call must pass the narrowed local, not the raw config read
    j = src.index("use_own_credentials=self_rotate")
    assert j > i, "the guard must run before register_managed_system is called"


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
