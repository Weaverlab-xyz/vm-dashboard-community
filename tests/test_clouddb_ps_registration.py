"""Password Safe onboarding as a CHOICE: at provision time and after the fact.

Password Safe onboarding for a cloud database existed, but there was no way to ask for
it. It ran, or didn't, purely off ``clouddb_ps_onboarding_enabled`` at the moment the
apply reached that line — so a database provisioned before the setting was configured
could never be onboarded at all, short of destroying and rebuilding it. The provision
form now carries a checkbox and the row carries a Register / Remove action, both landing
on the same machinery.

What is easy to get wrong here, and therefore what these pin:

- **the opt-in is TRI-state.** ``None`` means "the configured default", which is the
  behaviour every caller had before the field existed. Collapsing it to a bool would
  have every API client that omits the field silently opt out;
- **eligibility has one owner.** The button, the API pre-flight and the job must all ask
  ``_ps_ineligible_reason``. Three independent opinions is how a button ends up offering
  something the endpoint answers 400 to — the same arrangement, and the same reason, as
  ``_entitle_ineligible_reason``;
- **the ids reach the job metadata before the next remote call is made.** Everything used
  to be written in one go at the end, so a failure in the PRA Vault half left a real
  managed system in the customer's Password Safe with nothing recorded to remove it;
- **teardown clears only what it actually removed.** The standalone Remove action leaves
  the database in place, so a key left behind has the next decommission destroy something
  already gone, and a key cleared after a FAILED removal is an object nothing retries;
- **a referenced (operator-owned) functional account is never deleted** — the property
  ``test_clouddb_ps_functional_account`` pins for provisioning, re-checked on the path
  that now also runs on its own.

Plus the two static checks that keep the page honest: the row's action cell WRAPS (its
trailing Decommission used to run off the right edge of the card, exactly as the k8s
cluster row's Delete did), and every key the new buttons read is one ``_serialize``
actually supplies.

Runs under pytest, or standalone:  python tests/test_clouddb_ps_registration.py
"""
import ast
import asyncio
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "databases", "index.html")
_API = os.path.join(_ROOT, "web_dashboard", "api", "cloud_databases.py")
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "cloud_database_service.py")

CONF = {}
CALLS = []
JOB_LOGS = []
CERT_PATH = "/opt/bt/public_ssm.pem"
FAIL_ON_REGISTER = []      # names that register_managed_system should blow up on
FAIL_ON_DEREGISTER = []    # tf_state values that deregister should blow up on


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.ps_managed_system_id = None
        self.ps_managed_account_id = None
        self.__dict__.update(kw)


class _Job:
    id = None


class _FakeJobRow:
    def __init__(self, meta=None):
        self.id = "job-1"
        self.metadata_dict = dict(meta or {})


# ── fake Password Safe ────────────────────────────────────────────────────────

async def _fake_create_fa_on_platform(*, platform_id, account_name, display_name,
                                      password, description=""):
    CALLS.append(("create_fa", display_name))
    return 4242


async def _fake_get_platform_id(name):
    return 111


async def _fake_get_workgroup_id(name):
    return 5


async def _fake_get_functional_account(name):
    """reference mode looks the operator's account up by name. The name IS the contract
    (``<mode>:<dbLogin>``), so the fake echoes back whatever was configured."""
    # "ssm"/"azure" platform names so _platform_name_ok accepts either cloud's plugin.
    return {"id": 109, "account_name": name, "platform_id": 30,
            "platform_name": "psql SSM Custom Plugin" if name.startswith("IAM:")
            else "PostgreSQL Azure Run Command Plugin"}


async def _fake_delete_functional_account(fa_id):
    CALLS.append(("delete_fa", fa_id))
    if fa_id in FAIL_ON_DEREGISTER:
        raise RuntimeError(f"functional account {fa_id} is still referenced")


async def _fake_register_managed_system(**kw):
    CALLS.append(("register", kw["name"]))
    if kw["name"] in FAIL_ON_REGISTER:
        raise RuntimeError(f"boom on {kw['name']}")
    return {"tf_state_json": f'{{"for": "{kw["name"]}"}}',
            "managed_system_id": 1 if kw["name"].endswith("-db") else 9,
            "managed_account_id": 2 if kw["name"].endswith("-db") else 8}


