"""Running a staged file on a named POV guest — the grant, and the refusals.

The machinery for this already shipped with slice 5b; what was missing was permission.
``pov_broker.render_policy`` grants the agent a **closed list of /32 targets**, and that
list was one address once somebody had named a Resource Broker host. So what is pinned
here is mostly about who may be reached, and about the refusals that keep a permission
problem from arriving as a network one:

  * **The grant widens by opt-in only.** A POV that has named no guest renders exactly the
    policy it rendered before this existed. Widening a Config-Management grant by default
    would hand every guest in the environment a playbook target nobody asked for.
  * **The three grants union, they do not replace.** Where the Resource Broker goes, where
    the Entitle agent goes, and which guests an SE wants to configure are different
    questions, and a POV can want all three.
  * **A guest named after the last Broker run is refused, not attempted.** policy.yaml is
    written at enrolment, so the grant lags the list. Left alone, the run fails as a WinRM
    timeout — which reads as a firewall, a credential, or a guest that is not up, in any
    order but the right one.
  * **The asset must match the guest's family**, and a blank ``os_family`` matches neither:
    slice 3 made blank mean unknown, and a guest granted the wrong port fails as a timeout
    too.
  * **Arguments are refused for an asset that cannot take them.** Only the ``win_package``
    play templates a variable for them, so a value typed against a ``.ps1`` would be
    stored on the job and silently have no effect.
  * **There is no login field**, and the job carries ids rather than a credential — the
    same channel the Resource Broker install uses.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_guest_step.py
"""
import json
import os
import sys
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-guest-step")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, pov_broker,  # noqa: E402
                                    pov_env_service, pov_guest_step as gs,
                                    pov_resource_broker)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _agent(db, *, version="2.4.0",
           reported=("agent_discover", "agent_gateway", "agent_ansible")):
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "k"
    row.agent_version = version
    row.reported_job_types = json.dumps(list(reported))
    row.last_seen_at = datetime.utcnow()
    db.commit()
    return row


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name, os_family="windows", ip="10.9.0.20"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


def _ready(db, *, asset="Setup.ps1"):
    """A POV with a broker agent, one opted-in Windows guest, and a staged asset."""
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    vm = _vm(db, env, name="app01", ip="10.9.0.30")
    gs.configure(db, env, vm_names=["app01"])
    gs.record_grant(db, env, ["10.9.0.30"])
    return env, agent, vm, asset


# ── the opt-in list ──────────────────────────────────────────────────────────

def test_a_pov_opts_no_guest_in_by_default():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="app01")
    assert gs.opted_in_names(env) == []
    assert gs.windows_targets(db, env) == []
    assert gs.linux_targets(db, env) == []
    db.close()


def test_naming_a_guest_grants_only_that_guest():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="app01", ip="10.9.0.30")
    _vm(db, env, name="dc01", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["app01"])
    assert gs.windows_targets(db, env) == ["10.9.0.30"]
    db.close()


def test_names_are_deduplicated_case_insensitively_and_keep_their_order():
    db = d.SessionLocal()
    env = _env(db)
    gs.configure(db, env, vm_names=["App01", "dc01", "APP01", "  ", "dc01"])
    assert gs.opted_in_names(env) == ["App01", "dc01"]
    db.close()


def test_none_leaves_the_list_alone_and_an_empty_list_clears_it():
    """Three intentions, three values -- the rule `pov_use_cases.set_state` follows for a
    note. Without it, a form that omits the field would silently revoke every grant."""
    db = d.SessionLocal()
    env = _env(db)
    gs.configure(db, env, vm_names=["app01"])
    gs.configure(db, env, vm_names=None)
    assert gs.opted_in_names(env) == ["app01"]
    gs.configure(db, env, vm_names=[])
    assert gs.opted_in_names(env) == []
    db.close()


def test_a_named_guest_that_does_not_exist_yet_is_kept_not_refused():
    """An SE names a template Part 2 guest before those VMs have been added. The policy
    render must not fail on it, so the refusal belongs on the run."""
    db = d.SessionLocal()
    env = _env(db)
    gs.configure(db, env, vm_names=["app02"])
    assert gs.opted_in_names(env) == ["app02"]
    assert gs.opted_in_vms(db, env) == []
    assert gs.windows_targets(db, env) == []
    db.close()


def test_a_guest_with_no_known_os_is_in_neither_target_list():
    """Blank means unknown. A guest granted the wrong port fails as a timeout."""
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="mystery", os_family="", ip="10.9.0.40")
    gs.configure(db, env, vm_names=["mystery"])
    assert gs.windows_targets(db, env) == [] and gs.linux_targets(db, env) == []
    db.close()


