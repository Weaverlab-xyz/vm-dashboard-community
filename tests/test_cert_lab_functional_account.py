"""Certificate Lab: the dashboard mints the functional account from the CA build.

The Certificate plugin needs one functional account carrying TWO credentials, split on
the LAST colon: username ``<ca-account>:<bi-run-as-user>``, password
``<ca-secret>:<bi-api-key>``. That account used to be an operator's manual step, which
was the wrong shape for a reason that is easy to lose:

- **the CA half exists for one moment only.** It comes back in the apply's outputs — a
  GCP service account key, an AWS secret access key — both of which their APIs return
  exactly once. By the time somebody opens BeyondInsight the only copies left are the
  remote terraform state and the dict that is about to go out of scope;
- **the manual path hits a wall the automated one does not.** ``ps-cli`` caps a
  functional-account password at 1,000 characters and a GCP ``private_key`` PEM is about
  1,700. The REST API this uses accepts 3,216;
- **the most common mistake is pasting the whole JSON key file** instead of the
  ``private_key`` field out of it.

So these pin: the composition per cloud, that the PEM is the field and survives at full
length, that a referenced account is never recorded as owned (teardown deletes only what
it minted), and that the composed password never reaches a log line.

Runs under pytest, or standalone:  python tests/test_cert_lab_functional_account.py
"""
import asyncio
import io
import logging
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []

# A real-shape PKCS#8 RSA-2048 PEM: what matters is that it is over the 1,000-character
# ps-cli cap, so a test that quietly used a short key would prove nothing.
_PEM = ("-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"] * 26)
        + "\n-----END PRIVATE KEY-----\n")
_KEY_JSON = ('{"type": "service_account", "project_id": "bt-se-lab", '
             '"private_key_id": "abc123", "private_key": '
             + '"' + _PEM.replace("\n", "\\n") + '", '
             '"client_email": "certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com"}')

_GCP_OUTPUTS = {
    "pool_id": "demo-pipeline-pool-47455d",
    "location": "us-central1",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----",
    "service_account_email": "certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com",
    "service_account_key_json": _KEY_JSON,
}
_AWS_OUTPUTS = {
    "ca_arn": "arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/abc",
    "region": "us-east-1",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----",
    "enroll_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "enroll_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CertLab:
    def __init__(self, **kw):
        self.id = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        self.name = "demo-pipeline"
        self.cloud = "gcp"
        self.error_message = None
        self.ps_functional_account = None
        self.ps_functional_account_id = None
        self.__dict__.update(kw)


async def _fake_create_fa_on_platform(*, platform_id, account_name, display_name,
                                      password, description="", tenant=None):
    CALLS.append(("create", platform_id, account_name, display_name, password))
    return 4242


async def _fake_get_platform_id(name):
    CALLS.append(("get_platform_id", name))
    return 1008


async def _fake_get_functional_account(name, tenant=None):
    CALLS.append(("get", name))
    return {"id": 7, "platform_id": 1008, "platform_name": "Certificate",
            "account_name": name}


async def _fake_delete_functional_account(account_id, tenant=None):
    CALLS.append(("delete", account_id))


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    ps = types.ModuleType("web_dashboard.services.ps_api_service")
    ps.create_functional_account_on_platform = _fake_create_fa_on_platform
    ps.get_platform_id = _fake_get_platform_id
    ps.get_functional_account = _fake_get_functional_account
    ps.delete_functional_account = _fake_delete_functional_account
    sys.modules["web_dashboard.services.ps_api_service"] = ps

    psr = types.ModuleType("web_dashboard.services.ps_resource_service")
    psr.PSResourceError = type("PSResourceError", (Exception,), {})
    psr._MAX_MANAGED_SYSTEM_ADDRESS = 255
    sys.modules["web_dashboard.services.ps_resource_service"] = psr


_install_stubs()
try:
    from web_dashboard.services import cert_ps_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cert_ps_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _reset(**conf):
    CONF.clear()
    CONF.update({"cert_ps_bi_api_key": "9f2c1b7e4a",
                 "pscli_api_account_name": "certauth-svc"})
    CONF.update(conf)
    CALLS[:] = []


def _run(coro):
    return asyncio.run(coro)


def _last_create():
    for call in reversed(CALLS):
        if call[0] == "create":
            return call
    raise AssertionError(f"no functional account was created; calls were {CALLS}")


# ── composing the two credentials ─────────────────────────────────────────────

def test_gcp_composes_both_halves_onto_one_account():
    _reset()
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    _, platform_id, account_name, display_name, password = _last_create()
    assert platform_id == 1008
    assert account_name == ("certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com"
                            ":certauth-svc")
    assert password.endswith(":9f2c1b7e4a")
    assert out["account_name"] == account_name
    assert out["id"] == "4242"
    assert out["mode"] == "create"


def test_aws_composes_from_its_own_differently_named_outputs():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(cloud="aws"), _AWS_OUTPUTS))
    _, _, account_name, _, password = _last_create()
    assert account_name == "AKIAIOSFODNN7EXAMPLE:certauth-svc"
    assert password == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY:9f2c1b7e4a"