async def _fake_deregister(state):
    CALLS.append(("deregister", state))
    if state in FAIL_ON_DEREGISTER:
        raise RuntimeError(f"boom on {state}")


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = _Job
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    ps = types.ModuleType("web_dashboard.services.ps_api_service")
    ps.create_functional_account_on_platform = _fake_create_fa_on_platform
    ps.get_functional_account = _fake_get_functional_account
    ps.get_platform_id = _fake_get_platform_id
    ps.get_workgroup_id = _fake_get_workgroup_id
    ps.delete_functional_account = _fake_delete_functional_account
    sys.modules["web_dashboard.services.ps_api_service"] = ps

    psr = types.ModuleType("web_dashboard.services.ps_resource_service")
    psr.register_managed_system = _fake_register_managed_system
    psr.deregister = _fake_deregister
    psr.PSResourceError = type("PSResourceError", (Exception,), {})
    sys.modules["web_dashboard.services.ps_resource_service"] = psr

    js = types.ModuleType("web_dashboard.services.job_service")
    js.update_progress = lambda *a, **k: None
    js.append_job_log = lambda _db, _job_id, msg: JOB_LOGS.append(msg)
    js.set_running = lambda *a, **k: None
    js.set_failed = lambda *a, **k: None
    js.set_completed = lambda *a, **k: None
    js.create_job = lambda *a, **k: None
    sys.modules["web_dashboard.services.job_service"] = js

    for name in ("terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, job_row):
        self._job = job_row
        self.commits = 0

    def query(self, _model):
        return _Query(self._job)

    def commit(self):
        self.commits += 1


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(**conf):
    CONF.clear()
    CALLS.clear()
    JOB_LOGS.clear()
    FAIL_ON_REGISTER.clear()
    FAIL_ON_DEREGISTER.clear()
    CONF.update(conf)


def _row(**kw):
    defaults = dict(id="db-abc12345", engine="postgres", provider="rds", cloud="aws",
                    region="us-east-2", instance_id="i-1", private_host="h.rds.aws",
                    port=5432, status="available", jump_item_id=None, db_name="appdb",
                    connect_db_name=None, entitle_integration_id=None,
                    source="provisioned", created_by="tester", created_at=None,
                    agent_id=None)
    defaults.update(kw)
    return _CloudDatabase(**defaults)


def _ctx(**kw):
    defaults = dict(managed_user="psafe_dbabc12345", managed_pw="pw", jump_host_id="i-jump",
                    region="us-east-2", db_name="appdb", admin_username="dbadmin",
                    client_image="postgres:16", port=5432,
                    # the Azure branch's two extra keys, filled by
                    # _create_db_managed_user_azure on the real path
                    jump_vm_name="clouddb-jumpoint", resource_group="rg")
    defaults.update(kw)
    return defaults


# ── the tri-state opt-in ──────────────────────────────────────────────────────

def test_an_absent_choice_means_the_configured_default():
    """The state every caller that predates the checkbox is in — the API without the
    field, a script, a test. Reading it as False would silently turn the feature off
    for all of them."""
    assert svc._ps_onboarding_opted({}) is True
    assert svc._ps_onboarding_opted({"db_id": "x"}) is True
    assert svc._ps_onboarding_opted(None) is True


def test_an_explicit_choice_is_honoured_both_ways():
    assert svc._ps_onboarding_opted({"register_in_passwordsafe": True}) is True
    assert svc._ps_onboarding_opted({"register_in_passwordsafe": False}) is False


def test_the_provision_form_always_sends_a_definite_answer():
    """A cleared checkbox has to travel as False. If the page omitted the key when
    unticked, the server would read "absent" as the default and onboard it anyway —
    the box would appear to do nothing in the one direction that matters."""
    html = open(_PAGE, encoding="utf-8").read()
    assert re.search(r"body\.register_in_passwordsafe\s*=\s*!!this\.form\.register_in_passwordsafe",
                     html), "submitProvision must always send the field, not only when ticked"
    assert "x-model=\"form.register_in_passwordsafe\"" in html, \
        "the provision modal has no Password Safe checkbox bound to the form"
    assert re.search(r"register_in_passwordsafe:\s*\{\{\s*clouddb_ps_onboarding_enabled", html), \
        ("the checkbox must START at the configured default — one seeded to false would "
         "silently opt every database out while the setting says on")


def test_the_checkbox_does_not_also_switch_off_the_legacy_credential_staging():
    """The box is labelled "managed system + rotatable managed account", and that is all
    it decides. Staging the admin credential as a functional account + Secrets Safe
    secret is a separate feature on its own ``pscli_*`` gate — folding it in here would
    have a deployment with onboarding OFF quietly lose the staging it has today, from a
    checkbox that never mentioned it."""
    src = ast.parse(open(_SVC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_provision_apply")
    branch = next(n for n in ast.walk(fn)
                  if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                  and n.test.id == "onboard_ctx")
    staged = [n for n in ast.walk(ast.Module(body=branch.orelse, type_ignores=[]))
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_store_ps_credentials"]
    assert staged, "the legacy staging fallback vanished from the else branch"
    gated = [n for n in branch.orelse
             if isinstance(n, ast.If) and "_ps_choice" in ast.dump(n.test)
             and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_store_ps_credentials"
                     for c in ast.walk(n))]
    assert not gated, "the per-database choice must not gate the legacy admin staging"


def test_a_failed_onboarding_fails_the_job_instead_of_going_green():
    """It shipped once as best-effort: Password Safe rejected the managed-system create
    ("The field 'IPAddress' is required.") and the job page said *completed* — the
    operator opted in, got no Password Safe artifacts, and only a mid-log line said so.
    Every except arm inside the ``if onboard_ctx:`` branch must re-raise (the outer
    handler marks the JOB failed), never append-and-continue."""
    src = ast.parse(open(_SVC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_provision_apply")
    branch = next(n for n in ast.walk(fn)
                  if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                  and n.test.id == "onboard_ctx")
    handlers = [h for t in ast.walk(ast.Module(body=branch.body, type_ignores=[]))
                if isinstance(t, ast.Try) for h in t.handlers]
    assert handlers, "the onboarding call lost its try/except"
    for handler in handlers:
        assert any(isinstance(n, ast.Raise) for n in ast.walk(handler)), \
            "an onboarding failure must propagate — a green job with no Password Safe " \
            "artifacts is the exact bug this pins"


# ── eligibility has one owner ─────────────────────────────────────────────────

def test_a_provisioned_aws_azure_or_gcp_database_is_onboardable():
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        assert svc._ps_ineligible_reason(_row(cloud=cloud)) is None


def test_a_registered_database_is_refused_and_told_why():
    _reset()
    reason = svc._ps_ineligible_reason(_row(source="registered"))
    assert reason and "Import from Password Safe" in reason


def test_a_cloud_with_no_plugin_transport_is_refused():
    """OCI has no SSM/Run Command/Data API equivalent into a private managed database,
    so there is nothing to onboard it WITH — a fact about the cloud, not a switch.
    (GCP used to be in this list; it now reaches Cloud SQL over Google's control plane.)"""
    _reset()
    for cloud in ("oci", "local"):
        reason = svc._ps_ineligible_reason(_row(cloud=cloud))
        assert reason, f"{cloud} has no plugin transport and must be refused"
        assert cloud.upper() in reason


def test_gcp_sql_server_is_eligible_now_that_cloud_run_exists():
    """It used to be a structural refusal: Cloud SQL for SQL Server has no IAM database
    authentication, so data-api would have needed the functional account's password
    mirrored into Secret Manager. The cloud-run channel removes that — the credential
    travels in the request body — so the question moved from "can this ever work" to
    "is the Cloud Run service deployed", which is configuration, not a fact about the
    row. _ps_ineligible_reason is documented as structural blockers only."""
    _reset()
    for engine in ("postgres", "mysql", "sqlserver"):
        assert svc._ps_ineligible_reason(_row(cloud="gcp", engine=engine)) is None, engine


def test_the_row_the_page_reads_carries_the_same_verdict():
    _reset()
    assert svc._serialize(_row())["ps_viable"] is True
    assert svc._serialize(_row(cloud="oci", engine="oracle"))["ps_viable"] is False
    assert svc._serialize(_row(cloud="gcp"))["ps_viable"] is True
    assert svc._serialize(_row(cloud="gcp", engine="sqlserver"))["ps_viable"] is True
    assert svc._serialize(_row())["ps_onboarded"] is False
    assert svc._serialize(_row(ps_managed_system_id="1"))["ps_onboarded"] is True


def test_the_api_preflight_uses_that_same_function():
    """Not a copy of the rules — the same call. A second opinion in the route is how the
    button and the endpoint drift apart."""
    api = open(_API, encoding="utf-8").read()
    assert "cloud_database_service._ps_ineligible_reason(row)" in api
    # The session is part of the call now: the GCP cloud-run gate is satisfied by a
    # DEPLOYED per-region DB-Ops service as well as by the config key, and that lookup
    # needs a session. A route that dropped it would silently answer "not enabled" for
    # every region the dashboard deployed into.
    assert "cloud_database_service._ps_db_onboarding_enabled(row, db)" in api
    assert "cloud_database_service.VALID_PS_DB_ACTIONS" in api


# ── the ids reach the metadata before the next remote call ────────────────────

def _onboard(row, job, cloud="aws"):
    return _run(svc._onboard_ps_managed_systems(
        _FakeDB(job), row=row, job_id=job.id, engine="postgres",
        tf_variables={"identifier": "clouddb-abc"}, ctx=_ctx()))


def test_the_db_managed_system_is_recorded_before_the_pra_vault_half_runs():
    """The failure this was written for: the PRA Vault half raised, the caller logged it
    as non-fatal, and a real managed system + functional account sat in the customer's
    Password Safe with nothing recorded to tear them down."""
    _reset(bt_api_host="pra.example.com",
           clouddb_ps_ssm_public_key_path=r"C:\Utils\public_ssm.pem")
    FAIL_ON_REGISTER.append("clouddb-abc-pravault")
    row, job = _row(), _FakeJobRow({"vault_account_name": "clouddb-abc-admin"})
    try:
        _onboard(row, job)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the PRA Vault failure should propagate to the caller")
    assert job.metadata_dict.get("ps_db_registration_tf_state"), \
        "the DB managed system must be recorded the moment it exists"
    assert job.metadata_dict.get("ps_db_functional_account_id") == 4242


def test_the_row_columns_mirror_the_stash_that_teardown_reads():
    _reset(clouddb_ps_ssm_public_key_path=r"C:\Utils\public_ssm.pem")
    row, job = _row(), _FakeJobRow()
    _onboard(row, job)
    assert row.ps_managed_system_id == "1"
    assert row.ps_managed_account_id == "2"
    assert bool(row.ps_managed_system_id) is bool(job.metadata_dict.get("ps_db_registration_tf_state"))


# ── teardown clears only what it removed ──────────────────────────────────────

def _full_meta():
    return {"ps_db_registration_tf_state": "db-state",
            "ps_db_functional_account_id": 4242,
            "ps_pravault_registration_tf_state": "pv-state",
            "ps_pravault_functional_account_id": 4243,
            "ps_db_managed_user": "psafe_dbabc12345",
            "ps_db_system_id": 1, "ps_db_account_id": 2,
            "name": "clouddb-abc"}


def _teardown(row, job):
    return _run(svc._teardown_ps_onboarding(
        _FakeDB(job), row=row, prov_job=job, progress_job_id="job-x"))


def test_a_clean_teardown_removes_every_key_and_clears_the_row():
    _reset()
    row, job = _row(ps_managed_system_id="1", ps_managed_account_id="2"), _FakeJobRow(_full_meta())
    assert _teardown(row, job) == []
    for key in ("ps_db_registration_tf_state", "ps_db_functional_account_id",
                "ps_pravault_registration_tf_state", "ps_pravault_functional_account_id",
                "ps_db_managed_user", "ps_db_system_id", "ps_db_account_id"):
        assert key not in job.metadata_dict, f"{key} survived a clean teardown"
    assert job.metadata_dict["name"] == "clouddb-abc", "unrelated metadata must be left alone"
    assert (row.ps_managed_system_id, row.ps_managed_account_id) == (None, None)


def test_a_failed_removal_keeps_its_key_so_something_can_retry_it():
    _reset()
    FAIL_ON_DEREGISTER.append("db-state")
    row, job = _row(ps_managed_system_id="1"), _FakeJobRow(_full_meta())
    errors = _teardown(row, job)
    assert errors and "DB managed system" in errors[0]
    assert job.metadata_dict.get("ps_db_registration_tf_state") == "db-state"
    # Its functional account is still referenced by that managed system, so it must not
    # even be attempted — the delete would fail and read as a second, unrelated fault.
    assert job.metadata_dict.get("ps_db_functional_account_id") == 4242
    assert ("delete_fa", 4242) not in CALLS
    # The PRA Vault half is independent and did come off.
    assert "ps_pravault_registration_tf_state" not in job.metadata_dict
    # The row still says onboarded, because in Password Safe it still is.
    assert row.ps_managed_system_id == "1"


def test_an_operator_owned_functional_account_is_never_deleted():
    """reference mode stashes the id under ``*_ref``, a key the teardown loop does not
    read. One account is shared by every database, so deleting it would take Password
    Safe onboarding down for all of them."""
    _reset()
    meta = _full_meta()
    del meta["ps_db_functional_account_id"]
    del meta["ps_pravault_functional_account_id"]
    meta["ps_db_functional_account_ref"] = 109
    meta["ps_pravault_functional_account_ref"] = 78
    row, job = _row(ps_managed_system_id="1"), _FakeJobRow(meta)
    assert _teardown(row, job) == []
    assert not any(c[0] == "delete_fa" for c in CALLS), \
        "a referenced functional account must survive teardown"
    # The ref keys go with the managed system they described — nothing remote to remove.
    assert "ps_db_functional_account_ref" not in job.metadata_dict


# -- the database-side prerequisites, on the clouds that actually have them ----
#
# The dashboard creates the managed user and cannot create the functional account's own
# DB login: that password lives in Password Safe, which never returns it. Azure and AWS
# are the two clouds where a REFERENCED account must carry such a login, and they are
# exactly the two that used to be told nothing -- the report shipped inside
# _create_db_managed_user_gcp. Live 2026-09-02, Azure postgres: Verify failed with
# "password authentication failed for user 'psfa_pg'" and no job line had ever named it.

def _ref_conf(**extra):
    conf = dict(clouddb_ps_functional_account_mode="reference",
                clouddb_ps_functional_account_postgres="IAM:psfa_pg",
                clouddb_ps_functional_account_azure_postgres="SP:psfa_pg",
                clouddb_ps_ssm_public_key_path=CERT_PATH)
    conf.update(extra)
    return conf


def test_aws_reference_mode_is_told_to_create_the_functional_accounts_login():
    _reset(**_ref_conf())
    _onboard(_row(cloud="aws"), _FakeJobRow())
    logs = " ".join(JOB_LOGS)
    assert "psfa_pg" in logs and "CREATE ROLE" in logs, JOB_LOGS
    assert "third ':'-segment" in logs, JOB_LOGS
    # and the grant that rotation needs on top of it
    assert "ADMIN OPTION" in logs, JOB_LOGS


def test_azure_reference_mode_is_told_the_same():
    _reset(**_ref_conf())
    _onboard(_row(cloud="azure"), _FakeJobRow())
    logs = " ".join(JOB_LOGS)
    assert "psfa_pg" in logs and "CREATE ROLE" in logs, JOB_LOGS
    assert "ADMIN OPTION" in logs, JOB_LOGS


def test_create_mode_is_told_nothing_because_its_account_is_the_minted_admin():
    _reset(clouddb_ps_ssm_public_key_path=CERT_PATH)
    _onboard(_row(cloud="azure"), _FakeJobRow())
    logs = " ".join(JOB_LOGS)
    assert "CREATE ROLE" not in logs and "ADMIN OPTION" not in logs, JOB_LOGS


def test_self_rotation_drops_the_grant_but_keeps_the_login():
    """Self-rotation means the managed account alters itself, so no grant is needed --
    but Verify Functional Account still signs in as the functional account's login."""
    _reset(**_ref_conf(clouddb_ps_self_rotation=True))
    _onboard(_row(cloud="azure"), _FakeJobRow())
    logs = " ".join(JOB_LOGS)
    assert "CREATE ROLE" in logs, JOB_LOGS
    assert "ADMIN OPTION" not in logs, JOB_LOGS


def test_nothing_recorded_means_nothing_attempted():
    _reset()
    row, job = _row(), _FakeJobRow({"name": "clouddb-abc"})
    assert _teardown(row, job) == []
    assert CALLS == []
    assert svc._has_ps_onboarding(job.metadata_dict) is False


# ── the page ──────────────────────────────────────────────────────────────────

def _action_cell() -> str:
    html = open(_PAGE, encoding="utf-8").read()
    start = html.index('<div class="flex flex-wrap justify-end items-center gap-1">')
    return html[start:html.index("</td>", start)]


def test_the_row_actions_wrap_instead_of_running_off_the_card():
    """The cell was `text-right whitespace-nowrap`, so with Connection + the Entitle pair
    the trailing Decommission was already past the right edge of a max-w-6xl card, with
    nothing to scroll. Adding the Password Safe actions would have pushed two more off.
    Same break, same fix, as the k8s cluster row (templates/k8s/index.html)."""
    cell = _action_cell()
    assert "whitespace-nowrap" not in cell, \
        "nowrap is what pushed the trailing action off the card"
    assert "flex-wrap" in cell
    # A margin on a button that has wrapped to a second line misaligns it; the flex gap
    # is what spaces them now.
    assert "ml-1" not in cell


def test_every_key_the_new_buttons_read_is_one_the_row_supplies():
    _reset()
    supplied = set(svc._serialize(_row()))
    read = set(re.findall(r"\bd\.([a-z_0-9]+)", _action_cell()))
    missing = read - supplied
    assert not missing, (
        f"the action cell reads d.{{{','.join(sorted(missing))}}}, which /api/databases "
        f"does not return — an undefined key is falsy, so those buttons never render")


def test_both_password_safe_actions_are_offered_and_gated_on_the_product_flag():
    cell = _action_cell()
    assert "psRegister(d)" in cell and "psDeregister(d)" in cell
    html = open(_PAGE, encoding="utf-8").read()
    # Inside the same {% if password_safe_enabled %} gate the Import button uses — a
    # Password-Safe-less deployment must not be shown an action it cannot complete.
    gate = html.index("{% if password_safe_enabled %}", html.index("psRegister") - 2000)
    assert gate < html.index("psRegister") < html.index("{% endif %}", gate)


def test_the_register_action_warns_that_the_tunnel_is_rebrokered():
    """Registering destroys the DB's PRA jump and brokers a new one against the managed
    user. Any open session drops, so the confirm has to say so."""
    html = open(_PAGE, encoding="utf-8").read()
    body = html[html.index("async psRegister(d)"):html.index("async psDeregister(d)")]
    assert "re-brokered" in body and "drop" in body


# ── the Azure jump VM's plugin prep ───────────────────────────────────────────
# Prep runs under `set -e` on a SHARED VM, so every command in it is a gate on the
# onboarding that triggered it. It used to install all three DB clients regardless of
# engine, which put an external Microsoft apt repo, a GPG key and an EULA on the
# critical path of a PostgreSQL registration — and aborted before the key material was
# staged when any of that stumbled.

def test_azure_prep_installs_only_this_engines_client():
    CONF.clear()
    pg = " ".join(svc._azure_jump_prep_commands("postgres"))
    assert "postgresql-client" in pg
    assert "mssql-tools18" not in pg and "mysql-client" not in pg
    ms = " ".join(svc._azure_jump_prep_commands("mysql"))
    assert "mysql-client" in ms and "postgresql-client" not in ms
    ss = " ".join(svc._azure_jump_prep_commands("sqlserver"))
    assert "mssql-tools18" in ss and "postgresql-client" not in ss


def test_azure_prep_without_an_engine_keeps_all_three():
    """The cloud-init head start bakes all three in; an unnamed engine matches it."""
    CONF.clear()
    every = " ".join(svc._azure_jump_prep_commands())
    for client in ("postgresql-client", "mysql-client", "mssql-tools18"):
        assert client in every


def test_azure_prep_still_stages_the_plugin_key_material():
    """The client install is the head start; the key drop is the part a rotation
    cannot do without. Narrowing the former must not drop the latter."""
    CONF.clear()
    CONF["clouddb_ps_azure_plugin_private_key"] = "-----BEGIN RSA PRIVATE KEY-----"
    CONF["clouddb_ps_azure_plugin_passphrase"] = "s3cret"
    cmds = " ".join(svc._azure_jump_prep_commands("postgres"))
    assert "/root/psplugin/private.pem" in cmds
    assert "/root/psplugin/passphrase.txt" in cmds


def test_run_detail_never_ends_at_the_colon():
    """A remote-command failure with no output still has to say something: the job
    detail page shows `error_message` and nothing else."""
    assert svc._run_detail({"stderr": "", "stdout": ""})
    assert svc._run_detail({"stderr": "", "stdout": "  boom  "}) == "boom"
    assert svc._run_detail({"stderr": "real cause", "stdout": "noise"}) == "real cause"


_TESTS = [(n, f) for n, f in sorted(globals().items())
          if n.startswith("test_") and callable(f)]

if __name__ == "__main__":
    failed = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    sys.exit(1 if failed else 0)