def test_the_two_families_are_split_by_port_not_merged():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="app01", os_family="windows", ip="10.9.0.30")
    _vm(db, env, name="lin01", os_family="linux", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["app01", "lin01"])
    assert gs.windows_targets(db, env) == ["10.9.0.30"]
    assert gs.linux_targets(db, env) == ["10.9.0.31"]
    db.close()


# ── the policy render unions the three grants ────────────────────────────────

def test_the_rendered_policy_grants_the_opted_in_guest_as_well_as_the_rb_host():
    """The substance of the slice. Before it, the WinRM grant was the RB host alone."""
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="rb", os_family="windows", ip="10.9.0.20")
    _vm(db, env, name="app01", os_family="windows", ip="10.9.0.30")
    pov_resource_broker.configure(db, env, vm_name="rb")
    gs.configure(db, env, vm_names=["app01"])

    win = sorted(set(pov_resource_broker.windows_targets(db, env))
                 | set(gs.windows_targets(db, env)))
    policy = pov_broker.render_policy(["10.9.0.20"], win, [])
    assert "10.9.0.20/32" in policy and "10.9.0.30/32" in policy
    assert "5985" in policy
    db.close()


def test_a_pov_with_no_opted_in_guest_renders_what_it_rendered_before():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="rb", os_family="windows", ip="10.9.0.20")
    pov_resource_broker.configure(db, env, vm_name="rb")
    before = pov_broker.render_policy(
        ["10.9.0.20"], pov_resource_broker.windows_targets(db, env), [])
    after = pov_broker.render_policy(
        ["10.9.0.20"],
        sorted(set(pov_resource_broker.windows_targets(db, env))
               | set(gs.windows_targets(db, env))), [])
    assert before == after
    db.close()


def test_the_call_site_unions_rather_than_replaces():
    """Pinned against the source, because the union is one line and losing it would put
    the feature back to a single granted address without failing a test."""
    with open(os.path.join(_ROOT, "web_dashboard", "services", "pov_broker.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    block = src.split("def ensure_broker", 1)[1]
    assert "pov_guest_step.windows_targets(db, env)" in block
    assert "pov_guest_step.linux_targets(db, env)" in block
    assert "pov_guest_step.record_grant(db, env," in block


# ── the recorded grant, and the refusal it enables ──────────────────────────

def test_the_grant_is_recorded_so_a_later_name_can_be_refused():
    db = d.SessionLocal()
    env = _env(db)
    gs.record_grant(db, env, ["10.9.0.30", "10.9.0.20", "10.9.0.30"])
    assert gs.granted_addresses(env) == ["10.9.0.20", "10.9.0.30"]
    gs.record_grant(db, env, [])
    assert gs.granted_addresses(env) == []
    db.close()


def test_a_guest_named_after_the_last_broker_run_is_refused_with_press_broker():
    """The refusal that stops a support call. Left alone this is a WinRM timeout."""
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["app01", "dc01"])
    try:
        gs.preflight(db, env, vm_name="dc01", asset=asset)
        raise AssertionError("a guest outside the recorded grant was accepted")
    except gs.GuestStepError as exc:
        assert "Press Broker" in str(exc) and "timeout" in str(exc)
    db.close()


def test_a_pov_with_no_recorded_grant_is_not_refused_on_that_ground():
    """A POV brokered before this feature existed has no record. Refusing on an absent
    record would strand every one of them behind a re-broker they cannot be told about."""
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    gs.record_grant(db, env, [])
    agent, vm = gs.preflight(db, env, vm_name="app01", asset=asset)
    assert vm.name == "app01" and agent is not None
    db.close()


# ── the target ───────────────────────────────────────────────────────────────

def test_an_unnamed_guest_is_refused_with_what_the_pov_has():
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    try:
        gs.preflight(db, env, vm_name="nope", asset=asset)
        raise AssertionError("an unknown guest was accepted")
    except gs.GuestStepError as exc:
        assert "no guest called" in str(exc) and "app01" in str(exc)
    db.close()


def test_a_guest_not_on_the_list_is_refused_before_anything_runs():
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.31")
    try:
        gs.preflight(db, env, vm_name="dc01", asset=asset)
        raise AssertionError("a guest outside the opt-in list was accepted")
    except gs.GuestStepError as exc:
        assert "not opened up" in str(exc)
    db.close()


def test_the_match_is_exact_and_case_insensitive():
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    _agent, vm = gs.preflight(db, env, vm_name="APP01", asset=asset)
    assert vm.name == "app01"
    db.close()


def test_a_guest_with_no_address_is_refused():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="app01", ip="")
    gs.configure(db, env, vm_names=["app01"])
    try:
        gs.preflight(db, env, vm_name="app01", asset="Setup.ps1")
        raise AssertionError("a guest with no address was accepted")
    except gs.GuestStepError as exc:
        assert "no address" in str(exc)
    db.close()


