"""Windows server builds: where the administrator password lives, and what happens to it.

What these pin:

- ``windows_admin_secret.resolve_backend`` NEVER answers ``"database"`` — not by default,
  not from the global ``secrets_backend``, not from an explicit override. Before this, a
  Windows VM's administrator password went to the global backend, whose default is the
  dashboard's own ``app_config`` table.
- The order: an explicit override, then Password Safe (Secrets Safe), then an external
  global backend, then the cloud's own vault, then a refusal.
- ``windows_server_hook.wire``: once Password Safe holds a working credential the
  build-time copy is deleted and custody flips to ``passwordsafe_managed``; when
  onboarding fails the copy stays (break-glass). The RDP jump gets a PRA Vault copy only
  when Password Safe does NOT own the credential.
- ``windows_server_hook.teardown`` removes the RDP jump and the stored password.
- The admin-password endpoint needs ``azure:write`` and answers 409 for a VM whose
  account Password Safe manages.

Run: python tests/test_windows_server_builds.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="winsrv-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-windows-server-tests")

# Unguarded on purpose: CI installs the full requirements, and a broken first-party
# import must fail this file rather than skip it (tests/test_import_guard_narrowness.py).
from web_dashboard.services import (config_service, ps_vm_hook, secrets_backend_service,  # noqa: E402
                                    terraform_pra_service, windows_admin_secret as was,
                                    windows_server_hook as wsh)


# ── config stub ───────────────────────────────────────────────────────────────

_CFG: dict = {}
_orig_get, _orig_get_bool = config_service.get, config_service.get_bool


def _set_cfg(**kv):
    _CFG.clear()
    _CFG.update(kv)
    config_service.get = lambda key, default="", workgroup=None: _CFG.get(key, default)
    config_service.get_bool = lambda key, default=False: (
        str(_CFG[key]).lower() in ("1", "true", "yes") if key in _CFG else default)


_BT = dict(pscli_api_url="https://ps.example", pscli_client_id="id",
           pscli_client_secret="s", secrets_bt_owner="2")


# ── resolve_backend ───────────────────────────────────────────────────────────

def test_default_install_refuses_rather_than_using_the_database():
    _set_cfg(secrets_backend="database")
    try:
        was.resolve_backend("gcp")
    except was.WindowsSecretError as e:
        assert "never kept in the dashboard database" in str(e)
    else:
        raise AssertionError("resolved a backend with nothing external configured")


def test_explicit_database_override_is_refused():
    _set_cfg(windows_admin_secret_backend="database", **_BT)
    try:
        was.resolve_backend("azure")
    except was.WindowsSecretError as e:
        assert "database" in str(e)
    else:
        raise AssertionError("explicit 'database' was accepted")


def test_password_safe_wins_over_the_global_backend():
    _set_cfg(secrets_backend="azure_kv", secrets_azure_kv_url="https://kv", **_BT)
    assert was.resolve_backend("azure") == "bt_secrets_safe"


def test_password_safe_needs_a_numeric_owner():
    _set_cfg(**{**_BT, "secrets_bt_owner": "admin"}, secrets_azure_kv_url="https://kv")
    assert was.resolve_backend("azure") == "azure_kv"


def test_external_global_backend_is_used():
    _set_cfg(secrets_backend="gcp_sm", gcp_project="p1")
    assert was.resolve_backend("azure") == "gcp_sm"


def test_global_gcp_sm_without_a_project_is_not_usable():
    _set_cfg(secrets_backend="gcp_sm", secrets_azure_kv_url="https://kv")
    assert was.resolve_backend("azure") == "azure_kv"


def test_gcp_falls_back_to_secret_manager():
    _set_cfg(secrets_backend="database", secrets_gcp_project="p1")
    assert was.resolve_backend("gcp") == "gcp_sm"


def test_directory_prefix_keys_apart_from_windows():
    assert was.secret_key("corp.example.com", "ab12", was.DIRECTORY_PREFIX) == \
        "ad-admin-corp.example.com-ab12"
    assert was.secret_key("web01", "ab12") == "windows-admin-web01-ab12"


def test_cloud_native_fallback_per_cloud():
    _set_cfg(secrets_backend="database", secrets_azure_kv_url="https://kv",
             secrets_aws_region="us-east-1")
    assert was.resolve_backend("azure") == "azure_kv"
    assert was.resolve_backend("aws") == "aws_sm"


def test_explicit_override_wins():
    _set_cfg(windows_admin_secret_backend="aws_sm", **_BT)
    assert was.resolve_backend("azure") == "aws_sm"


def test_resolve_never_returns_database_across_combinations():
    for gb in ("", "database", "azure_kv", "aws_sm", "bt_secrets_safe"):
        for kv in ("", "https://kv"):
            for bt in (True, False):
                _set_cfg(secrets_backend=gb, secrets_azure_kv_url=kv, **(_BT if bt else {}))
                for cloud in ("azure", "aws", "gcp"):
                    try:
                        assert was.resolve_backend(cloud) != "database"
                    except was.WindowsSecretError:
                        pass


def test_store_writes_through_the_resolved_backend():
    _set_cfg(**_BT)
    calls = []
    orig = secrets_backend_service.write_sync
    secrets_backend_service.write_sync = lambda b, k, v: calls.append((b, k, v)) or f"Dashboard/{k}"
    try:
        backend, ref = was.store("azure", "web01", "abcd1234", "P@ss")
    finally:
        secrets_backend_service.write_sync = orig
    assert backend == "bt_secrets_safe"
    assert ref == "Dashboard/windows-admin-web01-abcd1234"
    assert calls == [("bt_secrets_safe", "windows-admin-web01-abcd1234", "P@ss")]


# ── windows_server_hook.wire / teardown ──────────────────────────────────────

class _Rec:
    def __init__(self):
        self.calls = []


def _patch_wire(rec, *, ps_result=None, delete_err=""):
    async def register_windows(db, job_id, vm_name, hostname, *, result, tag, username, password,
                               port=3389):
        rec.calls.append(("register_windows", username, password))
        result.update(ps_result or {})

    async def provision_rdp_jump(**kw):
        rec.calls.append(("rdp", kw))
        return {"rdp_jump_id": "77", "tf_state_json": "{}", "jump_group_name": kw["jump_group_name"]}

    def delete(backend, ref):
        rec.calls.append(("delete", backend, ref))
        return delete_err

    ps_vm_hook.register_windows = register_windows
    terraform_pra_service.provision_rdp_jump = provision_rdp_jump
    was.delete = delete
    import web_dashboard.services.job_service as js
    js.update_progress = lambda *a, **k: None


def _wire(result, **kw):
    args = dict(vm_name="web01", hostname="10.0.0.4", username="azureuser", password="P@ss",
                result=result, tag="Azure", register_in_passwordsafe=True, pra_enabled=True,
                jump_group="jg", jumpoint_name="jp")
    args.update(kw)
    asyncio.run(wsh.wire(None, "job1", **args))


def _base_result():
    return {"admin_password_backend": "bt_secrets_safe", "admin_password_ref": "Dashboard/x"}


def test_password_safe_takes_custody_and_build_copy_is_retired():
    _set_cfg(passwordsafe_registration_enabled="1")
    rec = _Rec()
    _patch_wire(rec, ps_result={"ps_managed_account_id": 5, "ps_initial_password_seeded": True,
                                "ps_change_password_triggered": True})
    result = _base_result()
    _wire(result)
    assert result["admin_password_custody"] == "passwordsafe_managed"
    assert result["admin_password_retired"] is True
    assert "admin_password_ref" not in result
    assert ("delete", "bt_secrets_safe", "Dashboard/x") in rec.calls
    rdp = [c for c in rec.calls if c[0] == "rdp"][0][1]
    # PRA injects through its Password Safe integration — no stale Vault copy.
    assert rdp["admin_password"] == "" and rdp["vault_account_name"] == ""


def test_failed_onboarding_keeps_the_break_glass_copy():
    _set_cfg(passwordsafe_registration_enabled="1")
    rec = _Rec()
    _patch_wire(rec, ps_result={"ps_error": "no functional account"})
    result = _base_result()
    _wire(result)
    assert result["admin_password_custody"] == "secret_manager"
    assert result["admin_password_ref"] == "Dashboard/x"
    assert not [c for c in rec.calls if c[0] == "delete"]
    rdp = [c for c in rec.calls if c[0] == "rdp"][0][1]
    assert rdp["admin_password"] == "P@ss" and rdp["vault_account_name"] == "web01-admin"


def test_undeletable_copy_is_reported_and_ref_kept():
    _set_cfg(passwordsafe_registration_enabled="1")
    rec = _Rec()
    _patch_wire(rec, ps_result={"ps_managed_account_id": 5, "ps_initial_password_seeded": True},
                delete_err="403")
    result = _base_result()
    _wire(result)
    assert result["admin_password_custody"] == "passwordsafe_managed"
    assert "403" in result["windows_secret_cleanup_error"]
    assert result["admin_password_ref"] == "Dashboard/x"


def test_no_password_safe_opt_in_means_no_onboarding():
    _set_cfg(passwordsafe_registration_enabled="1")
    rec = _Rec()
    _patch_wire(rec)
    result = _base_result()
    _wire(result, register_in_passwordsafe=False)
    assert not [c for c in rec.calls if c[0] == "register_windows"]
    assert result["bt_rdp_jump_id"] == "77"


def test_pra_off_means_no_jump():
    _set_cfg()
    rec = _Rec()
    _patch_wire(rec)
    result = _base_result()
    _wire(result, register_in_passwordsafe=False, pra_enabled=False)
    assert not [c for c in rec.calls if c[0] == "rdp"]


def test_teardown_removes_jump_and_secret():
    calls = []

    async def remove_rdp_jump(state, tenant=None):
        calls.append(("remove_rdp", state))

    terraform_pra_service.remove_rdp_jump = remove_rdp_jump
    was.delete = lambda b, r: calls.append(("delete", b, r)) or ""
    result = {}
    asyncio.run(wsh.teardown({"bt_rdp_tf_state": "S", "bt_rdp_jump_id": "77",
                              "admin_password_backend": "azure_kv",
                              "admin_password_ref": "windows-admin-x"}, result))
    assert calls == [("remove_rdp", "S"), ("delete", "azure_kv", "windows-admin-x")]
    assert result["bt_rdp_jump_removed"] == "77"
    assert result["admin_password_deleted"] == "windows-admin-x"


def test_holds_credential_rules():
    f = ps_vm_hook.password_safe_holds_credential
    assert not f({})
    assert not f({"ps_managed_account_id": 1})
    assert f({"ps_managed_account_id": 1, "ps_initial_password_seeded": True})
    assert f({"ps_managed_account_id": 1, "ps_change_password_triggered": True})
    assert not f({"ps_managed_account_id": 1, "ps_initial_password_seeded": True, "ps_error": "x"})


def _run_register_windows(platform_name):
    import importlib
    from web_dashboard.services import ps_api_service, ps_resource_service, job_service
    hook = importlib.reload(ps_vm_hook)   # undo the stub _patch_wire installed
    captured = {}

    async def get_fa(name):
        captured["fa"] = name
        return {"id": 9, "platform_id": 1, "platform_name": platform_name}

    async def get_wg(name):
        return "wg"

    async def register_managed_system(**kw):
        captured["reg"] = kw
        return {"managed_system_id": 3, "managed_account_id": 4, "tf_state_json": "{}",
                "initial_password_seeded": True}

    async def change(aid):
        captured["change"] = aid

    ps_api_service.get_functional_account = get_fa
    ps_api_service.get_workgroup_id = get_wg
    ps_api_service.change_managed_account_password = change
    ps_resource_service.register_managed_system = register_managed_system
    job_service.update_progress = lambda *a, **k: None
    result = {}
    asyncio.run(hook.register_windows(None, "j", "web01", "10.0.0.4", result=result,
                                      tag="Azure", username="azureuser", password="P@ss"))
    return result, captured


def test_register_windows_seeds_and_rotates():
    _set_cfg(passwordsafe_vm_functional_account_windows="win-fa")
    result, cap = _run_register_windows("Windows")
    assert cap["fa"] == "win-fa"
    assert cap["reg"]["method"] == "password"
    assert cap["reg"]["initial_password"] == "P@ss"
    assert cap["reg"]["managed_account_name"] == "azureuser"
    assert cap["change"] == 4
    assert result["ps_managed_account_id"] == 4 and result["ps_change_password_triggered"]
    assert "ps_error" not in result


def test_register_windows_refuses_a_linux_platform():
    _set_cfg(passwordsafe_vm_functional_account_windows="win-fa")
    result, cap = _run_register_windows("Azure VM SSH Rotation")
    assert "not a Windows platform" in result["ps_error"]
    assert "reg" not in cap


def test_register_windows_needs_its_own_functional_account():
    # The Linux key must not be borrowed: its platform is an SSH-rotation plugin.
    _set_cfg(passwordsafe_vm_functional_account="linux-fa")
    result, cap = _run_register_windows("Windows")
    assert "passwordsafe_vm_functional_account_windows" in result["ps_error"]


# ── Entra ID join (Azure) ─────────────────────────────────────────────────────

def test_extension_payload_serializes_to_the_arm_shape():
    from web_dashboard.services import azure_service

    class _Poller:
        def done(self):
            return True

        def result(self):
            return type("R", (), {"provisioning_state": "Succeeded"})()

    sent = {}

    class _Ext:
        def begin_create_or_update(self, rg, vm, name, ext):
            body = ext.as_dict() if hasattr(ext, "as_dict") else ext.serialize()
            sent.update(rg=rg, vm=vm, name=name, body=body)
            return _Poller()

    orig = azure_service._get_compute
    azure_service._get_compute = lambda cred, sub: type("C", (), {"virtual_machine_extensions": _Ext()})()
    try:
        out = azure_service._enable_entra_login_sync(None, "sub", "rg1", "web01", "eastus", True)
    finally:
        azure_service._get_compute = orig
    props = sent["body"]["properties"]
    assert sent["name"] == "AADLoginForWindows"
    assert props["publisher"] == "Microsoft.Azure.ActiveDirectory"
    assert props["type"] == "AADLoginForWindows"
    assert props["settings"] == {"mdmId": azure_service.INTUNE_MDM_ID}
    assert out["provisioning_state"] == "Succeeded"


def test_entra_defaults_come_from_config():
    class R:
        entra_join = None
        entra_intune_enroll = None
    _set_cfg()
    assert wsh.entra_requested(R()) == (False, False)
    _set_cfg(azure_windows_entra_join="1", azure_windows_entra_intune_enroll="1")
    assert wsh.entra_requested(R()) == (True, True)
    r = R()
    r.entra_join = False
    assert wsh.entra_requested(r) == (False, False)   # the request wins


def _patch_azure_entra(fail_role=None):
    from web_dashboard.services import azure_service
    import web_dashboard.services.job_service as js
    js.update_progress = lambda *a, **k: None
    calls = []

    async def enable(rg, vm, loc, intune=False):
        calls.append(("enable", vm, intune))
        return {"extension": "AADLoginForWindows"}

    async def ensure(*, scope, role, principal_id, principal_type):
        calls.append(("assign", role, principal_id, principal_type))
        if fail_role and role == fail_role:
            raise azure_service.AzureError("role assignment failed (403): denied")
        return {"name": f"n-{principal_id}", "created": True}

    async def delete(scope, name):
        calls.append(("delete", name))

    azure_service.enable_entra_login = enable
    azure_service.ensure_role_assignment = ensure
    azure_service.delete_role_assignment = delete
    return calls, azure_service


def test_entra_join_assigns_login_roles_to_groups():
    _set_cfg(azure_entra_vm_admin_group_ids="g-admin", azure_entra_vm_user_group_ids="g1, g2")
    calls, az = _patch_azure_entra()
    result = {}
    asyncio.run(wsh.entra_join_azure(None, "j", rg="rg", vm_name="web01", location="eastus",
                                     vm_id="/sub/vm", intune=False, result=result))
    assigns = [c for c in calls if c[0] == "assign"]
    assert ("assign", az.ENTRA_VM_LOGIN_ROLES["admin"], "g-admin", "Group") in assigns
    assert len(assigns) == 3
    assert len(result["entra_role_assignments"]) == 3
    assert "entra_role_errors" not in result


def test_entra_role_403_is_a_warning_with_a_remedy():
    _set_cfg(azure_entra_vm_admin_group_ids="g-admin")
    _calls, az = _patch_azure_entra(fail_role="1c0163c0-47e6-4577-8991-ea5c82e286e4")
    result = {}
    asyncio.run(wsh.entra_join_azure(None, "j", rg="rg", vm_name="web01", location="eastus",
                                     vm_id="/sub/vm", intune=False, result=result))
    assert "roleAssignments/write" in result["entra_role_errors"][0]
    assert result["entra_join"]   # the join itself stands


def test_teardown_entra_only_removes_what_it_created():
    calls, _az = _patch_azure_entra()
    meta = {"entra_role_assignments": [
        {"scope": "/s", "name": "mine", "created": True},
        {"scope": "/s", "name": "theirs", "created": False}]}
    asyncio.run(wsh.teardown_entra(meta, {}))
    assert calls == [("delete", "mine")]


# ── AWS: Administrator password through a one-time key pair ──────────────────

def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return key, pem


def _ec2_password_blob(key, password):
    """What GetPasswordData returns: PKCS#1 v1.5, base64."""
    import base64
    from cryptography.hazmat.primitives.asymmetric import padding
    return base64.b64encode(key.public_key().encrypt(password.encode(), padding.PKCS1v15())).decode()


