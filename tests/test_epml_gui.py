"""Invariants for the EPM-L GUI surfaces.

The EPM-L integration was API-only — no page called `/api/epml/*` — and the one
endpoint that mattered most had never run: it passed `description` and `owner_id` to
`create_job`, which takes neither, and omitted the required `created_by`, while the
service it called imported a module (`ansible_storage`) that does not exist. Both were
hard failures on every call.

These assertions pin the wiring that makes the three GUI surfaces work, all of it
checkable without a BeyondTrust tenant:

  * the sync job is queued for the runner, not run in-request, and is actually
    dispatchable — the same shape every other long job in this app has;
  * `create_job` is called with parameters it really has (the original bug);
  * `epml_service` reaches for `storage_service`, not the module that isn't there;
  * a presigned URL for a synced package is addressed by the key `upload_asset`
    wrote — guessing the prefix produces a URL that 404s only at build time;
  * the build pages and the run form send the fields the API reads.

Run: python tests/test_epml_gui.py   (or under pytest)
"""
import ast
import inspect
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


# ── the sync job: the two bugs, and the wiring ────────────────────────────────

def test_create_job_is_called_with_parameters_it_actually_has():
    """The original break: description/owner_id aren't parameters and created_by is
    required, so every call raised TypeError before any work started."""
    src = _read("web_dashboard", "api", "epml.py")
    call = re.search(r"job_service\.create_job\((.*?)\n    \)", src, re.S)
    assert call, "no create_job call found in api/epml.py"
    args = set(re.findall(r"(\w+)\s*=", call.group(1)))

    sig_src = _read("web_dashboard", "services", "job_service.py")
    params = set(re.findall(r"^\s*(\w+)\s*:",
                            re.search(r"def create_job\((.*?)\) -> Job:", sig_src, re.S).group(1),
                            re.M))
    unknown = args - params
    assert not unknown, f"create_job called with parameters it does not have: {unknown}"
    assert "created_by" in args, "create_job requires created_by"


def test_the_sync_service_uses_the_storage_module_that_exists():
    """`from . import ansible_storage` was an ImportError — the app has
    storage_service. Nothing catches it, so the job just died."""
    src = _read("web_dashboard", "services", "epml_service.py")
    assert "ansible_storage" not in src, (
        "epml_service still references ansible_storage, which does not exist")
    assert "storage_service" in src
    assert os.path.exists(os.path.join(_ROOT, "web_dashboard", "services", "storage_service.py"))


def test_sync_is_queued_for_the_runner_not_run_in_request():
    """In-request execution is what strands a long job when the worker recycles."""
    api = _read("web_dashboard", "api", "epml.py")
    assert "background_tasks" not in api, (
        "api/epml.py still dispatches the sync in-process — it must be queued")
    worker = _read("web_dashboard", "jobs_worker.py")
    handled = re.search(r"HANDLED_TYPES = \((.*?)\)", worker, re.S).group(1)
    assert '"epml_sync"' in handled, "epml_sync is not in HANDLED_TYPES"
    assert 'job_type == "epml_sync"' in worker, "no dispatch branch for epml_sync"
    assert "epml_sync_service" in worker


def test_the_sync_service_exposes_the_runner_entry_point():
    """Dispatch calls run(db, job_id=…, meta=…), matching the other job services."""
    src = _read("web_dashboard", "services", "epml_sync_service.py")
    fn = re.search(r"async def run\((.*?)\) -> None:", src, re.S)
    assert fn, "epml_sync_service has no run() entry point"
    for expected in ("db", "job_id", "meta"):
        assert expected in fn.group(1), f"run() is missing {expected}"


# ── presigning a synced package ───────────────────────────────────────────────

def test_asset_key_matches_where_upload_asset_writes():
    """storage_service.presigned_url takes a RAW key, but upload_asset writes under a
    per-backend prefix. If asset_key disagrees, the build gets a URL that 404s — and
    only at build time, on a cloud builder, where it's expensive to discover."""
    src = _read("web_dashboard", "services", "storage_service.py")
    # Every cloud backend's asset upload uses f"{<prefix>()}/{name}".
    for backend, prefix_fn in (("s3", "_s3_prefix"), ("azure_blob", "_azure_prefix"),
                               ("gcs", "_gcs_prefix")):
        assert f"{prefix_fn}()" in src, f"{prefix_fn} missing"
    mapping = re.search(r"_ASSET_PREFIX_FN = \{(.*?)\}", src, re.S).group(1)
    for backend, prefix_fn in (("s3", "_s3_prefix"), ("azure_blob", "_azure_prefix"),
                               ("gcs", "_gcs_prefix")):
        assert re.search(rf'"{backend}":\s*{prefix_fn}', mapping), (
            f"asset_key maps {backend} to the wrong prefix function")
    assert "local" not in mapping, (
        "local filesystem has no presignable key space and must not be mapped")


