"""The POV setup ladder: order, the six states, and the green-job-with-nothing-behind-it.

``services/pov_setup_steps`` is pure — it holds no session and makes no call — so this is
the cheapest test shape in the repo: load the module by path, hand it dictionaries shaped
like the ones ``api/pov.py::_serialize`` builds, and assert the ladder. No database, no app,
no stubs, and deliberately **no module-level import guard** — there is no third-party
import here to be absent, so there is nothing for a guard to catch and nothing that could
make this file skip itself silently (see ``tests/test_import_guard_narrowness.py``).

The assertion that earns its keep is ``test_the_wireup_is_never_done_on_a_count_of_zero``,
paired with the static one below it. A POV whose guests all report no OS runs the wire-up,
skips every guest, and the job finishes **completed**. Judging that step by its job would
paint a green tick on a POV with nothing wired into anything.

Runs under pytest, or standalone: python tests/test_pov_setup_steps.py
"""
import importlib.util
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")
_SRC = os.path.join(_SERVICES, "pov_setup_steps.py")

_spec = importlib.util.spec_from_file_location("pov_setup_steps_under_test", _SRC)
steps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(steps)


def _row(**over):
    """A POV that is completely built, as a dict of the keys `_serialize` contributes.

    Every test overrides from finished rather than building up from empty, so each one
    states the single thing it is about and a key added later defaults to "fine".
    """
    base = {
        "status": "active",
        "provision_job_id": "job-1",
        "error_message": "",
        # pov_wireup.describe
        "vm_count": 2,
        "os_unknown_count": 0,
        "os_unknown_names": [],
        "wired_count": 2,
        "wiring_error_count": 0,
        "wireup_ready": True,
        # pov_broker.describe
        "broker_status": "online",
        "broker_agent_id": "agent-1",
        "broker_error": "",
        # the three tenants
        "pra_tenant_id": "t-pra",
        "ps_tenant_id": "t-ps",
        "entitle_tenant_id": "t-ent",
        # pov_gateway.describe
        "gateway_name": "gw-1",
        "gateway_has_key": True,
        "gateway_ready": True,
        # pov_resource_broker.describe
        "rb_asset": "bootstrapper.exe",
        "rb_zone": "zone-1",
        "rb_has_key": True,
        "rb_ready": True,
        # pov_entitle_agent.describe
        "entitle_agent_installed": True,
        "entitle_agent_ready": True,
        # pov_share.describe
        "shareable": True,
        "share_url": "https://share.example/x",
        "share_expired": False,
    }
    base.update(over)
    return base


def _by_key(result):
    return {s["key"]: s for s in result["steps"]}


def _state(key, **over):
    return _by_key(steps.describe(_row(**over)))[key]["state"]


# ── order and shape ──────────────────────────────────────────────────────────

def test_the_ladder_is_the_dependency_order_and_every_step_appears_once():
    """The order IS the feature — it is the thing the page could not say before."""
    result = steps.describe(_row())
    keys = [s["key"] for s in result["steps"]]
    assert keys == ["environment", "guest_os", "broker", "gateway",
                    "resource_broker", "entitle_agent", "wireup", "share"], keys
    assert len(set(keys)) == len(keys), "a step key is duplicated"
    assert keys == list(steps.STEP_KEYS), "STEP_KEYS has drifted from STEPS"
    # guest_os ahead of broker is deliberate: broker auto-detection is blind to a guest
    # with no OS, so asking for the OS first is what makes the broker step reliable.
    assert keys.index("guest_os") < keys.index("broker")
    # Every step carries the full contract, so no template has to guard on absence.
    for step in result["steps"]:
        assert set(step) == {"key", "label", "state", "detail", "action", "job_id"}, step
        assert step["label"], f"{step['key']} has no label"


def test_a_finished_pov_has_nothing_left_to_press():
    result = steps.describe(_row())
    assert result["next"] == "", result["next"]
    assert result["complete"] is True
    assert result["busy"] is False
    assert result["settled"] == result["total"]
    for step in result["steps"]:
        assert step["state"] not in ("ready", "blocked"), step


# ── skipped is the recipe, blocked is the run ────────────────────────────────