def test_decrypt_matches_ec2s_encryption():
    from web_dashboard.services import aws_service
    key, pem = _rsa_pair()
    blob = _ec2_password_blob(key, "Zq9!kL2#vbn")
    # EC2 returns the blob with line breaks in it.
    wrapped = "\n".join(blob[i:i + 64] for i in range(0, len(blob), 64))
    assert aws_service.decrypt_windows_password(wrapped, pem) == "Zq9!kL2#vbn"


def test_password_poll_waits_for_ec2_then_decrypts():
    from web_dashboard.services import aws_service
    key, pem = _rsa_pair()
    answers = ["", "", _ec2_password_blob(key, "S3cret!pw")]
    waits = []
    orig = aws_service._get_password_data_sync
    aws_service._get_password_data_sync = lambda region, iid: answers.pop(0)
    try:
        pw = asyncio.run(aws_service.get_windows_password(
            "us-east-1", "i-1", pem, timeout_s=60, interval_s=0, on_wait=waits.append))
    finally:
        aws_service._get_password_data_sync = orig
    assert pw == "S3cret!pw"
    assert len(waits) == 2


def test_password_poll_gives_up_with_a_reason():
    from web_dashboard.services import aws_service
    _key, pem = _rsa_pair()
    orig = aws_service._get_password_data_sync
    aws_service._get_password_data_sync = lambda region, iid: ""
    try:
        asyncio.run(aws_service.get_windows_password("us-east-1", "i-1", pem,
                                                     timeout_s=0, interval_s=0))
    except aws_service.AWSError as e:
        assert "EC2Launch" in str(e)
    else:
        raise AssertionError("never timed out")
    finally:
        aws_service._get_password_data_sync = orig


