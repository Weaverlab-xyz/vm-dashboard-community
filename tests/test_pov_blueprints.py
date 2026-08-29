"""POV blueprints, and the build job's two orphan rules.

A blueprint is a saved POV recipe: the eleven fields the create form asks, under one name.
What is pinned here is the part that decides whether it stays a convenience or becomes a
second way to provision a POV:

  * **The request wins field by field.** A blueprint supplies defaults for fields the
    request left blank and nothing else. An SE who typed a broker VM name before picking a
    recipe meant it.
  * **A blueprint cannot store a selection the provision would reject.** Tenant ids go
    through the same `validate_selection` the create endpoint uses, so a bad id is a form
    error rather than a job failure minutes later.
  * **Nothing on a blueprint is a secret.** The Gateway deploy key and the Password Safe
    installer key are per-tenant, and a recipe that carried them would spread two secrets
    across every blueprint an SE ever saved.
  * **The pre-fill never overwrites.** A POV that already carries a Gateway name keeps it.

And the build job's own rules, which mirror the POV provision's:

  * **The scratch environment id is committed before anything else can fail.** An
    environment on the platform and not in this database is the one failure nothing can
    clean up automatically — and a scratch one bills until somebody notices.
  * **A failed prepare does not fail the build,** and its reason never lands in
    `error_message`, which means "this build is broken".

Runs against SQLite in a temp file with a stubbed platform adapter. No network, no app.

Runs under pytest, or standalone:
    python tests/test_pov_blueprints.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-blueprints")

# A temp database, set BEFORE web_dashboard.database is imported: the suite shares a real
# vm_cli.db and the developer's .env, and a test that writes rows into it mutates the dev
# install. Same trap test_skytap_verify documents for config rows.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from web_dashboard import database as db_mod  # noqa: E402
from web_dashboard.database import (Base, PovBlueprint, PovEnvironment,  # noqa: E402
                                    PovTemplateBuild)
from web_dashboard.services import pov_blueprint_service as bp  # noqa: E402
from web_dashboard.services import pov_template_builder as tb  # noqa: E402

Base.metadata.create_all(bind=db_mod.engine)


def _session():
    return db_mod.SessionLocal()


def _clean(db):
    for model in (PovBlueprint, PovTemplateBuild, PovEnvironment):
        db.query(model).delete()
    db.commit()


class _Payload:
    """Stands in for a ProvisionRequest: apply_to only reads attributes off it."""

    def __init__(self, **kw):
        defaults = {"template_id": "", "project_id": "", "broker_vm_name": "",
                    "suspend_on_idle_seconds": 0, "pra_tenant_id": "",
                    "ps_tenant_id": "", "entitle_tenant_id": ""}
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _blueprint(db, **kw):
    kw.setdefault("platform", "skytap")
    kw.setdefault("name", "ps-eval")
    kw.setdefault("template_id", "tpl-1")
    return bp.create(db, **kw)


# ── names and validation ─────────────────────────────────────────────────────

def test_a_name_must_be_a_slug():
    for bad in ("", "A", "Has Spaces", "-leading", "x" * 64, "under_score"):
        try:
            bp.normalize_name(bad)
        except bp.BlueprintError:
            continue
        raise AssertionError(f"{bad!r} should not be a valid blueprint name")
    assert bp.normalize_name("  PS-Eval  ") == "ps-eval"


def test_a_duplicate_name_is_refused():
    db = _session()
    try:
        _clean(db)
        _blueprint(db)
        try:
            _blueprint(db)
        except bp.BlueprintError as exc:
            assert "already exists" in str(exc), exc
        else:
            raise AssertionError("a duplicate blueprint name must be refused")
    finally:
        _clean(db)
        db.close()


def test_a_blueprint_needs_a_template():
    db = _session()
    try:
        _clean(db)
        try:
            _blueprint(db, template_id="")
        except bp.BlueprintError as exc:
            assert "template id" in str(exc), exc
        else:
            raise AssertionError("a blueprint with no template must be refused")
    finally:
        _clean(db)
        db.close()


def test_a_tenant_id_that_does_not_exist_is_refused_at_save_time():
    """Not at provision time. A bad id that surfaces inside a job has already created an
    environment, and correcting a dropdown would mean destroying it first."""
    db = _session()
    try:
        _clean(db)
        try:
            _blueprint(db, pra_tenant_id="no-such-tenant")
        except bp.BlueprintError:
            pass
        else:
            raise AssertionError("an unknown tenant id must be refused")
    finally:
        _clean(db)
        db.close()


# ── apply_to: the request wins ───────────────────────────────────────────────

def test_a_blueprint_fills_only_what_the_request_left_blank():
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, template_id="tpl-1", broker_vm_name="bastion",
                         project_id="proj-9", suspend_on_idle_seconds=1800)
        payload = _Payload(broker_vm_name="my-broker")
        out = bp.apply_to(row, payload)
        # Blank fields are filled…
        assert out["template_id"] == "tpl-1", out
        assert out["project_id"] == "proj-9", out
        assert out["suspend_on_idle_seconds"] == 1800, out
        # …and the one the operator typed is not touched.
        assert "broker_vm_name" not in out, out
    finally:
        _clean(db)
        db.close()


def test_a_blueprint_never_supplies_a_name_or_a_workgroup():
    """A POV's name is per-POV by definition, and its workgroup decides RBAC and the expiry
    exempt list — not something a saved recipe should set silently."""
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, workgroup="team-a")
        out = bp.apply_to(row, _Payload())
        assert "name" not in out, out
        assert "workgroup" not in out, out
    finally:
        _clean(db)
        db.close()


def test_a_zero_idle_timeout_is_treated_as_unset():
    """0 is a real answer ("disable it") but it is also the form's default, so it cannot be
    told from unset — the blueprint fills it."""
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, suspend_on_idle_seconds=3600)
        out = bp.apply_to(row, _Payload(suspend_on_idle_seconds=0))
        assert out["suspend_on_idle_seconds"] == 3600, out
    finally:
        _clean(db)
        db.close()


def test_the_prefill_is_a_no_op_without_a_blueprint():
    """The provision calls it unconditionally, so `None` has to be the quiet case rather
    than a guard every caller has to remember."""
    assert bp.prefill_environment(None, None, None) == []


# ── the pre-fill ─────────────────────────────────────────────────────────────

def test_the_prefill_sets_a_gateway_name_and_resource_broker_staging():
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, gateway_name="pov-gw", rb_windows_vm_name="rb-host",
                         rb_zone="default", rb_asset="bootstrapper.exe")
        env = PovEnvironment(platform="skytap", name="acme", status="provisioning")
        db.add(env)
        db.commit()

        filled = bp.prefill_environment(db, row, env)
        assert "Gateway name" in filled, filled
        assert env.gateway_name == "pov-gw", env.gateway_name

        from web_dashboard.services import pov_resource_broker as rb
        assert rb.stored_rb_vm_name(env) == "rb-host"
        assert rb.zone(env) == "default"
        assert rb.asset(env) == "bootstrapper.exe"
    finally:
        _clean(db)
        db.close()


def test_the_prefill_never_overwrites_what_the_pov_already_carries():
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, gateway_name="from-blueprint",
                         rb_windows_vm_name="from-blueprint")
        env = PovEnvironment(platform="skytap", name="acme", status="provisioning",
                             gateway_name="chosen-by-hand")
        db.add(env)
        db.commit()
        from web_dashboard.services import pov_resource_broker as rb
        rb.configure(db, env, vm_name="chosen-by-hand")

        bp.prefill_environment(db, row, env)
        assert env.gateway_name == "chosen-by-hand", env.gateway_name
        assert rb.stored_rb_vm_name(env) == "chosen-by-hand"
    finally:
        _clean(db)
        db.close()


def test_a_blueprint_carries_no_secret_field():
    """The Gateway deploy key and the Password Safe installer key are per-tenant. A recipe
    that carried them would spread two secrets across every blueprint ever saved."""
    columns = {c.name for c in PovBlueprint.__table__.columns}
    for forbidden in ("deploy_key", "installer_key", "client_secret", "secret",
                      "api_token", "password"):
        assert not any(forbidden in c for c in columns), \
            f"PovBlueprint has a {forbidden!r}-shaped column: {sorted(columns)}"


# ── update and delete ────────────────────────────────────────────────────────

def test_update_touches_only_the_keys_present():
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, broker_vm_name="bastion", description="first")
        bp.update(db, row, {"description": "second"})
        assert row.description == "second", row.description
        assert row.broker_vm_name == "bastion", row.broker_vm_name
    finally:
        _clean(db)
        db.close()


def test_an_empty_string_clears_a_blankable_field_rather_than_storing_it():
    """A field storing "" would read as set-to-empty everywhere downstream, which is a
    different thing from not set."""
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, broker_vm_name="bastion")
        bp.update(db, row, {"broker_vm_name": ""})
        assert row.broker_vm_name is None, row.broker_vm_name
    finally:
        _clean(db)
        db.close()


def test_renaming_onto_another_blueprints_name_is_refused():
    db = _session()
    try:
        _clean(db)
        _blueprint(db, name="ps-eval")
        second = _blueprint(db, name="pra-demo")
        try:
            bp.update(db, second, {"name": "ps-eval"})
        except bp.BlueprintError as exc:
            assert "already exists" in str(exc), exc
        else:
            raise AssertionError("renaming onto a taken name must be refused")
    finally:
        _clean(db)
        db.close()


def test_a_rename_to_its_own_name_is_allowed():
    db = _session()
    try:
        _clean(db)
        row = _blueprint(db, name="ps-eval")
        bp.update(db, row, {"name": "ps-eval", "description": "edited"})
        assert row.description == "edited", row.description
    finally:
        _clean(db)
        db.close()


# ── the build job's orphan rules ─────────────────────────────────────────────

class _StubAdapter:
    """The adapter surface run_template_build uses."""

    def __init__(self, *, vms=None, fail_after_create=False):
        self.vms = vms if vms is not None else [
            {"id": "vm-1", "name": "broker", "os_family": "linux",
             "interfaces": [{"id": "nic-1", "ip": "10.0.0.5",
                             "network_type": "automatic", "services": []}]},
            {"id": "vm-2", "name": "app", "os_family": "linux",
             "interfaces": [{"id": "nic-2", "ip": "10.0.0.6",
                             "network_type": "automatic", "services": []}]},
        ]
        self.fail_after_create = fail_after_create
        self.deleted = []

    async def create_environment(self, template_id, name="", project_id=""):
        return {"id": "env-99", "name": name}

    async def update_environment(self, env_id, changes):
        return {"id": env_id}

    async def set_runstate(self, env_id, runstate):
        if self.fail_after_create:
            raise RuntimeError("the power-on failed")
        return {"id": env_id, "runstate": runstate}

    async def wait_for_runstate(self, env_id, target, **kw):
        return {"id": env_id, "runstate": target}

    async def get_environment(self, env_id):
        return {"id": env_id, "vms": self.vms}

    async def create_template(self, env_id, name, description=""):
        return {"id": "tpl-new", "name": name}

    async def delete_environment(self, env_id):
        self.deleted.append(env_id)


def _run_build(adapter, *, install_runner=True, prepare=None, keep=False):
    db = _session()
    _clean(db)
    build = PovTemplateBuild(platform="skytap", name="saas-base",
                             base_template_id="tpl-1", broker_vm_name="broker",
                             keep_build_environment=keep, status=tb.STATUS_BUILDING)
    db.add(build)
    db.commit()
    build_id = build.id
    db.close()

    saved_adapter, saved_prepare = tb._adapter, tb.prepare_broker_vm
    tb._adapter = lambda _b: adapter
    if prepare is not None:
        tb.prepare_broker_vm = prepare
    try:
        from web_dashboard.services import job_service
        db2 = _session()
        job = job_service.create_job(db2, job_type="pov_template_build",
                                     created_by="tester",
                                     metadata={"build_id": build_id,
                                               "install_runner": install_runner})
        job_id = job.id
        db2.close()
        asyncio.run(tb.run_template_build(
            job_id, {"build_id": build_id, "install_runner": install_runner}))
    finally:
        tb._adapter, tb.prepare_broker_vm = saved_adapter, saved_prepare

    db3 = _session()
    row = db3.query(PovTemplateBuild).filter(PovTemplateBuild.id == build_id).first()
    out = tb.serialize(row)
    db3.close()
    return out


def test_a_successful_build_bakes_a_template_and_reaps_the_environment():
    async def _prepare(mod, env_id, vm):
        return "runner installed over SSH"

    adapter = _StubAdapter()
    out = _run_build(adapter, prepare=_prepare)
    assert out["status"] == tb.STATUS_READY, out
    assert out["result_template_id"] == "tpl-new", out
    assert out["prepare_method"] == "ssh", out
    assert adapter.deleted == ["env-99"], adapter.deleted
    assert out["build_environment_id"] == "", out
    # The row still says which environment produced it, long after that environment is gone.
    assert out["build_environment_was"] == "env-99", out


def test_a_failure_after_create_keeps_the_environment_id():
    """Failing WITHOUT the id is how an orphan is made — and a scratch environment nobody
    knows about bills until somebody notices."""
    out = _run_build(_StubAdapter(fail_after_create=True))
    assert out["status"] == tb.STATUS_FAILED, out
    assert out["build_environment_id"] == "env-99", out
    assert "Discard" in out["error_message"], out["error_message"]


def test_a_failed_prepare_does_not_fail_the_build():
    """A template that bakes without the runner is still usable — the operator pastes the
    script in, which is what they do today. Failing would throw away a correct template."""
    async def _prepare(mod, env_id, vm):
        raise tb.TemplateBuildError("no route to the published port")

    out = _run_build(_StubAdapter(), prepare=_prepare)
    assert out["status"] == tb.STATUS_READY, out
    assert out["result_template_id"] == "tpl-new", out
    assert out["prepare_method"] == "failed", out
    assert "no route" in out["prepare_detail"], out
    # `error_message` means "this build is broken". A missing runner is not that.
    assert out["error_message"] == "", out


def test_a_template_that_fails_the_contract_is_never_baked():
    """Baking it would produce a template that cannot run a POV at all."""
    adapter = _StubAdapter(vms=[
        {"id": "vm-2", "name": "app", "os_family": "linux",
         "interfaces": [{"id": "nic-2", "network_type": "automatic", "services": []}]}])
    out = _run_build(adapter)
    assert out["status"] == tb.STATUS_FAILED, out
    assert out["result_template_id"] == "", out
    assert any(r["status"] == tb.CHECK_FAIL for r in out["contract_report"]), out
    # The environment is kept so Discard can reap it.
    assert out["build_environment_id"] == "env-99", out


def test_opting_out_of_the_runner_install_is_recorded_as_skipped_not_failed():
    out = _run_build(_StubAdapter(), install_runner=False)
    assert out["status"] == tb.STATUS_READY, out
    assert out["prepare_method"] == "skipped", out
    assert "by hand" in out["prepare_detail"], out


def test_keeping_the_build_environment_leaves_it_running():
    async def _prepare(mod, env_id, vm):
        return "installed"

    adapter = _StubAdapter()
    out = _run_build(adapter, prepare=_prepare, keep=True)
    assert out["status"] == tb.STATUS_READY, out
    assert adapter.deleted == [], adapter.deleted
    assert out["build_environment_id"] == "env-99", out


# ── the blueprint reaches the provision endpoint ─────────────────────────────

def _provision_app(templates):
    """The real POV router on a bare app, following tests/test_pov_api. No `main`, so no
    setup middleware and no feature gate in the way."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import pov as pov_api
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db

    app = FastAPI()
    app.include_router(pov_api.router)

    class _User:
        username = "tester"
        is_admin = True

    def _db():
        db = _session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[get_db] = _db

    saved = pov_api._adapter

    class _Mod:
        @staticmethod
        async def list_templates():
            return templates

        @staticmethod
        def configured_project_id():
            return ""

    pov_api._adapter = lambda platform: ("skytap", _Mod)
    return TestClient(app, raise_server_exceptions=False), saved