def test_no_password_safe_tenant_greys_its_step_and_the_ladder_still_reaches_sharing():
    """A PRA-only POV is COMPLETE without a Resource Broker.

    Reading "no tenant" as a blocker is how a correctly scoped evaluation reads as half
    broken forever, so this pins the state and that the ladder still finishes.
    """
    result = steps.describe(_row(ps_tenant_id="", rb_ready=False, rb_has_key=False,
                                 rb_asset="", rb_zone=""))
    by_key = _by_key(result)
    assert by_key["resource_broker"]["state"] == steps.SKIPPED
    assert by_key["share"]["state"] == steps.DONE
    assert result["complete"] is True
    # Grey, but it must name the remedy: "choose later" is a real answer on the create
    # form, so this is a pending choice and not a verdict.
    assert "Tenants column" in by_key["resource_broker"]["detail"]


def test_no_entitle_tenant_greys_only_the_entitle_step():
    result = _by_key(steps.describe(_row(entitle_tenant_id="",
                                         entitle_agent_installed=False,
                                         entitle_agent_ready=False)))
    assert result["entitle_agent"]["state"] == steps.SKIPPED
    assert result["wireup"]["state"] == steps.DONE, "the PRA half must be unaffected"


def test_a_platform_that_publishes_no_link_greys_the_share_step():
    """Every cloud POV. `share_link` is a capability, not a setting to go and turn on."""
    result = steps.describe(_row(shareable=False, share_url="", share_expired=False))
    by_key = _by_key(result)
    assert by_key["share"]["state"] == steps.SKIPPED
    assert result["complete"] is True, "a cloud POV is finished without a share link"


# ── the honesty rules ────────────────────────────────────────────────────────

def test_the_gateway_and_the_resource_broker_never_claim_to_be_done():
    """Neither has a stored installed-signal to read, on any input.

    A Gateway is a cluster, so its name is present whether or not a node of it is
    connected; and of the Resource Broker this dashboard knows only that the install has
    everything it needs. The state reserved for them means "configured, and whether it ran
    is a live question" — the rule is described here rather than by quoting the state name
    it must never take, so that a source-scanning sweep cannot match this explanation.
    """
    scenarios = [
        _row(),
        _row(gateway_ready=False, rb_ready=False),
        _row(gateway_has_key=False, rb_has_key=False),
        _row(broker_status="offline", broker_agent_id=""),
        _row(status="failed"),
        _row(wired_count=0),
    ]
    for parts in scenarios:
        by_key = _by_key(steps.describe(parts))
        for key in ("gateway", "resource_broker"):
            assert by_key[key]["state"] != steps.DONE, (key, parts.get("status"))
    # And when everything is in place it is the settled state, not a standing demand for
    # action — otherwise the cursor parks on it and the ladder never advances.
    assert _state("gateway") == steps.CONFIGURED
    assert _state("resource_broker") == steps.CONFIGURED


def test_the_wireup_is_never_done_on_a_count_of_zero():
    """The one the whole module exists for.

    `run_env_wireup` SKIPS a guest with no OS, and a skipped guest is not a failed one, so
    the job completes. The step is judged on artifacts instead: no jump items means not
    done, whatever /jobs says.
    """
    by_key = _by_key(steps.describe(_row(wired_count=0, os_unknown_count=2, vm_count=2)))
    assert by_key["wireup"]["state"] == steps.BLOCKED
    assert by_key["wireup"]["action"] == "vms", "the remedy is the OS, not another run"
    assert "still report success" in by_key["wireup"]["detail"]
    # A partial wire-up is not done either, and says where it got to.
    partial = _by_key(steps.describe(_row(wired_count=1, vm_count=3)))
    assert partial["wireup"]["state"] == steps.READY
    assert "1 of 3" in partial["wireup"]["detail"]


def test_the_wireup_source_still_completes_a_run_that_wired_nothing():
    """Why the step above ignores job status, pinned where somebody would go to check.

    If this test fails because `run_env_wireup` now FAILS a run that wired nothing, that is
    good news and the rule above can be relaxed — but relax it deliberately, here, rather
    than discovering the ladder had been trusting a job status all along.
    """
    src = io.open(os.path.join(_SERVICES, "pov_wireup.py"), encoding="utf-8").read()
    assert "if failed and not wired:" in src, (
        "run_env_wireup's completion gate has changed shape; re-read it before trusting "
        "a completed wire-up job")
    # A VM `wireable` refuses lands in the skipped counter, which the gate does not read.
    assert 'elif "skipped" in line:' in src
    assert "skipped += 1" in src


