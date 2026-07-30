"""Unit tests for services/expiry_policy.py — the auto-delete timer's pure policy.

The headline test here is :func:`test_a_resource_with_no_expiry_is_never_returned`. It
pins the property the whole feature's safety rests on: every row that predates the
``expires_at`` column backfilled to NULL, so enabling auto-delete on a live fleet must
select ZERO resources. Not "zero because a guard caught them" — zero because the predicate
has no input. If that test ever fails, flipping one toggle destroys an estate.

The rest of the file checks each reap guard independently, and that the floors an operator
cannot lower are still where they should be (mirroring
test_cloud_run_job_reaper.test_the_age_guard_is_floored_below_a_runners_own_lifetime).

Pure Python: ``config_service`` and ``config.settings`` are stubbed, no DB, no deps.
Runs under pytest, or standalone:
    python tests/test_expiry_policy.py
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Simulated config store, keyed exactly as app_config is. Unset keys resolve to "" —
# what the real config_service.get returns for a key nobody wrote.
CONF = {}


def _install_stubs():
    """Register stub parent packages so expiry_policy's relative imports resolve while
    it is loaded by file path (mirrors test_setup_feature_roundtrip._install_config_stub).
    Loading by path is what keeps this runnable on a bare checkout."""
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    # expiry_policy falls back to Settings when a config key is unset. An empty object is
    # enough — getattr(settings, key, default) then yields the literal default.
    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = conf_mod


_install_stubs()
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "expiry_policy.py")
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.expiry_policy", _PATH)
pol = importlib.util.module_from_spec(_spec)
sys.modules["web_dashboard.services.expiry_policy"] = pol
_spec.loader.exec_module(pol)


# ── helpers ──────────────────────────────────────────────────────────────────

NOW = datetime(2026, 7, 28, 12, 0, 0)
NOW_TS = NOW.timestamp()
HOUR = 3600


def _on(**overrides):
    """Turn the feature on with a 24h default, plus any overrides."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    CONF["resource_expiry_default_hours"] = "24"
    CONF.update({k: str(v) for k, v in overrides.items()})