def test_launch_attaches_the_key_pair_only_when_given():
    from web_dashboard.services import aws_service
    sent = []

    class _EC2:
        def run_instances(self, **kw):
            sent.append(kw)
            return {"Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"}}]}

    saved = (aws_service._get_ec2, aws_service._root_bdm_sync)
    aws_service._get_ec2 = lambda region: _EC2()
    aws_service._root_bdm_sync = lambda region, ami: []
    try:
        aws_service._launch_instance_sync("us-east-1", "ami-1", "w", "t3.medium", "",
                                          "subnet-1", ["sg-1"], "", "windows", "", "",
                                          "vmdash-win-abcd1234")
        aws_service._launch_instance_sync("us-east-1", "ami-1", "l", "t3.medium", "",
                                          "subnet-1", ["sg-1"], "", "ubuntu", "", "")
    finally:
        aws_service._get_ec2, aws_service._root_bdm_sync = saved
    assert sent[0]["KeyName"] == "vmdash-win-abcd1234"
    assert "UserData" not in sent[0]       # no password or key material in UserData
    assert "KeyName" not in sent[1]


def test_failed_launch_drops_the_key_pair():
    from web_dashboard.services import aws_service, aws_vm_service
    deleted = []

    async def delete_key_pair(region, name):
        deleted.append(name)

    orig = aws_service.delete_key_pair
    aws_service.delete_key_pair = delete_key_pair
    try:
        result = {}
        asyncio.run(aws_vm_service._drop_windows_key_pair("us-east-1", "vmdash-win-x", result))
        asyncio.run(aws_vm_service._drop_windows_key_pair("us-east-1", "", {}))
    finally:
        aws_service.delete_key_pair = orig
    assert deleted == ["vmdash-win-x"]
    assert result["windows_key_pair_deleted"] is True


def test_aws_endpoint_requires_write_and_refuses_ps_managed():
    import inspect
    from web_dashboard.api import aws as api_aws
    src = inspect.getsource(api_aws.get_instance_admin_password)
    assert 'require_permission("aws", "write")' in src
    assert "passwordsafe_managed" in src and "409" in src


# ── the admin-password endpoint ───────────────────────────────────────────────

def test_endpoint_requires_write_and_refuses_ps_managed():
    import inspect
    from web_dashboard.api import azure as api_azure
    src = inspect.getsource(api_azure.get_vm_admin_password)
    assert 'require_permission("azure", "write")' in src
    assert "passwordsafe_managed" in src and "409" in src


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
