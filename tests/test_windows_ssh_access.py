"""Windows servers reached over OpenSSH (a PRA Shell Jump), with RDP opt-in.

What these pin:

- ``windows_server_hook.access_modes``: SSH on and RDP off by default; the request's
  ``enable_rdp`` wins over ``windows_rdp_default``; SSH switched off forces RDP on, so a
  build is never left with no way in.
- ``plan_jumps``: a confirmed sshd gets the Shell Jump (plus RDP only when asked); a
  confirmed FAILURE swaps it for an RDP jump; an unconfirmed one gets both.
- ``parse_sshd_status`` reads the LAST sentinel, anywhere in a line (the GCP serial log
  prefixes it).
- ``wire``: the Shell Jump goes on port 22 with a PRA Vault copy only when Password Safe
  does not own the credential, under the Linux keys (``bt_shell_jump_id`` /
  ``bt_tf_state``) so every destroy path already removes it; Password Safe onboards on 22
  when SSH is the only way in; a fallback is a loud warning.
- The Shell Jump HCL: a Linux jump is byte-identical to the no-vault template, and the
  Windows one injects a ``shell_jump`` vault account whose password is never in the HCL.
- The bootstrap script never touches RDP, and each cloud delivers it.
- The two settings are declared on the setup model and bound in Settings.

Run: python tests/test_windows_ssh_access.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="winssh-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-windows-ssh-tests")

# Unguarded on purpose (tests/test_import_guard_narrowness.py).
from web_dashboard.services import (config_service, ps_vm_hook,  # noqa: E402
                                    terraform_pra_service as pra,
                                    windows_admin_secret as was,
                                    windows_server_hook as wsh)

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ── config stub ───────────────────────────────────────────────────────────────

_CFG: dict = {}


def _set_cfg(**kv):
    _CFG.clear()
    _CFG.update(kv)
    config_service.get = lambda key, default="", workgroup=None: _CFG.get(key, default)
    config_service.get_bool = lambda key, default=False: (
        str(_CFG[key]).lower() in ("1", "true", "yes") if key in _CFG else default)


def _req(enable_rdp=None):
    return types.SimpleNamespace(enable_rdp=enable_rdp)


# ── access_modes / plan_jumps ─────────────────────────────────────────────────

def test_default_is_ssh_only():
    _set_cfg()
    assert wsh.access_modes(_req()) == (True, False)


def test_rdp_default_setting_applies_when_request_is_silent():
    _set_cfg(windows_rdp_default="true")
    assert wsh.access_modes(_req()) == (True, True)


def test_request_choice_beats_the_default():
    _set_cfg(windows_rdp_default="true")
    assert wsh.access_modes(_req(False)) == (True, False)
    _set_cfg()
    assert wsh.access_modes(_req(True)) == (True, True)


def test_ssh_off_forces_rdp_on():
    _set_cfg(windows_ssh_enabled="false")
    assert wsh.access_modes(_req(False)) == (False, True)


def test_plan_jumps():
    ok, failed, unv = wsh.SSH_OK, wsh.SSH_FAILED, wsh.SSH_UNVERIFIED
    assert wsh.plan_jumps(True, False, ok) == (True, False)
    assert wsh.plan_jumps(True, True, ok) == (True, True)
    assert wsh.plan_jumps(True, False, failed) == (False, True)
    assert wsh.plan_jumps(True, False, unv) == (True, True)
    assert wsh.plan_jumps(True, False, "") == (True, True)
    assert wsh.plan_jumps(False, True, "") == (False, True)


# ── the sentinel ──────────────────────────────────────────────────────────────

def test_parse_status_reads_the_last_sentinel_anywhere_in_a_line():
    serial = ("GCEMetadataScripts: windows-startup-script-ps1: VMDASH-SSHD:FAIL no route\n"
              "other noise\n"
              "GCEMetadataScripts: windows-startup-script-ps1: VMDASH-SSHD:OK\n")
    assert wsh.parse_sshd_status(serial) == ("ok", "")
    assert wsh.parse_sshd_status("VMDASH-SSHD:FAIL sshd did not start") == (
        "failed", "sshd did not start")
    assert wsh.parse_sshd_status("nothing yet") == ("", "")
    assert wsh.parse_sshd_status("") == ("", "")


def test_poll_times_out_as_unverified():
    async def read():
        return "still booting"
    status, detail = asyncio.run(wsh._poll_status(read, timeout_s=0, interval_s=0))
    assert status == wsh.SSH_UNVERIFIED and "no OpenSSH bootstrap result" in detail


def test_bootstrap_script_never_touches_rdp_and_reports_both_ways():
    ps = wsh.WINDOWS_SSH_BOOTSTRAP_PS1
    for banned in ("fDenyTSConnections", "Remote Desktop", "TermService"):
        assert banned not in ps, banned
    assert "VMDASH-SSHD:OK" in ps and "VMDASH-SSHD:FAIL" in ps
    assert "OpenSSH.Server" in ps and "-LocalPort 22" in ps
    assert wsh.SSHD_STATUS_FILE in ps and wsh.SSHD_STATUS_FILE in wsh.SSHD_STATUS_READ_PS1
    # The registry key is created only when absent: New-Item -Force on an existing key
    # would wipe its values.
    assert "Test-Path 'HKLM:\\SOFTWARE\\OpenSSH'" in ps
    assert "__SENTINEL__" not in ps and "__STATUS_FILE__" not in ps


def test_delivery_shapes():
    ud = wsh.ssh_bootstrap_user_data()
    assert ud.startswith("<powershell>\n") and ud.rstrip().endswith("</powershell>")
    md = wsh.ssh_bootstrap_metadata()
    # Not the specialize key: the on-prem AD join owns that one.
    assert list(md) == ["windows-startup-script-ps1"]
    from web_dashboard.services import domain_join_service
    assert not set(md) & set(domain_join_service.agent_join_metadata())


# ── wire ──────────────────────────────────────────────────────────────────────

def _patch(calls, *, ps_result=None, vault_error=""):
    async def register_windows(db, job_id, vm_name, hostname, *, result, tag, username,
                               password, port=3389):
        calls.append(("ps", port))
        result.update(ps_result or {})

    async def provision_jump(**kw):
        calls.append(("shell", kw))
        out = {"shell_jump_id": "41", "tf_state_json": "{}",
               "jump_group_name": kw["jump_group_name"]}
        if kw.get("vault_account_name"):
            out["vault_account_id"] = "9"
            if vault_error:
                out["vault_error"] = vault_error
        return out

    async def provision_rdp_jump(**kw):
        calls.append(("rdp", kw))
        return {"rdp_jump_id": "77", "tf_state_json": "{}",
                "jump_group_name": kw["jump_group_name"]}

    ps_vm_hook.register_windows = register_windows
    pra.provision_jump = provision_jump
    pra.provision_rdp_jump = provision_rdp_jump
    was.delete = lambda b, r: calls.append(("delete", b, r)) or ""
    import web_dashboard.services.job_service as js
    js.update_progress = lambda *a, **k: None


def _wire(result, **kw):
    args = dict(vm_name="web01", hostname="10.0.0.4", username="Administrator",
                password="P@ss", result=result, tag="AWS", register_in_passwordsafe=False,
                pra_enabled=True, jump_group="jg", jumpoint_name="jp",
                ssh=True, rdp=False, ssh_status=wsh.SSH_OK)
    args.update(kw)
    asyncio.run(wsh.wire(None, "job1", **args))


def _kinds(calls):
    return [c[0] for c in calls]


def test_ssh_only_gets_a_vaulted_shell_jump_on_22_and_no_rdp():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    _wire(result)
    assert _kinds(calls) == ["shell"]
    kw = calls[0][1]
    assert kw["port"] == 22 and kw["tag"] == "AWS"
    assert kw["admin_password"] == "P@ss"
    assert kw["vault_account_name"] == "web01-ssh-admin"
    assert kw["vault_username"] == "Administrator"     # never `.\` -- that is NLA's
    # The Linux keys, so every cloud's destroy path removes it unchanged.
    assert result["bt_shell_jump_id"] == "41" and result["bt_tf_state"] == "{}"
    assert result["bt_ssh_vault_account_id"] == "9"
    assert result["windows_access"] == {"ssh": True, "rdp": False}
    assert result["windows_ssh_status"] == "ok"
    assert "windows_ssh_error" not in result and "bt_rdp_jump_id" not in result


def test_rdp_opt_in_gets_both():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    _wire(result, rdp=True)
    assert _kinds(calls) == ["shell", "rdp"]
    assert calls[1][1]["vault_account_name"] == "web01-admin"
    assert result["bt_shell_jump_id"] == "41" and result["bt_rdp_jump_id"] == "77"
    assert "bt_rdp_fallback" not in result


def test_failed_sshd_falls_back_to_rdp_loudly():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    _wire(result, ssh_status=wsh.SSH_FAILED, ssh_detail="no route to Windows Update")
    assert _kinds(calls) == ["rdp"]
    assert result["bt_rdp_fallback"] is True
    assert "no route to Windows Update" in result["windows_ssh_error"]
    assert "instead" in result["windows_ssh_error"]
    assert "bt_shell_jump_id" not in result


def test_unverified_sshd_gets_both_and_says_so():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    _wire(result, ssh_status=wsh.SSH_UNVERIFIED, ssh_detail="SSM never came online")
    assert _kinds(calls) == ["shell", "rdp"]
    assert result["bt_rdp_fallback"] is True
    assert "as well" in result["windows_ssh_error"]


def test_password_safe_custody_means_no_vault_copy_and_port_22():
    _set_cfg(passwordsafe_registration_enabled="1")
    calls = []
    _patch(calls, ps_result={"ps_managed_account_id": 5, "ps_initial_password_seeded": True})
    result = {"admin_password_backend": "aws_sm", "admin_password_ref": "x"}
    _wire(result, register_in_passwordsafe=True)
    assert calls[0] == ("ps", 22)
    shell = [c for c in calls if c[0] == "shell"][0][1]
    assert shell["admin_password"] == "" and shell["vault_account_name"] == ""
    assert result["admin_password_custody"] == "passwordsafe_managed"


def test_password_safe_port_is_3389_when_there_is_an_rdp_jump():
    _set_cfg(passwordsafe_registration_enabled="1")
    calls = []
    _patch(calls)
    _wire({}, register_in_passwordsafe=True, rdp=True)
    assert calls[0] == ("ps", 3389)
    calls.clear()
    _wire({}, register_in_passwordsafe=True, ssh_status=wsh.SSH_FAILED)
    assert calls[0] == ("ps", 3389)


def test_vault_failure_keeps_the_jump_and_is_reported():
    _set_cfg()
    calls = []
    _patch(calls, vault_error="403 vault")
    result = {}
    _wire(result)
    assert result["bt_shell_jump_id"] == "41"
    assert result["bt_ssh_vault_error"] == "403 vault"


def test_missing_jump_group_is_reported_for_each_jump_asked_for():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    _wire(result, jump_group="", rdp=True)
    assert not [c for c in calls if c[0] in ("shell", "rdp")]
    assert "Jump Group" in result["bt_error"] and "Jump Group" in result["bt_rdp_error"]


def test_defaults_are_the_old_rdp_only_build():
    _set_cfg()
    calls = []
    _patch(calls)
    result = {}
    asyncio.run(wsh.wire(None, "j", vm_name="w", hostname="h", username="u", password="p",
                         result=result, tag="Azure", register_in_passwordsafe=False,
                         pra_enabled=True, jump_group="jg", jumpoint_name="jp"))
    assert _kinds(calls) == ["rdp"]
    assert "windows_ssh_status" not in result and "bt_rdp_fallback" not in result


# ── the Shell Jump HCL ────────────────────────────────────────────────────────

_LINUX_HCL_TAIL = '''output "shell_jump_id" {
  value = sra_shell_jump.vm1.id
}
'''


def test_linux_shell_jump_hcl_has_no_vault():
    hcl = pra._generate_hcl("vm1", "10.0.0.6", "JG", "JP", 22, "AWS")
    assert hcl.endswith(_LINUX_HCL_TAIL)
    assert "sra_vault" not in hcl and "ssh_password" not in hcl
    assert 'variable "bt_client_secret" { sensitive = true }\n\nprovider "sra"' in hcl


def test_windows_shell_jump_hcl_injects_a_shell_jump_vault_account():
    hcl = pra._generate_hcl("web01", "10.0.0.4", "JG", "JP", 22, "AWS",
                            vault_account_name="web01-ssh-admin",
                            vault_username="Administrator", vault_account_group_id=12)
    assert 'variable "ssh_password"     { sensitive = true }' in hcl
    assert 'resource "sra_vault_username_password_account" "ssh_admin"' in hcl
    assert "password    = var.ssh_password" in hcl
    assert "P@ss" not in hcl
    assert 'username    = "Administrator"' in hcl
    assert "id   = tonumber(sra_shell_jump.web01.id)" in hcl
    assert 'type = "shell_jump"' in hcl
    assert "account_group_id = 12" in hcl
    assert 'name = ["web01"]' in hcl
    assert "(Windows server)" in hcl


def test_rdp_hcl_still_uses_remote_rdp_association():
    hcl = pra._generate_rdp_hcl("w1", "10.0.0.5", "JG", "JP", "azureuser", "Azure",
                                vault_account_name="w1-admin")
    assert 'type = "remote_rdp"' in hcl and "tonumber(sra_remote_rdp.w1.id)" in hcl
    assert 'username    = ".\\\\azureuser"' in hcl
    assert "(VDI desktop)" in hcl


def test_provision_sync_scrubs_state_and_retries_jump_only():
    """A failed vault apply is retried without it; the stashed state is scrubbed."""
    applies = []

    class _P:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, "boom" if rc else ""

    def run_tf(args, work_dir, timeout=120, extra_env=None, tenant=None):
        from pathlib import Path
        if args[0] == "apply":
            applies.append(dict(extra_env or {}))
            main = Path(work_dir, "main.tf").read_text()
            if "ssh_admin" in main:
                return _P(1)
            Path(work_dir, "terraform.tfstate").write_text(
                '{"resources": [{"instances": [{"attributes": {"password": "P@ss"}}]}]}')
            return _P(0)
        if args[0] == "output":
            return _P(0, '{"shell_jump_id": {"value": 41}}')
        return _P(0)

    saved = pra._run_tf
    pra._run_tf = run_tf
    try:
        out = pra._provision_sync("web01", "10.0.0.4", "JG", "JP", 22, "AWS",
                                  admin_password="P@ss", vault_account_name="web01-ssh-admin",
                                  vault_username="Administrator")
    finally:
        pra._run_tf = saved
    assert applies[0].get("TF_VAR_ssh_password") == "P@ss"
    assert "TF_VAR_ssh_password" not in applies[1]
    assert out["shell_jump_id"] == "41"
    assert out["vault_error"] == "boom"
    assert "P@ss" not in (out["tf_state_json"] or "")


# ── call sites, settings, templates ───────────────────────────────────────────

def test_each_cloud_passes_its_verdict_to_wire():
    for name, probe in (("aws_vm_service.py", "confirm_ssh_aws"),
                        ("azure_vm_service.py", "run_ssh_bootstrap_azure"),
                        ("gcp_vm_service.py", "confirm_ssh_gcp")):
        src = _read("web_dashboard", "services", name)
        assert probe in src, name
        assert "access_modes(" in src, name
        assert "ssh=win_ssh, rdp=win_rdp," in src, name
        assert "ssh_status=win_ssh_status, ssh_detail=win_ssh_detail," in src, name
    assert "user_data=win_user_data" in _read("web_dashboard", "services", "aws_vm_service.py")
    assert "**ssh_md" in _read("web_dashboard", "services", "gcp_vm_service.py")


def test_settings_are_declared_and_bound():
    from web_dashboard.config import Settings
    for key, default in (("windows_ssh_enabled", True), ("windows_rdp_default", False)):
        assert Settings.model_fields[key].default is default, key
        assert f"    {key}: bool = {default}" in _read("web_dashboard", "api", "setup.py"), key
        assert f'x-model="panelCfg.{key}"' in _read("web_dashboard", "templates",
                                                    "settings.html"), key


def test_every_deploy_form_offers_rdp_and_sends_it():
    for page, count in (("aws", 2), ("gcp", 2), ("azure", 2)):
        src = _read("web_dashboard", "templates", page, "index.html")
        assert src.count(".enable_rdp\" class=\"rounded") == count, page
        assert "windows_rdp_default" in src and "windows_ssh_enabled" in src, page
        assert "enable_rdp:" in src, page
    main = _read("web_dashboard", "main.py")
    assert main.count("**_windows_access_ctx()") == 3


def test_request_models_carry_enable_rdp():
    from web_dashboard.models import aws, azure, gcp
    for model in (aws.DeployRequest, aws.BulkDeployRequest,
                  azure.AzureDeployRequest, azure.AzureBulkDeployRequest,
                  gcp.GCPDeployRequest, gcp.GCPBulkDeployRequest):
        assert model.model_fields["enable_rdp"].default is None, model.__name__


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok   {_name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
