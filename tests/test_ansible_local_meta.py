"""Invariants for ansible_local run metadata — what makes a VM run durable.

A VM Config-Management run used to execute as a FastAPI BackgroundTask with its
parameters held only in the task closure. Nothing survived a worker restart, and a
bulk run that queued 50 of them left the un-started ones `pending` forever:
reconcile_stale_jobs ignores `pending` by design, and no runner claimed
`ansible_local`. Persisting the parameters is what lets the durable runner resume a
run, so these assertions pin the two things that would break it:

  * the round-trip. If a key written by the endpoint isn't read back under the same
    name, the resumed run silently loses that argument — a missing `become` source or
    managed account would change how the run authenticates, not just fail loudly.
  * the closed allowlist. Job metadata is written to the database, so only refs, ids
    and non-secret values may appear. A field added later must trip this test rather
    than quietly carrying a credential to disk.

Plus a one-line guard that `ansible_local` is actually in the runner's HANDLED_TYPES
— the single line that makes the whole thing work, and reverts it silently if lost.

Pure module, stdlib only. Runs under pytest, or standalone:
    python tests/test_ansible_local_meta.py
"""
import importlib.util
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ansible_run_meta.py")
_spec = importlib.util.spec_from_file_location("ansible_run_meta", _PATH)
arm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arm)


def _payload(**over):
    """A RunRequest-shaped object. run_meta reads with getattr, so a namespace works
    exactly like the pydantic model does."""
    base = dict(asset="patch.yml", target="10.0.0.5", cloud="aws",
                ansible_user="ec2-user", extra_vars={"pkg": "nginx"},
                secret_vars={"db_pw": "bt_safe://prod/db"},
                secret_become_source="cfg:become_key",
                secret_ssh_key_source="bt_safe://prod/key",
                managed_account=None, managed_become=None,
                asset_backend="s3")
    base.update(over)
    return types.SimpleNamespace(**base)


# ── the allowlist ─────────────────────────────────────────────────────────────

def test_metadata_keys_are_a_closed_set():
    """The boundary that keeps credentials out of the database. A new field must
    fail here and be considered, not ride along silently."""
    meta = arm.run_meta(_payload(), description="d", asset_backend="s3")
    assert set(meta) == set(arm.RUN_META_KEYS) | {"description"}


def test_no_credential_shaped_key_is_persisted():
    """Refs and ids only. `*_source` names a secret; `*_password`/`*_key` would BE
    one — the distinction this module exists to hold."""
    banned = re.compile(r"(password|passwd|private_key|ssh_key$|token|credential)", re.I)
    offenders = [k for k in arm.RUN_META_KEYS if banned.search(k)]
    assert not offenders, f"credential-shaped key in the metadata allowlist: {offenders}"


# ── the round-trip ────────────────────────────────────────────────────────────

def test_round_trip_preserves_every_field():
    p = _payload()
    kwargs = arm.run_kwargs(arm.run_meta(p, description="d", asset_backend="s3"))
    for key in arm.RUN_META_KEYS:
        if key == "asset_backend":
            continue                       # resolved by the endpoint, asserted below
        assert kwargs[key] == getattr(p, key), f"{key} did not survive the round-trip"


def test_run_kwargs_match_the_run_job_signature():
    """The worker calls _run_job(job_id, **run_kwargs(meta)), so a name that drifts
    from the parameter list is a TypeError at dispatch — in a background worker,
    where nobody is watching."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "ansible_local_run_service.py")).read()
    sig = re.search(r"async def _run_job\((.*?)\) -> None:", src, re.S).group(1)
    params = set(re.findall(r"^\s*(\w+)\s*[:=]", sig, re.M)) - {"job_id"}
    assert set(arm.RUN_META_KEYS) == params, (
        f"metadata keys and _run_job parameters disagree: "
        f"only in meta={set(arm.RUN_META_KEYS) - params}, only in signature={params - set(arm.RUN_META_KEYS)}")


def test_asset_backend_comes_from_the_endpoint_not_the_payload():
    """The endpoint resolves it against the active storage backend before creating
    the job; persisting the unresolved payload value would resume against the wrong
    backend."""
    meta = arm.run_meta(_payload(asset_backend=""), description="d", asset_backend="gcs")
    assert meta["asset_backend"] == "gcs"
    assert arm.run_kwargs(meta)["asset_backend"] == "gcs"


def test_managed_account_is_flattened_to_a_plain_dict():
    """A pydantic sub-model must be dumped, or the metadata isn't JSON-serializable."""
    class _Ref:
        def model_dump(self):
            return {"system_id": 7, "account_id": 2,
                    "account_name": "svc-ansible", "uses_ssh_key": True}
    meta = arm.run_meta(_payload(managed_account=_Ref()), description="d", asset_backend="s3")
    assert meta["managed_account"] == {"system_id": 7, "account_id": 2,
                                       "account_name": "svc-ansible", "uses_ssh_key": True}


