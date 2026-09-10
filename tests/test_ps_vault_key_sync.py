"""Behavioral tests for the cloud-VM → PRA Vault Private Key sync (ps_vm_hook).

A VM onboarded with one of the cloud-native SSH-key plugins (AWS Systems Manager,
Azure/GCP VM SSH Rotation) has a managed account whose stored credential IS the private
key Password Safe mints and rotates. This feature mirrors that key into PRA so a rep can
check it out in /login and PRA can inject it into the VM's Shell Jump. Three artifacts,
in this order:

    PRA Vault SSH account → Password Safe mirror on the "PRA Vault Private Key"
    plugin → SyncedAccounts LINK → one Change so PRA holds a real key now

What has sharp edges, and is therefore what these tests pin:

- **Direction.** ``POST ManagedAccounts/{parent}/SyncedAccounts/{sub}`` takes two plain
  account ids, so a swapped pair links happily and then syncs backwards — pushing the
  Vault account's throwaway key onto the VM's real managed account.
- **The functional account's platform.** ``"pra vault"`` is a substring of ``"PRA Vault
  Username Password"``, so a fixed-token guard would wave through the OT / cloud-database
  functional account and register a mirror on a plugin that writes a password field and
  never a key — success, syncing nothing.
- **The mirror's own managed system.** The provider attaches an account to its system BY
  NAME, so every VM passing the appliance URL as ``host_name`` would pile its account
  onto whichever "PRA Vault" system was created first.
- **The converge flag.** It must not be a cloud's change-on-register posture: AWS defaults
  that OFF, which would leave PRA holding the throwaway key.
- **The teardown order.** Unlink, then off-board the mirror, then destroy the Vault
  account — all before the parent account the link hangs off is off-boarded.
- **The seeded key never reaching the jobs table.** ``jobs.extra_data`` is served by the
  jobs API and the MCP ``get_job`` tool.

Every collaborator is a stub injected into sys.modules (ps_vm_hook's imports are
function-level, so they resolve at call time), so this runs with no app, DB, Password Safe
or PRA. Runs under pytest or standalone:  python tests/test_ps_vault_key_sync.py
"""
import asyncio
import ast
import importlib.util
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = types.SimpleNamespace()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

# The real terraform_pra_service, captured BEFORE the stubs below replace it in
# sys.modules — the HCL tests want the real generator.
from web_dashboard.services import terraform_pra_service as pra  # noqa: E402

_HOOK_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ps_vm_hook.py")
_HOOK_SRC = open(_HOOK_PATH, encoding="utf-8").read()


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.ps_vm_hook", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


# ── 1. the PRA Vault SSH account HCL ─────────────────────────────────────────

def test_vault_ssh_hcl_takes_the_key_as_a_tf_var_only():
    hcl = pra._generate_vault_ssh_account_hcl("web01-adminuser", "adminuser", "Cloud VMs")
    assert 'resource "sra_vault_ssh_account" "vm_key"' in hcl
    assert 'variable "vault_private_key" { sensitive = true }' in hcl
    assert "private_key            = var.vault_private_key" in hcl
    assert '"web01-adminuser"' in hcl
    assert 'username               = "adminuser"' in hcl
    assert 'output "vault_account_id"' in hcl
    # No PEM, no BEGIN block, nothing key-shaped inline.
    assert "PRIVATE KEY" not in hcl


def test_vault_ssh_hcl_associates_via_the_jump_groups_criteria():
    hcl = pra._generate_vault_ssh_account_hcl("x", "adminuser", "Cloud VMs")
    # By NAME through a data source (the caller holds no id), into
    # criteria.shared_jump_groups — never jump_items[].type, which PRA 422s.
    assert 'data "sra_jump_group_list" "jg"' in hcl
    assert 'name = "Cloud VMs"' in hcl
    assert "shared_jump_groups = [tonumber(data.sra_jump_group_list.jg.items[0].id)]" in hcl
    for arr in ("host", "name", "tag", "comment"):
        assert re.search(rf"^\s*{arr}\s+= \[\]", hcl, re.MULTILINE), \
            f"criteria array {arr!r} missing — the API 4xxes without it"
    assert "jump_items = []" in hcl


