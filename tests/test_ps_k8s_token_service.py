"""Behavioral tests for ps_k8s_token_service — the register/deregister orchestration
and the address builder.

The register ORDER is the correctness argument, so it is what these tests pin:

    RBAC → read current token → managed system → PRA Vault account →
    LINK (FATAL) → rotate → delete the legacy Secret (only if linked)

Both accounts are OFFERED the live token as their initial password, but the create API caps
Password at 128 characters and a bearer token is 800-1,200, so the seed is dropped and the
rotation is what actually populates the vault. That makes the rotation mandatory rather
than a nicety — `change_on_register=false` is overridden, with a warning, when the seed did
not take.

The link is what hands the sync to Password Safe, and it comes before the rotation on
purpose: a failure there has changed nothing in the cluster, whereas rotating first and
failing to link would leave PRA holding a value nothing will refresh. Completing with an
unlinked pair is the exact hole this feature closes, so an unconfirmed link fails the job.

Deleting the legacy ``<sa>-token`` Secret any earlier would destroy the credential the
PRA-brokered session is currently using; never deleting it leaves a cluster-admin token
the rotation plugin's label-scoped sweep will never touch.

The other half of that order is its recovery: the managed system is committed at step 2,
so a fatal failure at step 4 leaves a row that looks registered beside a pair that does
not sync. Re-running the register reconciles that link instead of returning early —
otherwise the RBAC re-apply reports success on a still-broken registration.

Every collaborator module is a stub injected into sys.modules (function-level imports
resolve at call time), so this runs with no app, DB or Password Safe.
Runs under pytest or standalone:  python tests/test_ps_k8s_token_service.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── module scaffolding ────────────────────────────────────────────────────────────

class _Col:
    """Stands in for a SQLAlchemy column in class-level comparisons."""
    def __eq__(self, other):  # noqa: D105
        return True
    def __hash__(self):
        return 0
    def isnot(self, other):
        return True
    def in_(self, other):
        return True


class _Meta(type):
    def __getattr__(cls, name):
        return _Col()


def _load():
    pkg_web = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg_web.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    pkg_svc = sys.modules.setdefault("web_dashboard.services",
                                     types.ModuleType("web_dashboard.services"))
    pkg_svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]

    sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
    _orm = types.ModuleType("sqlalchemy.orm")
    _orm.Session = object
    sys.modules.setdefault("sqlalchemy.orm", _orm)

    _dbm = types.ModuleType("web_dashboard.database")
    _dbm.Job = _Meta("Job", (), {})
    _dbm.K8sCluster = _Meta("K8sCluster", (), {})
    sys.modules["web_dashboard.database"] = _dbm

    settings_stub = types.ModuleType("web_dashboard.config")
    settings_stub.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = settings_stub

    path = os.path.join(_ROOT, "web_dashboard", "services", "ps_k8s_token_service.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.ps_k8s_token_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, _dbm


svc, _dbm = _load()


class FakeDB:
    """query(Model).filter(...).first() → the canned row for that model."""
    def __init__(self, rows):
        self.rows = rows          # model-name → object
        self.commits = 0

    def query(self, model):
        db = self

        class _Q:
            def filter(self, *a, **kw):
                return self
            def first(self):
                return db.rows.get(getattr(model, "__name__", str(model)))
        return _Q()

    def commit(self):
        self.commits += 1


class Recorder:
    def __init__(self):
        self.events = []

    def hit(self, name):
        self.events.append(name)

    def index(self, name):
        return self.events.index(name)


# A realistic ServiceAccount bearer token: a JWT of ~900 characters. This fixture used to
# be a 43-character stub, and that is precisely why the suite passed while registration
# failed live — the create API refuses a Password over 128 characters, and no real token is
# ever short enough to notice.
_LONG_TOKEN = ("eyJhbGciOiJSUzI1NiIsImtpZCI6InN2Yy1hY2N0LXNpZ25pbmcta2V5In0."
               + "eyJhdWQiOlsiaHR0cHM6Ly9rdWJlcm5ldGVzLmRlZmF1bHQuc3ZjIl0s" * 12
               + ".c2lnbmF0dXJlLWJ5dGVz" * 8)

# What the real ps_resource_service enforces (_MAX_SEED_PASSWORD_LEN). Duplicated because
# that module is stubbed out entirely here; the stub mirrors its contract, cap included.
_MAX_SEED = 128


def _row(**kw):
    base = dict(id="c-1", cloud="gcp", name="gke-demo", region="us-central1",
                api_server="https://1.2.3.4", deploy_job_id="job-1",
                ps_token_account_id=None, ps_pra_vault_account_id=None,
                pra_vault_account_id="77", status="registered")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _install_stubs(rec, *, cfg=None, link_fails=False, link_confirmed=True,
                   linked_now=True, checkout_value="tok"):
    """Inject stub collaborator modules and return (config_store, modules)."""
    store = {
        "k8s_ps_token_rotation_enabled": "1",
        "k8s_ps_functional_account_gcp": "psafe-rotator@p.iam.gserviceaccount.com",
        "k8s_ps_pravault_functional_account": "pra-config-api",
        "k8s_ps_workgroup": "55",
        "bt_api_host": "pra.example.com",
        "gcp_project_id": "proj-1",
    }
    store.update(cfg or {})

    m_cfg = types.ModuleType("web_dashboard.services.config_service")
    m_cfg.get = lambda key, workgroup=None: store.get(key, "")
    m_cfg.get_bool = lambda key, default=False: (
        store[key] not in ("0", "", "false") if key in store else default)
    m_cfg.set = lambda key, value, workgroup=None: store.__setitem__(key, value)
    m_cfg.delete = lambda key, workgroup=None: store.pop(key, None)

    m_k8s = types.ModuleType("web_dashboard.services.k8s_service")
    m_k8s.resolve_kubeconfig = lambda db, cid: "kubeconfig-yaml"

    async def _resolve(db, row, kubeconfig):
        rec.hit("read_current_token")
        return _LONG_TOKEN, "minted"
    m_k8s._resolve_pra_sa_token = _resolve

    async def _rbac(db, cid, *, mode, subject_name=""):
        rec.hit("rbac")
        return "rotator RBAC applied (User x)"
    m_k8s.apply_ps_rotator_rbac = _rbac

    async def _rbac_rm(db, cid, *, mode):
        rec.hit("rbac_removed")
    m_k8s.remove_ps_rotator_rbac = _rbac_rm

    async def _del_secret(db, cid):
        rec.hit("delete_legacy_secret")
    m_k8s.delete_legacy_pra_token_secret = _del_secret

    m_ps = types.ModuleType("web_dashboard.services.ps_api_service")
    m_ps.configured = lambda: True

    async def _fa(name):
        return {"id": 88, "platform_id": 1008, "platform_name": "does not matter here",
                "account_name": name}
    m_ps.get_functional_account = _fa

    async def _pid(name):
        return 1008
    m_ps.get_platform_id = _pid

    async def _wg(name):
        return "55"
    m_ps.get_workgroup_id = _wg

    async def _change(account_id):
        rec.hit("rotate")
    m_ps.change_managed_account_password = _change

    async def _link(*, parent_account_id, synced_account_id,
                    expect_subscriber_platform=""):
        rec.hit("link")
        rec.events.append(("linked", parent_account_id, synced_account_id))
        if link_fails:
            raise RuntimeError("POST ManagedAccounts/201/SyncedAccounts/202 failed (403)")
        return {"linked": True, "confirmed": link_confirmed}
    m_ps.link_synced_account = _link

    async def _unlink(*, parent_account_id, synced_account_id):
        rec.hit("unlink")
        return True
    m_ps.unlink_synced_account = _unlink

    async def _sync_status(*, parent_account_id, synced_account_id):
        rec.hit("sync_status")
        return {"linked": linked_now, "parent_exists": True,
                "parent_last_change": "2026-08-11T08:00:00Z",
                "subscriber_last_change": "2026-08-11T09:00:00Z",
                "subscriber_count": 1 if linked_now else 0}
    m_ps.synced_account_status = _sync_status

    async def _checkout(account_id, *, duration_min=15, reason=""):
        rec.hit("checkout")
        return checkout_value
    m_ps.checkout_credential = _checkout

    m_res = types.ModuleType("web_dashboard.services.ps_resource_service")
    m_res._validate_k8ssa_dns_name = lambda addr: None
    _n = {"count": 0}

    async def _register(**kw):
        rec.hit("register_managed_system")
        rec.events.append(("registered", kw))
        _n["count"] += 1
        seed = kw.get("initial_password") or ""
        return {"managed_system_id": str(100 + _n["count"]),
                "managed_account_id": str(200 + _n["count"]),
                "tf_state_json": json.dumps({"n": _n["count"]}),
                # The real service drops an over-long seed for a placeholder and says so,
                # because sending it would 400 the apply. False for any real token.
                "initial_password_seeded": bool(seed) and len(seed) <= _MAX_SEED}
    m_res.register_managed_system = _register

    async def _deregister(tf_state_json):
        rec.hit(("deregistered", json.loads(tf_state_json)["n"]))
    m_res.deregister = _deregister

    m_hook = types.ModuleType("web_dashboard.services.ps_vm_hook")
    m_hook._platform_name_ok = lambda pname, *tokens: True

    m_job = types.ModuleType("web_dashboard.services.job_service")
    m_job.set_running = lambda db, jid: rec.hit("job_running")
    m_job.set_failed = lambda db, jid, msg: rec.events.append(("job_failed", msg))
    m_job.set_completed = lambda db, jid, result=None: rec.events.append(
        ("job_completed", result))

    pkg_api = types.ModuleType("web_dashboard.api")
    pkg_api.__path__ = []
    m_ws = types.ModuleType("web_dashboard.api.websocket")

    async def _bp(job_id, pct, msg, log_line=None):
        pass
    m_ws.broadcast_progress = _bp

    mods = {
        "web_dashboard.services.config_service": m_cfg,
        "web_dashboard.services.k8s_service": m_k8s,
        "web_dashboard.services.ps_api_service": m_ps,
        "web_dashboard.services.ps_resource_service": m_res,
        "web_dashboard.services.ps_vm_hook": m_hook,
        "web_dashboard.services.job_service": m_job,
        "web_dashboard.api": pkg_api,
        "web_dashboard.api.websocket": m_ws,
    }
    for name, mod in mods.items():
        sys.modules[name] = mod
    return store


def _prior_state(*, rotated=None, seeded=False):
    """The per-cluster state key a previous registration left behind, as a cfg override.

    ``rotated`` omitted is the half-state that matters: step 2 committed the account id and
    the run died before step 5, so nothing ever replaced the create-time placeholder."""
    st = {"system_id": "101", "address": "gke;proj-1;us-central1;k8s-gke-demo",
          "seeded": seeded}
    if rotated is not None:
        st["rotated"] = rotated
    return {"ps_k8s_token_c-1": json.dumps(st)}


def _fake_job(meta):
    return types.SimpleNamespace(metadata_dict=meta)


# ── the address builder ───────────────────────────────────────────────────────────

def test_build_address_per_cloud():
    a = svc.build_address(cloud="aws", region="us-east-1", cluster_name="prod",
                          mode="longlived")
    assert a == "eks;us-east-1;prod;longlived"
    a = svc.build_address(cloud="azure", subscription_id="sub", resource_group="rg",
                          cluster_name="c1", mode="longlived")
    assert a == "aks;sub;rg;c1;longlived"
    a = svc.build_address(cloud="gcp", project_id="p", location="us-central1-a",
                          cluster_name="c1", mode="bound", ttl_seconds=43200)
    assert a == "gke;p;us-central1-a;c1;bound;ttl=43200"
    a = svc.build_address(cloud="local", api_server="https://api.internal:6443",
                          mode="longlived", namespace="pra-access")
    assert a == "k8s;https://api.internal:6443;longlived;ns=pra-access"


def test_build_address_oci_takes_the_generic_path():
    # The plugin has no OCI provider; OKE rides k8s;<apiServerUrl>.
    a = svc.build_address(cloud="oci", api_server="https://oke.example:6443",
                          mode="longlived")
    assert a.startswith("k8s;https://oke.example:6443")


def test_build_address_clamps_the_bound_ttl_to_the_api_server_floor():
    a = svc.build_address(cloud="aws", region="r", cluster_name="c", mode="bound",
                          ttl_seconds=60)
    assert ";ttl=600" in a, "the API server silently caps below 600 — clamp, don't pass through"


def test_build_address_names_the_missing_part():
    try:
        svc.build_address(cloud="azure", subscription_id="sub", cluster_name="c1")
    except svc.PSK8sTokenError as exc:
        assert "resource group" in str(exc)
    else:
        raise AssertionError("a missing AKS resource group must be named")
    try:
        svc.build_address(cloud="local", api_server="")
    except svc.PSK8sTokenError as exc:
        assert "API server" in str(exc)
    else:
        raise AssertionError("a missing generic api_server must be named")


def test_build_address_appends_extra_options_verbatim():
    a = svc.build_address(cloud="gcp", project_id="p", location="l", cluster_name="c",
                          mode="longlived", extra_options="dnsEndpoint=true;serverName=x")
    assert a.endswith(";dnsEndpoint=true;serverName=x")


def test_build_address_rejects_an_unknown_mode():
    try:
        svc.build_address(cloud="aws", region="r", cluster_name="c", mode="forever")
    except svc.PSK8sTokenError:
        pass
    else:
        raise AssertionError("an unknown mode must be rejected")


# ── register: the order is the contract ─────────────────────────────────────────

def _run_register(rec, row, **kw):
    db = FakeDB({"K8sCluster": row,
                 "Job": _fake_job({"tf_variables": {
                     "project": "proj-1", "region": "us-central1",
                     "cluster_name": "k8s-gke-demo"}})})
    return asyncio.run(svc.register(db, "c-1", **kw)), db


def test_register_runs_the_steps_in_the_safe_order():
    rec = Recorder()
    _install_stubs(rec)
    row = _row()
    out, db = _run_register(rec, row)

    assert rec.index("rbac") < rec.index("read_current_token")
    assert rec.index("read_current_token") < rec.index("register_managed_system")
    assert rec.index("link") < rec.index("rotate"), (
        "linking after the rotation would leave PRA holding a value nothing refreshes "
        "if the link then failed — and in LongLived mode that value was just revoked")
    assert rec.index("rotate") < rec.index("delete_legacy_secret"), (
        "the legacy Secret is the credential PRA is USING — deleting it before the "
        "rotation has produced a Password-Safe-issued token breaks brokered sessions")
    assert out["rotated"] and out["pra_synced"] and out["legacy_secret_deleted"]
    assert row.ps_token_account_id == "201"
    assert row.ps_pra_vault_account_id == "202"


def test_register_makes_the_token_account_the_parent_of_the_pair():
    """Direction. Swapping the two ids links successfully and syncs BACKWARDS — the PRA
    Vault account's value would be pushed onto the cluster's token account."""
    rec = Recorder()
    _install_stubs(rec)
    _run_register(rec, _row())
    linked = [e for e in rec.events if isinstance(e, tuple) and e[0] == "linked"]
    assert linked == [("linked", 201, 202)], (
        f"expected parent=201 (the ServiceAccount token), subscriber=202 (the PRA Vault "
        f"copy); got {linked}")


def test_register_offers_the_live_token_as_the_seed_to_both_accounts():
    """Offering it still matters — it is free, and it is what makes the account correct on
    any path whose limit is higher. What must NOT happen is the caller assuming it took."""
    rec = Recorder()
    _install_stubs(rec)
    _run_register(rec, _row())
    seeded = [kw for _e, kw in [ev for ev in rec.events if isinstance(ev, tuple)
                                and ev[0] == "registered"]]
    assert len(seeded) == 2, "the token account and the PRA Vault subscriber"
    assert all(kw["initial_password"] == _LONG_TOKEN for kw in seeded)


def test_register_rotates_even_when_change_on_register_is_off_if_the_seed_was_dropped():
    """The seed cannot be taken for a bearer token (the create API caps Password at 128),
    so the rotation is the only thing that puts a real credential in the vault. Honouring
    change_on_register=false would leave a registration that reads as complete while
    current_token serves the placeholder to the PRA tunnel."""
    rec = Recorder()
    _install_stubs(rec)
    out, _db = _run_register(rec, _row(), change_on_register=False)

    assert out["rotated"], "a dropped seed makes the rotation mandatory, not optional"
    assert "rotate" in rec.events
    assert any("could not be seeded" in w for w in out["warnings"]), (
        f"overriding the caller's change_on_register must be reported; got {out['warnings']}")


def test_register_honours_change_on_register_off_when_the_seed_did_take():
    """The override is scoped to the reason for it. A credential Password Safe actually
    holds must not be rotated behind the caller's back — for LongLived mode that would mint
    and revoke Secrets in the cluster nobody asked it to touch."""
    rec = Recorder()
    _install_stubs(rec)
    # A seedable credential: short enough for the create API to accept.
    mods = sys.modules["web_dashboard.services.k8s_service"]

    async def _short(db, row, kubeconfig):
        rec.hit("read_current_token")
        return "short-enough-to-seed", "minted"
    mods._resolve_pra_sa_token = _short

    out, _db = _run_register(rec, _row(), change_on_register=False)
    assert not out["rotated"], "nothing forced a rotation here"
    assert "rotate" not in rec.events
    assert not any("could not be seeded" in w for w in out["warnings"])


def test_a_failed_link_fails_register_before_anything_rotates():
    rec = Recorder()
    _install_stubs(rec, link_fails=True)
    try:
        _run_register(rec, _row())
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "a managed-but-unsynced token is the exact hole this feature closes — "
            "the register must fail loudly")
    assert "rotate" not in rec.events, (
        "the link comes first precisely so a failure leaves the cluster's token alone")
    assert "delete_legacy_secret" not in rec.events


def test_an_unconfirmed_link_fails_register():
    """Password Safe returned 200 but the subscriber is not in the parent's list. That
    is indistinguishable from success at the call site, which is why it is re-read."""
    rec = Recorder()
    _install_stubs(rec, link_confirmed=False)
    try:
        _run_register(rec, _row())
    except svc.PSK8sTokenError as exc:
        assert "synced list" in str(exc)
    else:
        raise AssertionError("an unconfirmed link must fail the register")
    assert "rotate" not in rec.events and "delete_legacy_secret" not in rec.events


def test_no_pra_vault_account_means_no_link_and_the_secret_stays():
    rec = Recorder()
    _install_stubs(rec)
    row = _row(pra_vault_account_id=None)
    out, _db = _run_register(rec, row)
    assert "link" not in rec.events
    assert "delete_legacy_secret" not in rec.events, (
        "with nothing syncing to PRA the legacy Secret is what keeps the tunnel alive")
    assert out["pra_synced"] is False
    assert any("PRA Vault account" in w for w in out["warnings"])


def test_register_is_idempotent_for_an_already_registered_cluster():
    rec = Recorder()
    _install_stubs(rec, cfg=_prior_state(rotated=True))
    out, _db = _run_register(rec, _row(ps_token_account_id="201"))
    assert out["already_registered"] is True
    assert "register_managed_system" not in rec.events
    assert "rbac" in rec.events, "re-applying the (idempotent) RBAC is the useful half"
    assert "rotate" not in rec.events, (
        "the previous run rotated, so the vault holds a real credential — re-registering "
        "must not mint and revoke Secrets in the cluster to re-prove that")


# ── register: the already-registered path reconciles a missing link ──────────────
#
# ps_token_account_id is committed at step 2 and the link is step 4, so a failure at the
# (fatal) link leaves a row that LOOKS registered next to a pair that does not sync.

def test_register_relinks_an_already_registered_pair_that_is_unlinked():
    """The recovery path for a link that failed after the column was committed — most
    likely a 403, whose fix is tenant-side and then wants a retry. Returning early here
    would re-apply the RBAC and report success on a pair that still never reaches PRA,
    leaving deregister-then-register (two managed systems destroyed and rebuilt) as the
    only way to re-create one missing reference."""
    rec = Recorder()
    _install_stubs(rec, linked_now=False)
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    out, _db = _run_register(rec, row)

    assert out["already_registered"] is True
    assert rec.index("sync_status") < rec.index("link"), (
        "read before linking — the read needs only Read permission, and a POST against a "
        "link that is already live is not documented as idempotent")
    assert out["relinked"] is True and out["pra_synced"] is True
    linked = [e for e in rec.events if isinstance(e, tuple) and e[0] == "linked"]
    assert linked == [("linked", 201, 202)], (
        f"the repair must keep the direction of the original link: parent=201 (the "
        f"ServiceAccount token), subscriber=202 (the PRA Vault copy). A swapped pair links "
        f"successfully and syncs BACKWARDS; got {linked}")
    assert "register_managed_system" not in rec.events, (
        "both managed accounts already exist — the repair creates nothing")


def test_re_register_replaces_the_placeholder_a_dead_run_left_in_the_vault():
    """The other half of that same half-state. Because the seed is always dropped, a run
    that died at the (fatal) link left the account holding its create-time placeholder —
    and current_token serves whatever is in the vault to the PRA tunnel. Reconciling only
    the link would repair the plumbing around a credential that authenticates to nothing."""
    rec = Recorder()
    _install_stubs(rec, cfg=_prior_state())        # no "rotated" — the run never got there
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    out, _db = _run_register(rec, row)

    assert out["already_registered"] is True
    assert out["rotated"] is True and "rotate" in rec.events, (
        "nothing has ever put a real credential in this account — neither a seed nor a "
        "completed rotation — so the re-register has to fill it")
    assert any("placeholder" in w for w in out["warnings"]), (
        f"a silently-repaired credential is indistinguishable from one that was fine all "
        f"along; got {out['warnings']}")


def test_re_register_does_not_rotate_when_the_first_run_seeded_a_real_credential():
    # The signal is "does the vault hold a real credential", which is seeded OR rotated —
    # not rotated alone. A short credential that WAS seeded and deliberately not rotated
    # (change_on_register=false) must survive a re-register untouched.
    rec = Recorder()
    _install_stubs(rec, cfg=_prior_state(seeded=True, rotated=False))
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    out, _db = _run_register(rec, row)
    assert out["rotated"] is False and "rotate" not in rec.events
    assert "rotate" not in rec.events, (
        "the repair restores the missing reference and stops; rotating would revoke the "
        "token PRA is currently using for no reason")


def test_register_does_not_relink_a_pair_that_is_already_linked():
    """The healthy re-register. Password Safe already owns the sync, so the only work is
    the RBAC — one status read and no write."""
    rec = Recorder()
    _install_stubs(rec, cfg=_prior_state(rotated=True))   # linked_now=True
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    out, _db = _run_register(rec, row)

    assert "sync_status" in rec.events
    assert "link" not in rec.events, "re-POSTing a live link is a write nothing needs"
    assert "rotate" not in rec.events, "nor is re-rotating a credential already in the vault"
    assert out["pra_synced"] is True and out["relinked"] is False
    assert "rbac" in rec.events


def test_an_unconfirmed_relink_fails_the_register():
    """Same rule as the first link: Password Safe can 200 a POST that did not take, so the
    subscriber list is re-read — and a repair that cannot be confirmed must not be
    reported as one."""
    rec = Recorder()
    _install_stubs(rec, linked_now=False, link_confirmed=False)
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    try:
        _run_register(rec, row)
    except svc.PSK8sTokenError as exc:
        assert "synced list" in str(exc)
    else:
        raise AssertionError("an unconfirmed relink must fail the job")


def test_an_already_registered_cluster_with_no_pra_vault_account_says_so():
    """Nothing to link to, and a re-register cannot create one — the PRA Vault account is
    only registered on a first registration. Say that rather than implying the re-run
    repaired something."""
    rec = Recorder()
    _install_stubs(rec, linked_now=False)
    out, _db = _run_register(rec, _row(ps_token_account_id="201",
                                       ps_pra_vault_account_id=None))
    assert "sync_status" not in rec.events and "link" not in rec.events
    assert out["pra_synced"] is False and out["relinked"] is False
    assert any("Remove the Password Safe registration" in w for w in out["warnings"])


def test_a_failed_sync_status_read_leaves_the_link_alone():
    """A transient read failure must not fail an otherwise fine re-register, and must not
    be answered by blind-POSTing a link onto a pair that may already be live."""
    rec = Recorder()
    _install_stubs(rec, linked_now=False)

    async def _boom(*, parent_account_id, synced_account_id):
        rec.hit("sync_status")
        raise RuntimeError("GET ManagedAccounts/201/SyncedAccounts failed (500)")
    sys.modules["web_dashboard.services.ps_api_service"].synced_account_status = _boom

    out, _db = _run_register(rec, _row(ps_token_account_id="201",
                                       ps_pra_vault_account_id="202"))
    assert "link" not in rec.events
    assert out["already_registered"] is True and out["relinked"] is False
    assert any("could not read the synced-account state" in w for w in out["warnings"])


def test_register_respects_the_master_gate():
    rec = Recorder()
    _install_stubs(rec, cfg={"k8s_ps_token_rotation_enabled": "0"})
    try:
        _run_register(rec, _row())
    except svc.PSK8sTokenError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("the master gate must refuse")


def test_current_token_requires_a_registration():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row(ps_token_account_id=None)})
    try:
        asyncio.run(svc.current_token(db, "c-1"))
    except svc.PSK8sTokenError:
        pass
    else:
        raise AssertionError("current_token without a registration must refuse")
    db = FakeDB({"K8sCluster": _row(ps_token_account_id="201")})
    tok = asyncio.run(svc.current_token(db, "c-1"))
    assert tok == "tok" and "checkout" in rec.events


# ── deregister ───────────────────────────────────────────────────────────────────

def test_deregister_unlinks_then_destroys_in_reverse_registration_order():
    rec = Recorder()
    store = _install_stubs(rec)
    row = _row(ps_token_account_id="201", ps_pra_vault_account_id="202")
    db = FakeDB({"K8sCluster": row})
    store["ps_k8s_token_c-1"] = json.dumps(
        {"tf_state": json.dumps({"n": 1}), "pravault_tf_state": json.dumps({"n": 2}),
         "token_mode": "longlived"})
    store["k8s_token_sync_c-1"] = json.dumps({"synced_change": "x"})

    out = asyncio.run(svc.deregister(db, "c-1"))
    assert out["removed"] is True and not out["errors"]
    assert rec.index("unlink") < rec.index(("deregistered", 2)), (
        "destroying a subscriber while the link is live can leave Password Safe syncing "
        "into an account that no longer exists")
    dereg = [e for e in rec.events if isinstance(e, tuple) and e[0] == "deregistered"]
    assert dereg == [("deregistered", 2), ("deregistered", 1)], (
        "the PRA Vault account references the functional account — destroy in reverse "
        "registration order, the same rule the cloud-DB teardown follows")
    assert row.ps_token_account_id is None and row.ps_pra_vault_account_id is None
    assert "ps_k8s_token_c-1" not in store, "the registration state must be dropped"
    assert "k8s_token_sync_c-1" not in store, (
        "orphan rows from the v26.7.7 poll-and-push sync must still be cleaned up")
    assert "rbac_removed" in rec.events


# ── rotate_now ───────────────────────────────────────────────────────────────────

def test_rotate_now_does_not_push_anything_itself():
    """One call to Password Safe. The synced link means PRA is updated by the same
    change, so there is no second half for the dashboard to drive."""
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row(ps_token_account_id="201",
                                    ps_pra_vault_account_id="202")})
    out = asyncio.run(svc.rotate_now(db, "c-1"))
    assert out["rotated"] is True and out["pra_synced"] is True
    assert "rotate" in rec.events
    assert "checkout" not in rec.events, "rotate_now must never handle the credential"


def test_rotate_now_warns_when_the_pair_is_no_longer_synced():
    """An admin can unlink in the Password Safe console. The rotation still succeeds and
    still revokes the old token — it just never reaches PRA, which is worth saying in
    the job result rather than leaving to a broken tunnel to reveal."""
    rec = Recorder()
    _install_stubs(rec, linked_now=False)
    db = FakeDB({"K8sCluster": _row(ps_token_account_id="201",
                                    ps_pra_vault_account_id="202")})
    out = asyncio.run(svc.rotate_now(db, "c-1"))
    assert out["rotated"] is True and out["pra_synced"] is False
    assert "NOT synced" in out["note"]


def test_deregister_without_a_registration_is_a_noop():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row()})
    out = asyncio.run(svc.deregister(db, "c-1"))
    assert out == {"ok": True, "removed": False}


# ── the worker entry owns its terminal state ─────────────────────────────────────

def test_run_fails_the_job_instead_of_raising():
    rec = Recorder()
    _install_stubs(rec, link_fails=True)
    db = FakeDB({"K8sCluster": _row(),
                 "Job": _fake_job({"tf_variables": {"project": "p", "region": "r",
                                                    "cluster_name": "c"}})})
    asyncio.run(svc.run(db, cluster_id="c-1", job_id="j-1", action="register"))
    assert any(isinstance(e, tuple) and e[0] == "job_failed" for e in rec.events)


def test_run_rejects_an_unknown_action():
    rec = Recorder()
    _install_stubs(rec)
    db = FakeDB({"K8sCluster": _row()})
    asyncio.run(svc.run(db, cluster_id="c-1", job_id="j-1", action="explode"))
    failed = [e for e in rec.events if isinstance(e, tuple) and e[0] == "job_failed"]
    assert failed and "explode" in failed[0][1]


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
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