# ── in-flight, and the five broker states ────────────────────────────────────

def test_a_pov_mid_provision_offers_nothing_to_press():
    """`may_act_on` refuses every action while a POV is mid-job, so the ladder must not
    offer one. Pointing past a running step is how a page grows a button that answers 409."""
    result = steps.describe(_row(status="provisioning"))
    by_key = _by_key(result)
    assert by_key["environment"]["state"] == steps.RUNNING
    assert by_key["environment"]["job_id"] == "job-1", "the running step links its job"
    assert result["busy"] is True
    assert result["next"] == ""
    assert result["complete"] is False


def test_a_failed_provision_shows_the_platforms_own_reason():
    by_key = _by_key(steps.describe(_row(status="failed",
                                         error_message="Skytap said 422")))
    assert by_key["environment"]["state"] == steps.BLOCKED
    assert by_key["environment"]["detail"] == "Skytap said 422"


def test_every_broker_status_maps_to_exactly_one_state():
    """Five values, not three: `status_of` returns four and `pov_broker.describe` adds one.

    `offline` and `revoked` are the two that were easiest to leave out, and they are the
    ones that most need a button.
    """
    expected = {
        "online": steps.DONE,
        "enrolling": steps.RUNNING,
        "offline": steps.READY,
        "revoked": steps.READY,
        "none": steps.READY,
    }
    for status, want in expected.items():
        got = _state("broker", broker_status=status)
        assert got == want, f"broker_status {status!r} -> {got!r}, wanted {want!r}"


def test_a_broker_that_never_enrolled_carries_the_reason_already_on_the_row():
    """`ensure_broker` waits out the enrolment code's full TTL inside the provision job, so
    once the environment is active there is nothing still coming — only a recorded remedy."""
    step = _by_key(steps.describe(_row(broker_status="none", broker_agent_id="",
                                       broker_error="no VM reported a private address")))[
        "broker"]
    assert step["state"] == steps.READY
    assert step["detail"] == "no VM reported a private address"
    assert step["action"] == "broker"


# ── the blank-OS step itself ──────────────────────────────────────────────────

def test_unknown_guest_os_blocks_and_names_the_guests():
    step = _by_key(steps.describe(_row(
        vm_count=4, os_unknown_count=2,
        os_unknown_names=["BtPocLin01", "BtPocWin02"])))["guest_os"]
    assert step["state"] == steps.BLOCKED
    assert "2 of 4" in step["detail"]
    assert "BtPocLin01" in step["detail"] and "BtPocWin02" in step["detail"]
    assert step["action"] == "vms"


def test_the_names_are_capped_and_say_so():
    """`os_unknown_names` is capped by pov_wireup.describe, so the count and the list can
    disagree — and a list that silently stopped short would understate the work."""
    step = _by_key(steps.describe(_row(
        vm_count=9, os_unknown_count=9,
        os_unknown_names=["a", "b", "c", "d"])))["guest_os"]
    assert "and others" in step["detail"]


def test_a_pov_whose_vms_have_not_been_read_back_yet_is_blocked_not_done():
    """Zero VMs must never read as "every guest's OS is known"."""
    assert _state("guest_os", vm_count=0, os_unknown_count=0) == steps.BLOCKED


# ── the cursor ────────────────────────────────────────────────────────────────

def test_the_cursor_is_the_first_unsettled_step_and_carries_its_action():
    result = steps.describe(_row(wired_count=0, os_unknown_count=0, vm_count=2,
                                 shareable=True, share_url="", share_expired=False))
    assert result["next"] == "wireup", result["next"]
    assert result["next_action"] == "wireup"
    assert result["next_blocked"] is False
    assert result["next_label"]


def test_the_cursor_skips_the_two_steps_that_cannot_report_done():
    """Otherwise a fully configured POV parks on the Gateway forever and never offers the
    share link, which is the last thing an SE actually wants."""
    result = steps.describe(_row(shareable=True, share_url="", share_expired=False))
    assert result["next"] == "share", result["next"]


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
