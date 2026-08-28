"""GCP Secret Manager's project, and what an unset one used to look like.

A `clouddb_adapter_pair` job on a GCP MySQL database died at 15% ("Staging the admin
credential…") with:

    400 Invalid resource field value in the request. [reason: "RESOURCE_PROJECT_INVALID"
    ... value: "google.cloud.secretmanager.v1.SecretManagerServiceService.CreateSecret"]

Nothing about Secret Manager was broken. `_gcp_cfg` read the project from
`secrets_gcp_project` — a field that exists only on the Secrets page — while reading
the CREDENTIAL from `gcp_service_account_json`, the main GCP one. On an install set up
through Setup → GCP, the credential was present and the project was "", so the parent
was the string "projects/" and Google rejected it. The 400 named the service, so the
error read as a Secret Manager outage rather than an empty text box.

Two halves are pinned here:
  * the project falls back to the main GCP config, as `_aws_cfg`'s region already did
  * an unset project raises an actionable ValueError instead of reaching the SDK

Plus the adapter-pairing side: the job detail view shows `error_message` and nothing
else, so the staging step has to name itself.
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

CONF = {}


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, as the sibling suites do: gating on whether google-cloud-
# secret-manager happens to be installed is how a suite passes locally and fails in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace(gcp_project_id=""))

from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402


def _read(path):
    return open(path, encoding="utf-8").read()


def _with(**cfg):
    CONF.clear()
    CONF.update(cfg)


# -- The project resolution ----------------------------------------------------

def test_the_secrets_page_field_still_wins():
    """A deliberately separate secrets project is the reason the field exists; the
    fallback must not override an operator who set it."""
    _with(secrets_gcp_project="secrets-proj", gcp_project_id="main-proj")
    assert sbs._gcp_cfg()[0] == "secrets-proj"


def test_an_unset_secrets_project_falls_back_to_the_main_gcp_config():
    """The exact shape of the reported failure: Setup → GCP filled in, Secrets → GCP
    left blank."""
    _with(gcp_project_id="main-proj")
    assert sbs._gcp_cfg()[0] == "main-proj"


def test_the_fallback_order_matches_every_other_gcp_caller():
    """`_cfg("gcp_project") or _cfg("gcp_project_id")` is the spelling used in
    cloud_database_service, cloud_function_service and entitle_db_proxy_service. A
    different order here would resolve a different project than the database was
    provisioned into."""
    _with(gcp_project="preferred", gcp_project_id="legacy")
    assert sbs._gcp_cfg()[0] == "preferred"


def test_the_prefix_default_survives_the_rewrite():
    """Secret ids are built from it, so a lost default renames every secret."""
    _with(gcp_project_id="p")
    assert sbs._gcp_cfg()[1] == "dashboard"


# -- The guard -----------------------------------------------------------------

def test_no_project_anywhere_raises_before_the_sdk_is_reached():
    _with()
    try:
        sbs._gcp_project_or_raise()
    except ValueError as exc:
        # Actionable: names both places the project can be set.
        assert "Setup" in str(exc) and "Secret Manager" in str(exc)
    else:
        raise AssertionError("an empty project reached the SDK")


def test_every_path_that_builds_a_projects_parent_is_guarded():
    """The 400 is identical whichever call sends "projects/" — write, list and delete
    are all reachable from the Secrets page, and ephemeral write from an Ansible run."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "secrets_backend_service.py"))
    for fn in ("def write_gcp_sm(", "def write_gcp_sm_ephemeral(",
               "def list_gcp_sm_ephemeral(", "def list_gcp_sm(",
               "def delete_gcp_sm(", "def test_gcp_sm("):
        body = src.split(fn)[1].split("\ndef ")[0]
        assert "_gcp_project_or_raise()" in body, fn


def test_the_test_button_and_the_write_path_share_one_message():
    """test_gcp_sm carried its own copy of the guidance. Two spellings of the same
    remedy drift, and the Test button is where an operator checks the fix."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "secrets_backend_service.py"))
    assert src.count("GCP project is not configured") == 1


# -- The pairing job's error text ----------------------------------------------

def test_staging_failures_name_the_stage_and_the_backend():
    """jobs' failed detail is error_message ONLY — a raw protobuf dump there tells the
    operator nothing about which of the pairing's three stages died."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "cloud_db_adapter_service.py"))
    body = src.split("def _stage_admin_secret(")[1].split("\ndef ")[0]
    assert "except Exception" in body
    assert "AdapterPairingError(" in body
    assert "_BACKEND_LABEL" in body
    # The backend key is an internal token; the message has to name the page.
    assert '"gcp_sm": "GCP Secret Manager"' in src


def test_every_secret_backend_the_pairing_uses_has_a_label():
    """A missing label degrades to the raw key, which is not a place an operator can
    navigate to. Keyed off _SECRET_BACKEND so a fourth cloud cannot skip it."""
    from web_dashboard.services import cloud_db_adapter_service as svc

    for backend in svc._SECRET_BACKEND.values():
        assert backend in svc._BACKEND_LABEL, backend


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