def test_the_password_is_the_private_key_field_not_the_whole_json_file():
    """The single most common setup mistake, and it is silent — the plugin takes the
    value it is given and fails later, at a rotation."""
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert password.startswith("-----BEGIN PRIVATE KEY-----")
    assert not password.lstrip().startswith("{")
    assert "service_account" not in password, "the JSON envelope leaked into the password"
    assert "private_key_id" not in password


def test_a_full_size_pem_survives_at_full_length():
    """ps-cli caps a functional-account password at 1,000 characters; the REST API this
    goes through accepts 3,216. Nothing here may quietly truncate to the smaller one."""
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert len(_PEM) > 1000, "the fixture key is too short to prove anything"
    assert password == f"{_PEM}:9f2c1b7e4a"


def test_the_escaped_newlines_in_the_key_file_come_back_as_real_ones():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert "\\n" not in password, "the PEM is still JSON-escaped"
    assert password.count("\n") > 20


def test_the_display_name_carries_the_row_so_a_retry_resolves_back():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    display_name = _last_create()[3]
    assert display_name == "demo-pipeline-certauth-1a2b3c4d"


# ── the BeyondInsight half ────────────────────────────────────────────────────

def test_the_run_as_user_falls_back_to_the_configured_password_safe_one():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[2].endswith(":certauth-svc")

    _reset(cert_ps_bi_run_as_user="cert-plugin-svc")
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[2].endswith(":cert-plugin-svc")