def test_vault_ssh_hcl_account_group_optional():
    assert "account_group_id" not in pra._generate_vault_ssh_account_hcl("x", "u", "jg")
    assert "account_group_id = 9" in pra._generate_vault_ssh_account_hcl(
        "x", "u", "jg", vault_account_group_id=9)


def test_the_seeded_key_is_a_real_fresh_openssh_key():
    a, b = pra._throwaway_openssh_key(), pra._throwaway_openssh_key()
    assert a.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert a != b, "the throwaway key must be minted per account, never a constant"


def test_the_scrub_covers_the_private_key_and_its_passphrase():
    # remove_vault_account destroys from stashed state, and jobs.extra_data is served
    # by the jobs API + the MCP get_job tool. The seed is a real PEM, and Password Safe
    # replaces it with the VM's LIVE key on the first synced rotation.
    state = ('{"resources":[{"type":"sra_vault_ssh_account","instances":[{"attributes":'
             '{"private_key":"-----BEGIN OPENSSH PRIVATE KEY-----seedseed",'
             '"private_key_passphrase":"pp","name":"web01-adminuser"}}]}]}')
    scrubbed = pra._scrub_tf_state(state)
    assert "seedseed" not in scrubbed and "BEGIN OPENSSH" not in scrubbed
    assert '"pp"' not in scrubbed
    assert "web01-adminuser" in scrubbed, "the name is how an operator finds it in PRA"


# ── 2. stub harness for the wiring ───────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.events = []
        self.calls = {}

    def hit(self, _event, **kw):
        # `_event` is positional-ish on purpose: several stubs record a `name=` kwarg
        # of their own, which would collide with a parameter called `name`.
        self.events.append(_event)
        self.calls[_event] = kw

    def index(self, _event):
        return self.events.index(_event)


def _install(rec, *, cfg=None, fa_platform="PRA Vault Private Key",
             link_confirmed=True, change_raises=False, mirror_account_id=900):
    """Inject stub collaborators and return the config store."""
    store = {
        "passwordsafe_vault_sync_enabled": "1",
        "passwordsafe_vault_sync_functional_account": "pra-config-api",
        "passwordsafe_managed_account_name": "adminuser",
        "passwordsafe_workgroup": "55",
        "bt_api_host": "pra.example.com",
        "bt_jump_group_name": "Config JG",
    }
    if cfg is not None:
        store.update(cfg)
    for k in [k for k, v in store.items() if v is None]:
        del store[k]

    pkg = sys.modules["web_dashboard.services"]

    def _mod(name, **attrs):
        m = types.ModuleType(f"web_dashboard.services.{name}")
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[m.__name__] = m
        setattr(pkg, name, m)      # `from . import x` reads the package attribute
        return m

    _mod("config_service",
         get=lambda key, workgroup=None: store.get(key, ""),
         get_bool=lambda key, default=False: (
             store[key] not in ("0", "", "false") if key in store else default))
    _mod("job_service", update_progress=lambda db, jid, pct, msg: None)
    _mod("ot_service",
         resolve_jump_targets=lambda jg, jp, cloud="gcp": (
             (jg or "").strip() or store.get("bt_jump_group_name", ""), ""))

    class PSResourceError(Exception):
        pass

    async def _provision(*, name, username, jump_group_name,
                         vault_account_group_id=None, client_secret=""):
        rec.hit("vault", name=name, username=username, jump_group=jump_group_name,
                group_id=vault_account_group_id)
        return {"vault_account_id": "1001", "tf_state_json": '{"scrubbed":true}'}

    async def _remove(state):
        rec.hit("vault_removed", state=state)

    _mod("terraform_pra_service",
         provision_vault_ssh_account=_provision, remove_vault_account=_remove)

    async def _fa(name):
        return {"id": 88, "platform_id": 1008, "platform_name": fa_platform,
                "account_name": name}

    async def _pid(name):
        return 1008

    async def _wg(name):
        return "55"

    async def _link(*, parent_account_id, synced_account_id,
                    expect_subscriber_platform=""):
        rec.hit("link", parent=parent_account_id, sub=synced_account_id,
                expect=expect_subscriber_platform)
        return {"confirmed": link_confirmed}

    async def _unlink(*, parent_account_id, synced_account_id):
        rec.hit("unlink", parent=parent_account_id, sub=synced_account_id)

    async def _change(account_id):
        rec.hit("change", account_id=account_id)
        if change_raises:
            raise RuntimeError("rotation plugin said no")

    _mod("ps_api_service", get_functional_account=_fa, get_platform_id=_pid,
         get_workgroup_id=_wg, link_synced_account=_link,
         unlink_synced_account=_unlink, change_managed_account_password=_change)

    async def _register(**kw):
        rec.hit("mirror", **kw)
        return {"managed_system_id": 700, "managed_account_id": mirror_account_id,
                "tf_state_json": '{"mirror":true}'}

    async def _dereg(state):
        rec.hit("parent_deregister" if state == "PARENT" else "mirror_deregister",
                state=state)

    _mod("ps_resource_service", register_managed_system=_register, deregister=_dereg,
         PSResourceError=PSResourceError)
    return store