# ── the asset ────────────────────────────────────────────────────────────────

def test_no_staged_asset_is_refused_and_points_at_storage():
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    try:
        gs.preflight(db, env, vm_name="app01", asset="")
        raise AssertionError("a step with no asset was accepted")
    except gs.GuestStepError as exc:
        assert "Storage" in str(exc)
    db.close()


def test_a_linux_asset_on_a_windows_guest_is_refused():
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    try:
        gs.preflight(db, env, vm_name="app01", asset="setup.sh")
        raise AssertionError("a .sh was accepted for a Windows guest")
    except gs.GuestStepError as exc:
        assert "Linux asset" in str(exc) and ".ps1" in str(exc)
    db.close()


def test_a_windows_asset_on_a_linux_guest_is_refused():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="lin01", os_family="linux", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["lin01"])
    gs.record_grant(db, env, ["10.9.0.31"])
    try:
        gs.preflight(db, env, vm_name="lin01", asset="Setup.ps1")
        raise AssertionError("a .ps1 was accepted for a Linux guest")
    except gs.GuestStepError as exc:
        assert "Windows asset" in str(exc) and ".sh" in str(exc)
    db.close()


def test_a_playbook_is_accepted_for_either_family():
    """A .yml is used as-is and carries its own hosts/connection choices."""
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    _agent, vm = gs.preflight(db, env, vm_name="app01", asset="site.yml")
    assert vm.name == "app01"
    db.close()


def test_arguments_are_refused_for_an_asset_that_cannot_take_them():
    """Only the win_package play templates a variable for them. Accepted-and-dropped is
    the shape this codebase treats as a bug."""
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    try:
        gs.preflight(db, env, vm_name="app01", asset="Setup.ps1", arguments="/quiet")
        raise AssertionError("arguments were accepted for a .ps1")
    except gs.GuestStepError as exc:
        assert "takes no arguments" in str(exc)
    db.close()


def test_arguments_are_accepted_for_a_windows_installer():
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    _agent, vm = gs.preflight(db, env, vm_name="app01", asset="sql.exe",
                              arguments="/Q /ACTION=Install")
    assert vm.name == "app01"
    db.close()


# ── the agent ────────────────────────────────────────────────────────────────

def test_a_pov_with_no_broker_agent_is_refused():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="app01", ip="10.9.0.30")
    gs.configure(db, env, vm_names=["app01"])
    try:
        gs.preflight(db, env, vm_name="app01", asset="Setup.ps1")
        raise AssertionError("a POV with no broker agent was accepted")
    except gs.GuestStepError as exc:
        assert "Broker" in str(exc)
    db.close()


def test_an_agent_that_does_not_report_ansible_is_told_to_re_broker():
    db = d.SessionLocal()
    agent = _agent(db, reported=("agent_discover",))
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="app01", ip="10.9.0.30")
    gs.configure(db, env, vm_names=["app01"])
    gs.record_grant(db, env, ["10.9.0.30"])
    try:
        gs.preflight(db, env, vm_name="app01", asset="Setup.ps1")
        raise AssertionError("an agent with no ansible grant was accepted")
    except gs.GuestStepError as exc:
        assert "policy.yaml" in str(exc) and "Press Broker" in str(exc)
    db.close()


# ── the job ──────────────────────────────────────────────────────────────────

def test_the_job_is_agent_ansible_at_the_guest_over_winrm():
    """No new agent verb, so no image rebuild — the whole reason this rides agent_ansible."""
    db = d.SessionLocal()
    env, agent, vm, asset = _ready(db)
    job = gs.queue(db, env, vm_name="app01", asset=asset, created_by="se")
    assert job.job_type == "agent_ansible"
    assert job.agent_id == agent.id
    meta = job.metadata_dict
    assert meta["run_kind"] == "vm" and meta["transport"] == "winrm"
    assert meta["target_host"] == "10.9.0.30"
    assert int(meta["target_port"]) == gs.WINRM_PORT
    assert meta["asset"] == asset
    db.close()


def test_a_linux_guest_runs_over_ssh_on_22():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="lin01", os_family="linux", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["lin01"])
    gs.record_grant(db, env, ["10.9.0.31"])
    job = gs.queue(db, env, vm_name="lin01", asset="setup.sh", created_by="se")
    meta = job.metadata_dict
    assert meta["transport"] == "ssh" and int(meta["target_port"]) == gs.SSH_PORT
    db.close()