def test_a_blueprint_supplies_the_template_the_request_omitted():
    """The whole point of the dropdown: a POV needs a name and nothing else."""
    db = _session()
    _clean(db)
    row = _blueprint(db, template_id="tpl-1", broker_vm_name="bastion",
                     project_id="proj-9", suspend_on_idle_seconds=1800)
    bp_id = row.id
    db.close()

    client, saved = _provision_app([{"id": "tpl-1", "name": "SaaS base"}])
    try:
        res = client.post("/api/pov/managed",
                          json={"name": "acme-pov", "blueprint_id": bp_id})
        assert res.status_code == 202, (res.status_code, res.text)
        body = res.json()
        assert body["blueprint"] == "ps-eval", body
        assert body["environment"]["template_id"] == "tpl-1", body
        assert body["environment"]["template_name"] == "SaaS base", body
        assert body["environment"]["project_id"] == "proj-9", body
    finally:
        from web_dashboard.api import pov as pov_api
        pov_api._adapter = saved
        db = _session(); _clean(db); db.close()


def test_the_request_beats_the_blueprint_through_the_endpoint():
    db = _session()
    _clean(db)
    row = _blueprint(db, template_id="tpl-1")
    bp_id = row.id
    db.close()

    client, saved = _provision_app([{"id": "tpl-1", "name": "SaaS base"},
                                    {"id": "tpl-2", "name": "Other"}])
    try:
        res = client.post("/api/pov/managed",
                          json={"name": "acme-pov", "blueprint_id": bp_id,
                                "template_id": "tpl-2"})
        assert res.status_code == 202, (res.status_code, res.text)
        assert res.json()["environment"]["template_id"] == "tpl-2", res.text
    finally:
        from web_dashboard.api import pov as pov_api
        pov_api._adapter = saved
        db = _session(); _clean(db); db.close()


def test_no_template_and_no_blueprint_is_a_400_not_a_500():
    """`template_id` stopped being required when blueprints landed. It still has to be
    present one way or the other, and saying so is the endpoint's job."""
    client, saved = _provision_app([])
    try:
        res = client.post("/api/pov/managed", json={"name": "acme-pov"})
        assert res.status_code == 400, (res.status_code, res.text)
        assert "template id is required" in res.text, res.text
    finally:
        from web_dashboard.api import pov as pov_api
        pov_api._adapter = saved


def test_an_unknown_blueprint_id_is_a_400():
    client, saved = _provision_app([{"id": "tpl-1", "name": "SaaS base"}])
    try:
        res = client.post("/api/pov/managed",
                          json={"name": "acme-pov", "blueprint_id": "nope"})
        assert res.status_code == 400, (res.status_code, res.text)
    finally:
        from web_dashboard.api import pov as pov_api
        pov_api._adapter = saved


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
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass
    sys.exit(1 if failures else 0)