# ── defaults ──────────────────────────────────────────────────────────────────

def test_missing_keys_fall_back_rather_than_raising():
    """A job queued by an older build predates some of these. Resuming it the way
    that build would have beats refusing to resume it."""
    kwargs = arm.run_kwargs({"asset": "p.yml", "target": "10.0.0.9"})
    assert kwargs["asset"] == "p.yml"
    assert kwargs["secret_vars"] is None
    assert kwargs["managed_account"] is None
    assert kwargs["extra_vars"] == {}


def test_empty_meta_is_survivable():
    assert arm.run_kwargs({})["asset"] == ""
    assert arm.run_kwargs(None)["extra_vars"] == {}


def test_extra_vars_defaults_to_a_dict_not_none():
    """_run_job passes extra_vars to config_drift.inputs_hash unguarded."""
    assert arm.run_kwargs({})["extra_vars"] == {}


def test_default_dict_is_not_shared_between_reconstructions():
    """Handing back the module-level default would let one run's mutation poison
    every later one."""
    a = arm.run_kwargs({})
    a["extra_vars"]["leaked"] = True
    assert arm.run_kwargs({})["extra_vars"] == {}


# ── the runner actually claims it ─────────────────────────────────────────────

def test_ansible_local_is_dispatched_by_the_job_runner():
    """Without this, jobs sit `pending` forever — reconcile_stale_jobs skips pending
    on purpose, so nothing would ever fail them either."""
    src = open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")).read()
    handled = re.search(r"HANDLED_TYPES = \((.*?)\)", src, re.S).group(1)
    assert '"ansible_local"' in handled, "ansible_local is not in HANDLED_TYPES"
    assert 'job_type == "ansible_local"' in src, "no dispatch branch for ansible_local"


def test_the_runner_reaches_the_run_path_through_services():
    """The Ansible execution path lives in services/, so the worker doesn't reach into
    the API package for it — an api import works but inverts the dependency, and it's
    the kind of shortcut that quietly returns under time pressure.

    Scoped to config_mgmt on purpose: `jobs_worker` still imports `api.packer` and
    `api.images` for the image-build job types. Those have the same shape and the same
    fix available, but they predate this and asserting a repo-wide rule the codebase
    doesn't hold would just be a failing test nobody trusts."""
    src = open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")).read()
    assert "from .api.config_mgmt import" not in src, (
        "jobs_worker imports the run path from the api package")
    assert "ansible_local_run_service" in src, (
        "jobs_worker should dispatch ansible_local through the service")


def test_run_execution_is_not_defined_in_the_api_module():
    """_run_job and its helpers belong to the service. Defining them in the request
    module is what forced the worker's backwards import in the first place."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py")).read()
    for name in ("_run_job", "_dispatch_cloud_runner", "_resolve_managed_ref",
                 "_resolve_cloud_ssh_key", "_delete_ephemeral"):
        assert f"def {name}(" not in src, f"{name} is still defined in api/config_mgmt.py"


def test_endpoint_no_longer_dispatches_in_process():
    """The BackgroundTask path is what stranded these jobs; if it comes back the
    durability guarantee is gone even with the metadata in place."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py")).read()
    assert "background_tasks.add_task" not in src, (
        "config_mgmt still dispatches a run in-process — it must be queued for the runner")


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