def test_the_job_carries_pov_ids_so_the_login_comes_from_the_platform():
    """The reason there is no login field. `agent_ansible_bundle` reads the lab platform's
    stored credential for that VM at fetch time, so nothing has to be stored."""
    db = d.SessionLocal()
    env, _a, vm, asset = _ready(db)
    job = gs.queue(db, env, vm_name="app01", asset=asset, created_by="se")
    meta = job.metadata_dict
    assert meta["pov_environment_id"] == env.id
    assert meta["pov_vm_id"] == vm.platform_vm_id
    db.close()


def test_the_job_carries_no_credential_and_not_even_a_ref_to_one():
    """`RUN_META_KEYS` allows a secret SOURCE -- a var name bound to a config path, which
    is how the Resource Broker install delivers its installer key. This path needs none:
    the guest login comes from the lab platform at bundle-assembly time, so the secret
    fields stay empty rather than pointing somewhere. Pinned because filling one in later
    would be the moment a credential started living in a job row."""
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    job = gs.queue(db, env, vm_name="app01", asset=asset, created_by="se")
    meta = job.metadata_dict
    assert not meta["secret_vars"]
    assert not meta["secret_become_source"]
    assert not meta["secret_ssh_key_source"]
    assert not meta["managed_account"] and not meta["managed_become"]
    # No login either: an ansible_user here would be a guess at the template's account.
    assert not meta["login_user"]
    # And nothing that looks like the shared template credential.
    blob = json.dumps(meta).lower()
    for smell in ("password", "univers@l", "btadmin"):
        assert smell not in blob, f"the job row carries {smell!r}"
    db.close()


def test_installer_arguments_reach_the_play_as_a_variable():
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    job = gs.queue(db, env, vm_name="app01", asset="sql.exe",
                   arguments="/Q /ACTION=Install", created_by="se")
    from web_dashboard.services import ansible_local_service
    assert (job.metadata_dict["extra_vars"][ansible_local_service.WINPKG_ARGS_VAR]
            == "/Q /ACTION=Install")
    db.close()


def test_a_step_with_no_arguments_sends_no_extra_vars():
    db = d.SessionLocal()
    env, _a, _v, asset = _ready(db)
    job = gs.queue(db, env, vm_name="app01", asset=asset, created_by="se")
    assert job.metadata_dict["extra_vars"] == {}
    db.close()


# ── what the UI shows ────────────────────────────────────────────────────────

def test_describe_reports_the_list_and_whether_the_grant_is_stale():
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    fresh = gs.describe(db, env)
    assert fresh["guest_step_vms"] == ["app01"]
    assert fresh["guest_step_resolved"] == ["app01"]
    assert fresh["guest_step_ready"] is True
    assert fresh["guest_step_stale"] is False

    _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.31")
    gs.configure(db, env, vm_names=["app01", "dc01"])
    stale = gs.describe(db, env)
    assert stale["guest_step_stale"] is True
    assert stale["guest_step_pending_grant"] == ["10.9.0.31"]
    db.close()


def test_describe_makes_no_network_call():
    """It is spread onto every row of the POV list, so a call here would be one per row."""
    import socket
    db = d.SessionLocal()
    env, _a, _v, _asset = _ready(db)
    original = socket.socket

    def _boom(*a, **k):
        raise AssertionError("describe opened a socket")

    socket.socket = _boom
    try:
        gs.describe(db, env)
    finally:
        socket.socket = original
    db.close()


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_vms_tab_colspan_matches_its_header_count():
    """The Configure column was added to a table whose empty-state row spans it.
    `tests/test_templates_parse` checks this for the inventory tables and not for the POV
    ones, so it is pinned here — a mismatched colspan is a page that renders subtly wrong
    only when a POV has no VMs yet, which is exactly when nobody is looking."""
    import re
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    blk = src.split("tab === 'vms'", 1)[1].split("tab === 'wired'", 1)[0]
    headers = len(re.findall(r"<th ", blk))
    assert headers == 5, f"the VMs tab has {headers} columns; the test expected 5"
    for span in re.findall(r'colspan="(\d+)"', blk):
        assert int(span) == headers, f"colspan {span} disagrees with {headers} columns"


def test_the_page_posts_the_opt_in_list_and_the_run_separately():
    """Ticking a guest is a PERMISSION change and must not wait for a file to be chosen:
    an operator ticks, then presses Broker, then comes back to run something."""
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "run: false" in src, "the opt-in save does not post run:false"
    assert "run: true" in src, "the step run does not post run:true"
    # The refusals name their remedy, so the page must show them rather than a status code.
    assert "data.detail" in src


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