def _wire(rec, *, result=None, tag="aws", **kw):
    store = _install(rec, **kw)
    res = {"ps_managed_account_id": 500, "ps_registration_tf_state": "PARENT"}
    res.update(result or {})
    asyncio.run(hook.wire_pra_vault_key_sync(None, "job-1", "web01", result=res, tag=tag))
    return res, store


# ── 3. the happy path and the two orderings ──────────────────────────────────

def test_the_happy_path_creates_all_three_artifacts_in_order():
    rec = Recorder()
    res, _ = _wire(rec)
    assert rec.events == ["vault", "mirror", "link", "change"], rec.events
    assert res["ps_vault_account_id"] == "1001"
    assert res["ps_vault_account_name"] == "web01-adminuser"
    assert res["ps_vault_tf_state"] == '{"scrubbed":true}'
    assert res["ps_vault_mirror_system_id"] == "700"
    assert res["ps_vault_mirror_account_id"] == "900"
    assert res["ps_vault_synced"] is True
    assert res["ps_vault_change_triggered"] is True
    assert "ps_vault_error" not in res


def test_the_link_names_the_vm_account_as_parent_and_the_mirror_as_subscriber():
    # The defect this pins: both path segments are plain account ids, so a swapped
    # pair links happily and syncs BACKWARDS — pushing the Vault account's throwaway
    # key onto the VM's real managed account, i.e. locking the operator out of the VM.
    rec = Recorder()
    _wire(rec)
    assert rec.calls["link"]["parent"] == 500, "the VM's own account is the parent"
    assert rec.calls["link"]["sub"] == 900, "the PRA Vault mirror is the subscriber"
    assert rec.calls["link"]["expect"] == "PRA Vault Private Key", (
        "link_synced_account must be given the expected subscriber platform — it is "
        "the guard that fails closed on a mirror created on the wrong plugin")


def test_the_vault_account_username_is_the_os_login_not_the_ps_account_name():
    # The AWS Systems Manager plugin qualifies its account as `{user};{suffix}`; that
    # suffix is a Password Safe naming detail and PRA would inject it as the username.
    rec = Recorder()
    res, _ = _wire(rec, cfg={"passwordsafe_managed_account_name": "adminuser;local"})
    assert rec.calls["vault"]["username"] == "adminuser"
    assert res["ps_vault_account_name"] == "web01-adminuser"


def test_the_mirror_gets_its_own_managed_system_not_the_appliance_url_as_host():
    # The defect this pins (measured live on the k8s mirror): Password Safe names a
    # workgroup-created managed system after its HostName and the provider attaches an
    # account to its system BY NAME, so every VM passing the URL as host_name would
    # pile its account onto whichever "PRA Vault" system existed first.
    rec = Recorder()
    _wire(rec)
    m = rec.calls["mirror"]
    assert m["host_name"] == "web01-pravault-key", m["host_name"]
    assert m["dns_name"] == "https://pra.example.com"
    assert m["host_name"] != m["dns_name"]
    assert m["method"] == "pravault"
    assert m["managed_account_name"] == "web01-adminuser", (
        "the plugin resolves its PRA-side target by NAME, so the mirror account and "
        "the Vault account must be named identically")