def test_asset_key_refuses_local_with_a_reason():
    """A cloud Packer builder can't reach the dashboard's disk either way — the error
    should say what to do, not just fail."""
    src = _read("web_dashboard", "services", "storage_service.py")
    body = re.search(r"def asset_key\(.*?\n(.*?)\nasync def ", src, re.S).group(1)
    assert "StorageError" in body
    assert "S3" in body or "cloud backend" in body, "the refusal should name the fix"


def test_package_source_helper_handles_both_sources():
    """'beyondtrust' keeps today's behavior; 'storage' presigns the synced copy.

    Lives in services/ with the rest of the build path — api/packer.py is the three
    enqueue-only route handlers now, and the worker is what runs this."""
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def _epml_package_url\(.*?\n(.*?)\nasync def ", src, re.S).group(1)
    assert "package_download_url" in body, "the BeyondTrust path is missing"
    assert "presigned_url" in body and "asset_key" in body, "the storage path is missing"
    assert 'source != "storage"' in body or 'source == "storage"' in body


def test_epml_error_is_imported_where_it_is_raised():
    """_epml_package_url raises EpmlError; an unimported name would be a NameError on
    the storage path only — the branch a test environment least often exercises."""
    src = _read("web_dashboard", "services", "packer_build_service.py")
    assert re.search(r"from \.epml_service import .*EpmlError", src), (
        "packer_build_service raises EpmlError without importing it")


# ── the UI sends what the API reads ───────────────────────────────────────────

def test_all_four_build_pages_send_the_package_source():
    for cloud in ("aws", "azure", "gcp", "oci"):
        page = _read("web_dashboard", "templates", cloud, "index.html")
        assert "bt_epml_source" in page, f"{cloud} build page has no package-source field"


def test_the_build_request_models_accept_the_package_source():
    src = _read("web_dashboard", "models", "packer.py")
    assert src.count("bt_epml_source") == 4, (
        "bt_epml_source must be on all four build request models")


def test_the_run_form_sends_the_token_variable_name():
    page = _read("web_dashboard", "templates", "config-mgmt", "index.html")
    assert "epml_token_var" in page, "the run form does not send epml_token_var"
    src = _read("web_dashboard", "api", "config_mgmt.py")
    assert "epml_token_var" in src, "RunRequest does not accept epml_token_var"


def test_the_token_is_minted_at_run_time_not_carried():
    """The var NAME travels; the token is fetched inside the run and appended to the
    scrub set. A token resolved anywhere earlier would be expired by then anyway.

    This lives in ``ansible_credentials`` rather than in one runner, so the guarantee now
    covers the agent-executed path as well: an agent's sealed run bundle gets a token minted
    at the moment it asks, through this same resolver, instead of a second copy of the logic
    that could cache one.
    """
    src = _read("web_dashboard", "services", "ansible_credentials.py")
    block = re.search(r"if epml_token_var:(.*?)\n    # Managed-account", src, re.S)
    assert block, "no epml_token_var handling in the shared credential resolver"
    body = block.group(1)
    assert "get_installation_token" in body, "the token is not minted at run time"
    assert "out.scrub.append" in body, "the token is not added to the scrub set"
    assert "out.extra_vars[epml_token_var]" in body
    # And the runner still asks for it, rather than having quietly dropped the field.
    runner = _read("web_dashboard", "services", "ansible_local_run_service.py")
    assert "epml_token_var=epml_token_var" in runner, (
        "ansible_local_run_service no longer passes epml_token_var to the resolver")


def test_storage_page_queues_the_sync_and_follows_the_job():
    page = _read("web_dashboard", "templates", "storage", "index.html")
    assert "/api/epml/sync-packages" in page, "the Storage page has no sync button"
    assert "/api/features" in page, "the EPM-L section is not gated on the feature flag"
    assert "epml.enabled" in page


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