def test_a_missing_api_key_names_the_config_key_and_creates_nothing():
    _reset(cert_ps_bi_api_key="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    except svc.CertPSError as exc:
        assert "cert_ps_bi_api_key" in str(exc)
    else:
        raise AssertionError("a missing API key must raise")
    assert not [c for c in CALLS if c[0] == "create"]


def test_a_missing_run_as_user_names_both_keys_it_could_come_from():
    _reset(pscli_api_account_name="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    except svc.CertPSError as exc:
        assert "cert_ps_bi_run_as_user" in str(exc)
        assert "pscli_api_account_name" in str(exc)
    else:
        raise AssertionError("a missing run-as user must raise")


def test_a_colon_in_a_beyondinsight_half_is_refused():
    """Splitting on the LAST colon is what lets the CA credential contain one. A colon
    in either BeyondInsight half moves the split point and mis-parses BOTH fields."""
    for key, value in (("cert_ps_bi_run_as_user", "corp:svc"),
                       ("cert_ps_bi_api_key", "9f2c:1b7e")):
        _reset(**{key: value})
        try:
            _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
        except svc.CertPSError as exc:
            assert "':'" in str(exc) or "colon" in str(exc).lower()
        else:
            raise AssertionError(f"a colon in {key} must be refused")
        assert not [c for c in CALLS if c[0] == "create"]


# ── bad or absent CA material ─────────────────────────────────────────────────

def test_a_key_file_without_a_private_key_field_is_refused():
    _reset()
    outputs = dict(_GCP_OUTPUTS, service_account_key_json='{"type": "service_account"}')
    try:
        _run(svc.ensure_functional_account(_CertLab(), outputs))
    except svc.CertPSError as exc:
        assert "private_key" in str(exc)
    else:
        raise AssertionError("a key file with no private_key must be refused")


def test_an_apply_that_returned_no_credential_is_refused():
    _reset()
    outputs = dict(_GCP_OUTPUTS, service_account_key_json="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), outputs))
    except svc.CertPSError as exc:
        assert "enrollment credential" in str(exc)
    else:
        raise AssertionError("no credential must be refused")


# ── modes ─────────────────────────────────────────────────────────────────────

def test_reference_mode_creates_nothing_and_uses_the_named_account():
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc")
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert out == {"mode": "reference", "account_name": "svc-adcs:certauth-svc",
                   "id": None}
    assert CALLS == [], "reference mode must not touch Password Safe here"


def test_reference_mode_reports_no_id_so_teardown_cannot_delete_it():
    """The id is the ownership flag. Returning one for an operator's own account would
    make teardown delete something every other CA may point at."""
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc")
    assert _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))["id"] is None


def test_the_mode_is_case_and_whitespace_tolerant():
    _reset(cert_ps_functional_account_mode="  Reference  ")
    assert svc.functional_account_mode() == "reference"


def test_an_unset_or_mistyped_mode_creates():
    """Anything that is not `reference` is `create`: a typo must not silently switch off
    the thing that makes the feature work."""
    _reset(cert_ps_functional_account_mode="")
    assert svc.functional_account_mode() == "create"
    _reset(cert_ps_functional_account_mode="refrence")
    assert svc.functional_account_mode() == "create"


def test_the_empty_account_error_points_at_the_right_remedy_per_mode():
    _reset()
    try:
        _run(svc.resolve_functional_account(""))
    except svc.CertPSError as exc:
        assert "Wire up Password Safe" in str(exc)
    else:
        raise AssertionError("create mode with no account must raise")

    _reset(cert_ps_functional_account_mode="reference")
    try:
        _run(svc.resolve_functional_account(""))
    except svc.CertPSError as exc:
        assert "cert_ps_functional_account" in str(exc)
    else:
        raise AssertionError("reference mode with no account must raise")


# ── the credential must not leak ──────────────────────────────────────────────

def test_the_composed_password_never_reaches_a_log_line():
    """The progress path persists every line verbatim into JobLog with no redaction, so
    a logger call carrying the password would put a private key in the database."""
    _reset()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    logged = buf.getvalue()
    assert "BEGIN PRIVATE KEY" not in logged
    assert "9f2c1b7e4a" not in logged, "the API key was logged"


def test_the_returned_dict_carries_the_name_but_never_the_credential():
    _reset()
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert "BEGIN PRIVATE KEY" not in repr(out)
    assert "9f2c1b7e4a" not in repr(out)


# ── the wiring, read off the source ───────────────────────────────────────────
#
# Text assertions rather than execution, matching tests/test_cert_lab_wiring.py: these
# are cross-file couplings where the failure is silent, and the whole cert_lab_service
# import pulls in sqlalchemy and the job worker for no extra proof.

def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_the_build_wires_up_the_account_and_never_fails_on_it():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_provision_apply(")[1].split("\nasync def ")[0]
    assert "_wire_up_functional_account(row, outputs)" in block, \
        "the build must mint the account while the apply's outputs are still in scope"
    assert block.index("_read_outputs(row, outputs)") < \
        block.index("_wire_up_functional_account"), "outputs are read first"

    helper = src.split("async def _wire_up_functional_account(")[1].split("\nasync def ")[0]
    assert "except Exception" in helper, "a CA that exists must not be rolled back"
    assert "row.error_message = str(exc)" in helper, \
        "only the message may be stored — never anything out of `outputs`"
    assert "row.status" not in helper, "the CA stays available when the account fails"


def test_add_identity_uses_this_cas_own_account_not_the_global_key():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_ps_register(")[1].split("\nasync def ")[0]
    assert "functional_account=row.ps_functional_account" in block, \
        "onboarding against another CA's identity fails every credential action"


def test_add_identity_is_refused_at_the_click_when_there_is_no_account():
    """And it consults BOTH sources: a CA built before this has a NULL column and an
    operator-configured account that works, which must not start being refused."""
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("def start_ps_register(")[1].split("\nasync def ")[0]
    assert "row.ps_functional_account" in block
    assert 'config_service.get("cert_ps_functional_account")' in block
    assert "Wire up Password Safe" in block


def test_the_page_shows_the_account_and_offers_the_retry():
    api = _src("web_dashboard/api/cert_lab.py")
    page = _src("web_dashboard/templates/cert_lab/index.html")
    assert '"functional_account": row.ps_functional_account' in api
    assert "/functional-account" in api, "the retry route is missing"
    assert "wireUp(ca)" in page and "async wireUp(ca)" in page
    assert "ca.functional_account" in page, \
        "a missing account has to be visible before Add identity is clicked"


def test_teardown_deletes_only_an_account_the_dashboard_minted():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_decommission(")[1].split("\nasync def ")[0]
    assert "if row.ps_functional_account_id:" in block, \
        "a NULL id is an operator-owned account and must survive"
    assert "delete_functional_account" in block
    assert "if not deregistered:" in block, \
        "a still-registered managed system blocks the delete; say so rather than 400"
    assert block.index("deregister(row.ps_tf_state)") < \
        block.index("delete_functional_account"), "the managed system goes first"


def test_the_id_is_cleared_only_after_a_successful_delete():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_decommission(")[1].split("\nasync def ")[0]
    delete_at = block.index("delete_functional_account")
    clear_at = block.index("row.ps_functional_account_id = None", delete_at)
    except_at = block.index("except Exception as exc:", delete_at)
    assert clear_at < except_at, \
        "clearing before the delete succeeds makes a retried teardown skip it"


def test_the_retry_reads_the_credential_back_out_of_the_state():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def rewire_functional_account(")[1].split("\nasync def ")[0]
    assert "terraform.read_state_outputs(row.deploy_job_id)" in block, \
        "a rebuild is not a retry: CAS never hands a deleted pool id back"
    assert "read_state_outputs" in _src("web_dashboard/services/terraform.py")


def test_both_columns_have_a_migration():
    src = _src("web_dashboard/database.py")
    for col in ("ps_functional_account", "ps_functional_account_id"):
        assert f"ALTER TABLE cert_labs ADD COLUMN {col} " in src, \
            f"{col} has no migration, so it is missing on every existing install"


def test_every_new_config_key_is_declared_bound_and_classified():
    conf = _src("web_dashboard/config.py")
    setup = _src("web_dashboard/api/setup.py")
    panel = _src("web_dashboard/templates/settings.html")
    model = setup.split("class CertLabFeatureConfig(")[1].split("\nclass ")[0]
    for key in ("cert_ps_bi_api_key", "cert_ps_bi_run_as_user",
                "cert_ps_functional_account_mode"):
        assert re.search(rf"^    {key}: ", conf, re.M), f"{key} missing from Settings"
        assert f"{key}: " in model, f"{key} missing from CertLabFeatureConfig"
        assert f"panelCfg.{key}" in panel, f"{key} is unbound, so a save discards it"
    secrets = setup.split("_SECRET_FEATURE_KEYS = frozenset({")[1].split("})")[0]
    assert '"cert_ps_bi_api_key"' in secrets, \
        "an unclassified secret round-trips in clear through the feature API"


def test_create_is_the_default_in_both_places_that_declare_it():
    conf = _src("web_dashboard/config.py")
    setup = _src("web_dashboard/api/setup.py")
    assert 'cert_ps_functional_account_mode: str = "create"' in conf
    assert 'cert_ps_functional_account_mode: str = "create"' in setup


def test_the_mode_select_keeps_its_x_init_default():
    """An unset string key reads back "" from /api/setup/feature, not the model default,
    so a select without x-init renders blank and saves "" over the mode."""
    panel = _src("web_dashboard/templates/settings.html")
    at = panel.index('x-model="panelCfg.cert_ps_functional_account_mode"')
    assert "x-init" in panel[at:at + 300]


if __name__ == "__main__":
    fns = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    failures = 0
    for name, fn in fns.items():
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