# ── 4. the gates ─────────────────────────────────────────────────────────────

def test_it_does_nothing_at_all_unless_the_operator_opted_in():
    rec = Recorder()
    res, _ = _wire(rec, cfg={"passwordsafe_vault_sync_enabled": "0"})
    assert rec.events == []
    assert not [k for k in res if k.startswith("ps_vault")]


def test_the_traditional_ssh_method_is_not_synced():
    # There the key is one the dashboard pushed from a cloud secret store, not one
    # Password Safe minted, so mirroring it into PRA publishes an existing key.
    rec = Recorder()
    res, _ = _wire(rec, tag="oci")
    assert rec.events == []
    assert "not a cloud-native SSH-key plugin" in res["ps_vault_skipped"]


def test_an_unscrubbable_parent_state_skips_rather_than_orphaning_pra_objects():
    # Every cloud's destroy path gates the Password Safe off-boarding on
    # ps_registration_tf_state, and _scrub_state drops that state fail-closed. Wiring
    # PRA objects then would add a Vault account and a managed system that nothing
    # would ever remove -- on top of a managed system that already needs cleanup.
    rec = Recorder()
    res, _ = _wire(rec, result={"ps_registration_tf_state": None})
    assert rec.events == []
    assert "outlive the VM" in res["ps_vault_skipped"]
    assert "ps_vault_error" not in res


def test_the_three_cloud_native_methods_are_exactly_the_synced_set():
    assert hook._VAULT_SYNC_METHODS == {"ssm", "azurevm", "gcpvm"}
    assert "ssh" not in hook._VAULT_SYNC_METHODS


def test_the_deploys_own_jump_group_wins_over_the_configured_default():
    rec = Recorder()
    _wire(rec, result={"bt_jump_group_name": "Deploy JG"})
    assert rec.calls["vault"]["jump_group"] == "Deploy JG"


def test_it_falls_back_to_the_configured_jump_group():
    rec = Recorder()
    _wire(rec)
    assert rec.calls["vault"]["jump_group"] == "Config JG"


def test_no_jump_group_anywhere_skips_rather_than_creating_an_unusable_account():
    rec = Recorder()
    res, _ = _wire(rec, cfg={"bt_jump_group_name": None})
    assert rec.events == []
    assert "Jump Group" in res["ps_vault_skipped"]
    assert "ps_vault_error" not in res, "a missing Jump Group is a skip, not a failure"


def test_the_account_group_is_passed_when_numeric_and_dropped_otherwise():
    rec = Recorder()
    _wire(rec, cfg={"bt_vault_account_group_id": "7"})
    assert rec.calls["vault"]["group_id"] == 7
    rec = Recorder()
    _wire(rec, cfg={"bt_vault_account_group_id": "(numeric id)"})
    assert rec.calls["vault"]["group_id"] is None


# ── 5. failing closed ────────────────────────────────────────────────────────

def test_a_username_password_functional_account_is_refused():
    # The one mistake this path must refuse. "pra vault" is a SUBSTRING of "PRA Vault
    # Username Password", so a fixed-token guard would wave the OT / cloud-database
    # functional account through and register a mirror on a plugin that writes a
    # password field and never a key — reporting success and syncing nothing.
    rec = Recorder()
    res, _ = _wire(rec, fa_platform="PRA Vault Username Password")
    assert rec.events == ["vault"], "the mirror must not be created"
    assert "PRA Vault Private Key" in res["ps_vault_error"]
    assert res.get("ps_vault_synced") is not True


def test_a_missing_functional_account_names_the_key_to_set():
    rec = Recorder()
    res, _ = _wire(rec, cfg={"passwordsafe_vault_sync_functional_account": None})
    assert "passwordsafe_vault_sync_functional_account" in res["ps_vault_error"]
    # The Vault account is still recorded, so a repair retries only what is missing.
    assert res["ps_vault_account_id"] == "1001"