def _item(**over):
    """A reapable, overdue, provisioned AWS VM — the happy path every guard test
    breaks in exactly one way."""
    base = {
        "id": "job:abc", "kind": "vm", "cloud": "aws", "name": "vm-1",
        "source": "provisioned", "state": "active", "workgroup": None,
        "region": "us-east-1", "job_id": "abc",
        "expires_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    base.update(over)
    return base


def _reap(item, *, grace=30, armed_ago_min=180):
    armed_ts = NOW_TS - armed_ago_min * 60 if armed_ago_min is not None else None
    return pol.reap_target(item, now_ts=NOW_TS, grace_min=grace, armed_at_ts=armed_ts)


# ── the retroactivity guarantee ──────────────────────────────────────────────

def test_a_resource_with_no_expiry_is_never_returned():
    """THE safety property. Every row that existed before this feature has
    expires_at NULL, so a fresh enable must select nothing at all.

    Deliberately the inverse of ephemeral_secrets.expired, which treats an
    undateable item as expired: that GC leaks a credential if it's wrong, this one
    deletes somebody's running database."""
    items = [{"id": "a", "expires_ts": None}, {"id": "b", "expires_ts": 0},
             {"id": "c"}, {"id": "d", "expires_ts": -1}]
    assert pol.expired(items, NOW_TS, grace_min=30) == []
    assert _reap(_item(expires_at=None)) is None
    assert _reap(_item(expires_at="")) is None


def test_an_unparseable_expiry_is_never_returned():
    """A timestamp we can't read is not an expiry — the reaper only acts on a
    resource it can date."""
    for bad in ("tomorrow", "2026-13-45", "  ", "0000"):
        assert pol.parse_ts(bad) is None, bad
        assert _reap(_item(expires_at=bad)) is None, bad


# ── expired(): the grace floor ───────────────────────────────────────────────

def test_expired_returns_only_what_is_past_expiry_plus_grace():
    items = [
        {"id": "way-over", "expires_ts": NOW_TS - 6 * HOUR},
        {"id": "just-over", "expires_ts": NOW_TS - 31 * 60},   # 31m > 30m grace
        {"id": "in-grace", "expires_ts": NOW_TS - 5 * 60},     # inside grace → keep
        {"id": "future", "expires_ts": NOW_TS + HOUR},
    ]
    assert set(pol.expired(items, NOW_TS, grace_min=30)) == {"way-over", "just-over"}


def test_a_zero_grace_config_cannot_lower_the_floor():
    """A misconfigured 0 must not make the sweeper act at the instant of expiry —
    clock skew between the app and the worker alone would be enough to misfire."""
    just_expired = [{"id": "x", "expires_ts": NOW_TS - 60}]        # 1 min past
    assert pol.expired(just_expired, NOW_TS, grace_min=0) == []
    assert pol.expired(just_expired, NOW_TS, grace_min=-99) == []
    # Past the floor it does come through.
    older = [{"id": "x", "expires_ts": NOW_TS - (pol.REAP_GRACE_MIN_FLOOR + 1) * 60}]
    assert pol.expired(older, NOW_TS, grace_min=0) == ["x"]


# ── reap_target(): one test per guard ────────────────────────────────────────

def test_the_happy_path_is_a_target():
    _on()
    t = _reap(_item())
    assert t is not None
    assert t["id"] == "job:abc" and t["kind"] == "vm" and t["cloud"] == "aws"
    assert t["overdue_s"] == 2 * HOUR


def test_every_reapable_cloud_and_kind_can_be_a_target():
    _on()
    for cloud in pol.REAPABLE_VM_CLOUDS:
        assert _reap(_item(cloud=cloud)) is not None, cloud
    assert _reap(_item(kind="database", cloud="aws", state="available")) is not None
    for state in ("registered", "managed", "awaiting_agent"):
        assert _reap(_item(kind="k8s", cloud="gcp", state=state)) is not None, state


def test_an_unarmed_feature_reaps_nothing():
    """A freshly flipped toggle must not act on a fleet nobody has reviewed."""
    _on()
    assert _reap(_item(), armed_ago_min=None) is None          # never armed
    assert _reap(_item(), armed_ago_min=5) is None             # arming
    assert _reap(_item(), armed_ago_min=pol.ARM_DELAY_MINUTES - 1) is None
    assert _reap(_item(), armed_ago_min=pol.ARM_DELAY_MINUTES) is not None


def test_a_registered_resource_is_never_a_target():
    """Deleting a registered row only drops the dashboard's own record of somebody
    else's database. A timer that silently does that is worse than no timer."""
    _on()
    for kind, state in (("database", "available"), ("k8s", "registered")):
        assert _reap(_item(kind=kind, state=state, source="registered")) is None, kind


def test_kinds_and_clouds_without_a_queued_teardown_are_never_targets():
    """Desktop seats are torn down with their pool; Proxmox/Nutanix deletes run
    in-request and are not in jobs_worker.HANDLED_TYPES, so there is no job to
    enqueue and the reaper must not pretend otherwise."""
    _on()
    assert _reap(_item(kind="desktop")) is None
    for cloud in ("proxmox", "nutanix", "local", ""):
        assert _reap(_item(cloud=cloud)) is None, cloud


def test_only_idle_states_are_targets_and_unknown_states_fail_safe():
    """Never a row mid-provision, mid-decommission or failed — and an unrecognised
    status is refused, so a state added elsewhere can only make the reaper do LESS."""
    _on()
    for state in ("provisioning", "decommissioning", "failed", "error", "deleting", ""):
        assert _reap(_item(state=state)) is None, state
    assert _reap(_item(state="some-future-status")) is None
    assert _reap(_item(kind="database", state="active")) is None   # vm state, wrong kind


def test_an_exempt_workgroup_is_never_a_target():
    _on(resource_expiry_exempt_workgroups="platform, Shared-Infra")
    assert _reap(_item(workgroup="platform")) is None
    assert _reap(_item(workgroup="SHARED-INFRA")) is None          # casefolded
    assert _reap(_item(workgroup="sandbox")) is not None


def test_a_resource_inside_its_grace_period_is_not_a_target():
    _on()
    assert _reap(_item(expires_at=(NOW - timedelta(minutes=5)).isoformat())) is None
    assert _reap(_item(expires_at=(NOW + timedelta(hours=3)).isoformat())) is None


# ── floors an operator cannot lower ──────────────────────────────────────────

def test_the_floors_cannot_be_lowered_by_a_future_edit():
    """These bound how wrong a misconfiguration can be. Lowering one in the source is
    a policy change, not a tweak, and should have to break a test to happen."""
    assert pol.ARM_DELAY_MINUTES >= 30
    assert pol.REAP_GRACE_MIN_FLOOR >= 5
    assert pol.MIN_TTL_MINUTES_FLOOR >= 30
    assert pol.MAX_PER_PASS_CEILING <= 100


def test_max_per_pass_is_clamped_to_the_ceiling():
    _on(resource_expiry_max_per_pass=9999)
    assert pol.max_per_pass() == pol.MAX_PER_PASS_CEILING
    _on(resource_expiry_max_per_pass=0)
    assert pol.max_per_pass() >= 1


def test_sweep_interval_and_grace_have_floors():
    _on(resource_expiry_sweep_interval_minutes=0, resource_expiry_grace_minutes=0)
    assert pol.sweep_interval_seconds() >= 5 * 60
    assert pol.grace_minutes() >= pol.REAP_GRACE_MIN_FLOOR


# ── enqueue dedupe window ────────────────────────────────────────────────────

def test_the_dedupe_window_never_swallows_the_next_scheduled_tick():
    """THE property of sweep_min_gap_seconds, at every interval an operator can set.

    The window suppresses a second sweep row. If it ever reached a full interval it would
    suppress the *next legitimate* tick too and the feature would silently halve — or stop.
    Strictly less than the interval is what makes it a duplicate filter rather than a rate
    limit, and it has to hold at the floor as well as at absurd values.
    """
    for minutes in (0, 1, 5, 7, 30, 90, 1440):
        _on(resource_expiry_sweep_interval_minutes=minutes)
        interval = pol.sweep_interval_seconds()
        gap = pol.sweep_min_gap_seconds()
        assert 0 < gap < interval, f"{minutes}m: gap {gap} vs interval {interval}"


def test_the_dedupe_window_is_wide_enough_for_two_app_workers():
    """The race it exists to close is sub-second — two gunicorn workers ~0.4s apart, with
    the first row already `completed` because an empty pass takes ~0s. Any window in
    minutes closes it; this pins that the window is not accidentally in that same
    sub-second range where it would do nothing at all."""
    _on(resource_expiry_sweep_interval_minutes=5)          # the tightest allowed cadence
    assert pol.sweep_min_gap_seconds() >= 60


# ── sweep-history retention ──────────────────────────────────────────────────

def test_sweep_retention_defaults_to_a_bounded_window():
    """Unset must not mean "keep forever": the sweep writes 48 rows/day at the default
    interval and nothing else prunes `jobs`."""
    _on()
    assert 0 < pol.sweep_retention_days() <= 30


def test_sweep_retention_zero_means_keep_forever():
    """An explicit 0 is a real choice — an operator who wants the full history — and must
    survive the read rather than being clamped up to the default."""
    _on(resource_expiry_sweep_retention_days=0)
    assert pol.sweep_retention_days() == 0
    _on(resource_expiry_sweep_retention_days=-5)           # nonsense floors to 0, not 7
    assert pol.sweep_retention_days() == 0


def test_clamp_hours_clamps_both_directions():
    _on(resource_expiry_max_total_hours=720)
    floor_h = pol.MIN_TTL_MINUTES_FLOOR / 60.0
    for tiny in (0, -5, 0.01):
        assert pol.clamp_hours(tiny) == floor_h, tiny
    assert pol.clamp_hours(99999) == 720
    assert pol.clamp_hours("not a number") == floor_h
    # An admin bypasses the ceiling but never the floor.
    assert pol.clamp_hours(99999, is_admin=True) == 99999
    assert pol.clamp_hours(0, is_admin=True) == floor_h


def test_the_ceiling_is_measured_from_creation_not_from_now():
    """A per-extension cap is defeated by clicking twice, so the ceiling counts total
    lifetime from created_at."""
    _on(resource_expiry_max_total_hours=100)
    created = pol._now() - timedelta(hours=90)
    # Only ~10h of ceiling left, however much is asked for.
    assert round(pol.clamp_hours(50, created_at=created)) == 10
    # A resource already past its ceiling collapses to the floor, never to the past.
    ancient = pol._now() - timedelta(hours=500)
    assert pol.clamp_hours(50, created_at=ancient) == pol.MIN_TTL_MINUTES_FLOOR / 60.0


def test_no_ceiling_when_max_total_hours_is_zero():
    _on(resource_expiry_max_total_hours=0)
    assert pol.clamp_hours(5000) == 5000


# ── stamping ─────────────────────────────────────────────────────────────────

def test_only_vm_deploy_job_types_are_stamped():
    """Everything else — destroys, bulk parents, provisions, the sweep itself — must
    come back None, and on a set-membership test before any config read."""
    _on()
    for jt in pol.REAPABLE_VM_JOB_TYPES:
        assert pol.default_expiry_for(jt) is not None, jt
    for jt in ("ec2_bulk_deploy", "azure_bulk_deploy", "gce_bulk_deploy",
               "oci_bulk_deploy", "ec2_destroy", "k8s_provision",
               "clouddb_provision", "expiry_sweep", "ansible_cloud_run",
               "proxmox_deploy", "nutanix_deploy", "gateway_deploy"):
        assert pol.default_expiry_for(jt) is None, jt


def test_nothing_is_stamped_while_the_feature_is_off():
    CONF.clear()
    CONF["resource_expiry_default_hours"] = "24"       # configured but feature off
    assert pol.default_expiry_for("ec2_deploy") is None
    assert pol.default_expiry_for_kind("database") is None


def test_nothing_is_stamped_when_the_default_is_zero():
    """An install that flips only the master switch stamps nothing — the second of the
    two deliberate acts required before anything can ever expire."""
    _on(resource_expiry_default_hours=0)
    assert pol.default_expiry_for("ec2_deploy") is None
    assert pol.default_expiry_for_kind("k8s") is None


def test_an_exempt_workgroup_is_not_stamped():
    _on(resource_expiry_exempt_workgroups="platform")
    assert pol.default_expiry_for("ec2_deploy", workgroup="platform") is None
    assert pol.default_expiry_for("ec2_deploy", workgroup="PLATFORM") is None
    assert pol.default_expiry_for("ec2_deploy", workgroup="sandbox") is not None


def test_registered_resources_are_not_stamped():
    _on()
    assert pol.default_expiry_for_kind("database", source="registered") is None
    assert pol.default_expiry_for_kind("k8s", source="registered") is None
    assert pol.default_expiry_for_kind("database", source="provisioned") is not None


def test_kinds_without_a_reaper_are_not_stamped():
    _on()
    assert pol.default_expiry_for_kind("desktop") is None
    assert pol.default_expiry_for_kind("gateway") is None
    # "vm" goes through default_expiry_for (the job seam), not this one.
    assert pol.default_expiry_for_kind("vm") is None


def test_a_stamped_default_is_clamped_to_the_floor():
    _on(resource_expiry_default_hours=1)          # 1h == the floor, allowed
    stamped = pol.default_expiry_for("ec2_deploy", now=NOW)
    assert stamped == NOW + timedelta(hours=pol.MIN_TTL_MINUTES_FLOOR / 60.0)


# ── destroy-type mapping ─────────────────────────────────────────────────────

def test_build_destroy_metadata_matches_each_endpoint():
    """The exact keys each DELETE endpoint puts on its destroy job. A reap that builds a
    different shape produces a job the runner mis-reads — or worse, one it reads as
    pointing somewhere else."""
    cases = {
        "ec2_deploy": ({"instance_id": "i-1", "region": "us-east-2"},
                       "ec2_destroy",
                       {"instance_id": "i-1", "region": "us-east-2", "deploy_job_id": "d"}),
        "azure_deploy": ({"vm_name": "az", "resource_group": "rg"},
                         "azure_destroy",
                         {"vm_name": "az", "resource_group": "rg", "deploy_job_id": "d"}),
        "gce_deploy": ({"instance_name": "g", "zone": "z", "project_id": "p"},
                       "gce_destroy",
                       {"instance_name": "g", "zone": "z", "project_id": "p",
                        "deploy_job_id": "d"}),
        "oci_deploy": ({"instance_ocid": "ocid1.x"},
                       "oci_destroy",
                       {"instance_ocid": "ocid1.x", "deploy_job_id": "d"}),
    }
    for deploy_type, (meta, want_type, want_meta) in cases.items():
        got_type, got_meta = pol.build_destroy_metadata(deploy_type, meta, "d")
        assert got_type == want_type, deploy_type
        assert got_meta == want_meta, (deploy_type, got_meta)


def test_build_destroy_metadata_refuses_rather_than_guessing():
    """A key the deploy never recorded must not be substituted from current config. This
    is the case api/gcp.py calls "the worst version of this bug" — a destroy aimed at the
    wrong project."""
    refusals = [
        ("ec2_deploy",   {"instance_id": "i-1"},                      "region"),
        ("ec2_deploy",   {"region": "us-east-1"},                     "instance_id"),
        ("azure_deploy", {"vm_name": "az"},                           "resource_group"),
        ("gce_deploy",   {"instance_name": "g", "zone": "z"},         "project_id"),
        ("gce_deploy",   {"instance_name": "g", "project_id": "p"},   "zone"),
        ("oci_deploy",   {},                                          "instance_ocid"),
    ]
    for deploy_type, meta, missing in refusals:
        got_type, reason = pol.build_destroy_metadata(deploy_type, meta, "d")
        assert got_type is None, f"{deploy_type} missing {missing} was not refused"
        assert missing in reason, (deploy_type, reason)
        assert "manually" in reason, "the refusal should tell the operator what to do"
    # A blank string is as absent as a missing key.
    assert pol.build_destroy_metadata("ec2_deploy",
                                      {"instance_id": "", "region": "us-east-1"}, "d")[0] is None


def test_build_destroy_metadata_refuses_unreapable_deploy_types():
    for jt in ("proxmox_deploy", "nutanix_deploy", "ec2_bulk_deploy", "k8s_provision", ""):
        got_type, reason = pol.build_destroy_metadata(jt, {"instance_id": "i"}, "d")
        assert got_type is None and reason, jt


def test_deletion_active_needs_both_flags_and_its_own_delay():
    """Deletion's arming clock is what stops unchecking report-only from acting on a
    backlog nobody has reviewed."""
    _on(resource_expiry_enforce=1, resource_expiry_dry_run=0)
    now = NOW_TS
    assert pol.deletion_active(now - pol.ARM_DELAY_MINUTES * 60, now) is True
    assert pol.deletion_active(now - 60, now) is False            # just enabled
    assert pol.deletion_active(None, now) is False                # never observed
    _on(resource_expiry_enforce=1, resource_expiry_dry_run=1)
    assert pol.deletion_active(now - 10 * 3600, now) is False     # report-only wins
    _on(resource_expiry_enforce=0, resource_expiry_dry_run=0)
    assert pol.deletion_active(now - 10 * 3600, now) is False     # enforce off wins


def test_destroy_job_type_matches_what_each_endpoint_creates():
    """These four strings must equal the job_type the DELETE endpoints enqueue, or the
    reaper would create rows nothing claims."""
    assert pol.destroy_job_type("ec2_deploy") == "ec2_destroy"
    assert pol.destroy_job_type("azure_deploy") == "azure_destroy"
    assert pol.destroy_job_type("gce_deploy") == "gce_destroy"
    assert pol.destroy_job_type("oci_deploy") == "oci_destroy"
    for jt in ("proxmox_deploy", "nutanix_deploy", "ec2_bulk_deploy", "", "nonsense"):
        assert pol.destroy_job_type(jt) is None, jt
    # Every reapable VM type has a destroy, and nothing else does.
    assert set(pol._DESTROY_FOR) == set(pol.REAPABLE_VM_JOB_TYPES)


# ── ttl_capable: the UI/server shared predicate ──────────────────────────────

def test_ttl_capable_explains_itself():
    """The reason string is what the page shows on hover instead of the operator
    finding out from a 400, so every refusal must carry one."""
    assert pol.ttl_capable(_item()) == (True, "")
    for bad in (_item(kind="desktop"), _item(source="registered"),
                _item(cloud="proxmox")):
        ok, why = pol.ttl_capable(bad)
        assert ok is False and why, bad


# ── parse_ts ─────────────────────────────────────────────────────────────────

def test_parse_ts_reads_naive_iso_as_utc():
    """The DB stores naive datetime.utcnow(), so a bare ISO string is UTC. Reading it
    as local time would shift every expiry by the host's offset."""
    naive = pol.parse_ts("2026-07-28T12:00:00")
    explicit = pol.parse_ts("2026-07-28T12:00:00Z")
    assert naive == explicit == datetime(2026, 7, 28, 12, 0, 0).timestamp()
    # A datetime object round-trips identically to its ISO form.
    assert pol.parse_ts(datetime(2026, 7, 28, 12, 0, 0)) == naive
    # An offset-aware string is normalised, not rejected.
    assert pol.parse_ts("2026-07-28T14:00:00+02:00") == naive


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