def test_an_unconfirmed_link_is_a_failure_not_a_success():
    # Password Safe accepting the POST is not the same as the subscriber appearing in
    # the parent's synced list; without it, rotations never reach PRA.
    rec = Recorder()
    res, _ = _wire(rec, link_confirmed=False)
    assert res.get("ps_vault_synced") is not True
    assert "synced list" in res["ps_vault_error"]
    assert "change" not in rec.events


def test_a_failed_converge_leaves_the_link_intact():
    # The link is what guarantees the NEXT rotation lands, so a failed convenience
    # rotation must not read as a failed sync.
    rec = Recorder()
    res, _ = _wire(rec, change_raises=True)
    assert res["ps_vault_synced"] is True
    assert "ps_vault_change_triggered" not in res
    assert "ps_vault_error" not in res


def test_a_mirror_with_no_account_id_does_not_link_anything():
    rec = Recorder()
    res, _ = _wire(rec, mirror_account_id=None)
    assert "link" not in rec.events
    assert "no managed-account id" in res["ps_vault_error"]


# ── 6. teardown ──────────────────────────────────────────────────────────────

def _dereg(rec, meta):
    _install(rec)
    result = {}
    asyncio.run(hook.deregister(meta, result))
    return result


def test_teardown_unlinks_then_offboards_the_mirror_then_the_vault_then_the_parent():
    # Every step depends on the one before still existing: the link hangs off the
    # parent account, and the mirror is a managed system of its own that nothing else
    # would ever clean up.
    rec = Recorder()
    res = _dereg(rec, {
        "ps_managed_account_id": 500, "ps_registration_tf_state": "PARENT",
        "ps_managed_system_id": 42, "ps_vault_mirror_tf_state": "MIRROR",
        "ps_vault_mirror_account_id": "900", "ps_vault_mirror_system_id": "700",
        "ps_vault_tf_state": "VAULT", "ps_vault_account_name": "web01-adminuser"})
    assert rec.events == ["unlink", "mirror_deregister", "vault_removed",
                          "parent_deregister"], rec.events
    assert rec.calls["unlink"] == {"parent": 500, "sub": 900}
    assert res["ps_vault_mirror_removed"] == "700"
    assert res["ps_vault_removed"] == "web01-adminuser"
    assert res["ps_registration_removed"] == 42


def test_teardown_of_an_unsynced_vm_touches_only_the_parent():
    # deregister is also reached from OCI and from any VM deployed before this feature
    # existed; every step is keyed off its own metadata.
    rec = Recorder()
    res = _dereg(rec, {"ps_registration_tf_state": "PARENT", "ps_managed_system_id": 42})
    assert rec.events == ["parent_deregister"]
    assert res == {"ps_registration_removed": 42}


def test_a_partially_wired_vm_tears_down_what_it_has():
    rec = Recorder()
    _dereg(rec, {"ps_vault_tf_state": "VAULT"})
    assert rec.events == ["vault_removed"]


def test_a_vault_teardown_failure_does_not_stop_the_parent_offboard():
    rec = Recorder()
    _install(rec)
    m = sys.modules["web_dashboard.services.terraform_pra_service"]

    async def _boom(state):
        raise RuntimeError("PRA said no")
    m.remove_vault_account = _boom
    result = {}
    asyncio.run(hook.deregister(
        {"ps_vault_tf_state": "VAULT", "ps_registration_tf_state": "PARENT",
         "ps_managed_system_id": 42}, result))
    assert rec.events == ["parent_deregister"]
    assert "PRA said no" in result["ps_vault_error"]
    assert result["ps_registration_removed"] == 42


# ── 7. structural pins ───────────────────────────────────────────────────────

def _fn(name):
    for node in ast.walk(ast.parse(_HOOK_SRC)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"ps_vm_hook.{name} not found")


def test_the_sync_is_wired_outside_the_registrations_try_except():
    # The defect this pins: folding the call into register()'s try/except would let a
    # PRA-side failure overwrite ps_error and report the onboarding itself as failed
    # while the managed system is fine — and the deploy's Password Safe step would
    # look broken to an operator whose VM is correctly onboarded.
    reg = _fn("register")
    inside = any("wire_pra_vault_key_sync" in ast.dump(n)
                 for n in reg.body if isinstance(n, ast.Try))
    assert not inside, "the vault sync must not share register()'s except handler"
    assert any("wire_pra_vault_key_sync" in ast.dump(n)
               for n in reg.body if not isinstance(n, ast.Try)), \
        "register() no longer calls the vault sync at all"


def test_the_teardown_is_wrapped_so_it_can_never_fail_a_destroy():
    # No cloud's _run_destroy wraps its `await ps_vm_hook.deregister(...)`, and this
    # module's contract is that neither half ever fails a deploy or a destroy. The
    # inner per-step handlers already cover everything reachable, so this is a
    # structural pin rather than a behavioural one -- there is no realistic input that
    # reaches the outer handler, which is exactly why it is easy to delete by accident.
    for node in ast.walk(_fn("deregister")):
        if isinstance(node, ast.Try) and "unwire_pra_vault_key_sync" in ast.dump(
                ast.Module(body=node.body, type_ignores=[])):
            return
    raise AssertionError(
        "deregister calls unwire_pra_vault_key_sync outside a try -- a PRA-side error "
        "would abandon the off-board of the VM's own managed system")


def test_the_teardown_lives_in_deregister_so_every_caller_gets_the_order():
    # Rather than a block copied into each cloud's _run_destroy (which is how the OT
    # cell's equivalent is wired, three times over).
    body = ast.dump(_fn("deregister"))
    assert "unwire_pra_vault_key_sync" in body
    src = _HOOK_SRC
    assert src.index("unwire_pra_vault_key_sync(meta, result)") < \
        src.index("ps_resource_service.deregister(state)"), \
        "the unwire must precede the parent off-board"


def _converge_guard():
    """The innermost `if` whose body triggers the post-link Change Password."""
    found = []
    for node in ast.walk(_fn("wire_pra_vault_key_sync")):
        if not isinstance(node, ast.If):
            continue
        if "change_managed_account_password" in ast.dump(
                ast.Module(body=node.body, type_ignores=[])):
            found.append((len(list(ast.walk(node))), node))
    assert found, "no branch guards the post-link change_managed_account_password"
    return min(found, key=lambda pair: pair[0])[1]


def test_the_converge_does_not_read_a_clouds_change_on_register_flag():
    # The same defect the OT cell shipped and fixed: change-on-register governs
    # ONBOARDING, the converge governs a subscriber linked AFTER the initial mint. On
    # AWS the former defaults False (SSM auto-management rotates on its own schedule),
    # so reading it here leaves every VM's Vault account holding the throwaway key.
    guard = ast.dump(_converge_guard().test)
    assert "passwordsafe_vault_sync_converge" in guard
    for cloud_key in ("passwordsafe_ssm_change_password_on_register",
                      "passwordsafe_azure_change_password_on_register",
                      "passwordsafe_gcp_change_password_on_register"):
        assert cloud_key not in guard, (
            f"the converge is gated on {cloud_key} — AWS VMs would ship a throwaway "
            f"key to PRA")


def test_the_converge_defaults_on():
    call = _converge_guard().test
    assert isinstance(call, ast.Call), "expected a config_service.get_bool(...) call"
    assert len(call.args) == 2 and call.args[1].value is True, (
        "passwordsafe_vault_sync_converge must default True — a VM nobody configured "
        "further is the case that needs a real key in PRA")


def test_the_functional_account_has_no_cross_platform_fallback():
    # The OT pair falls back to the cloud-database keys because they share a platform.
    # These do not: those functional accounts are on "PRA Vault Username Password".
    src = ast.dump(_fn("wire_pra_vault_key_sync"))
    for borrowed in ("ot_ps_pravault_functional_account",
                     "clouddb_ps_pravault_functional_account",
                     "ot_ps_pravault_platform", "clouddb_ps_pravault_platform"):
        assert borrowed not in src, (
            f"{borrowed} is read here — it names an account on the Username Password "
            f"platform, which writes no key")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
