"""The BeyondTrust platform identity, shared by PRA, Password Safe, EPM-L and Entitle.

What is guarded here:

  * the facade (services/bt_platform_identity.py): header shape, the per-product
    Service Name override, ``{site}`` substitution, and that an unknown auth mode
    reads as the product's NATIVE mode — a typo must fail with the familiar
    credential error, never go hunting for a metadata endpoint;
  * each product, in workload mode, makes NO native credential call (no
    ``/oauth2/token``, no ``Auth/Connect/Token``, no PAT) and sends the identity
    bearer to the resolved workload URL;
  * native mode is unchanged for every product;
  * a customer tenant (Password Safe override dict, Entitle ``cfg``) ignores
    workload mode — it is a different site with a different trust;
  * Terraform keeps the stored credentials (the hybrid the providers force);
  * the Settings panels bind only declared fields.

No network and no app: config_service and web_dashboard.config are stubbed, and the
workload token fetch is replaced with a constant.

Runs under pytest, or standalone:  python tests/test_bt_platform_identity.py
"""
import asyncio
import contextlib
import importlib
import os
import sys
import types
from _settings_src import settings_source

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SITE = "09be1e0a-29a9-4538-a335-b7ec84159a37"


def _b64(obj) -> str:
    import base64
    import json
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# An unsigned, inert stand-in JWT, built at import time rather than written out as a
# literal so the repo's secret scanner has nothing high-entropy to flag.
_FAKE_JWT = ".".join([_b64({"alg": "none"}), _b64({"sub": "workload"}), "sig"])

_CFG: dict = {}


def _pkg(name, path):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    if not getattr(mod, "__path__", None):
        mod.__path__ = [path]
    return mod


_web = _pkg("web_dashboard", os.path.join(_ROOT, "web_dashboard"))
_svc = _pkg("web_dashboard.services", os.path.join(_ROOT, "web_dashboard", "services"))


def _install_stubs():
    """(Re)install config stubs. Called per test: other test files install their
    own under the same names at collection time, and every module here resolves
    ``config_service`` at CALL time."""
    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default=None: _CFG.get(key, default if default is not None else "")
    cfg.get_raw = lambda key: _CFG.get(key, "")
    cfg.get_bool = lambda key, default=False: str(_CFG.get(key, default)).lower() in (
        "1", "true", "yes", "on")
    sys.modules["web_dashboard.services.config_service"] = cfg
    _svc.config_service = cfg
    settings = types.ModuleType("web_dashboard.config")
    settings.settings = types.SimpleNamespace(
        epml_pat="", epml_site_id="", epml_base_url="https://api.beyondtrust.io")
    sys.modules["web_dashboard.config"] = settings
    _web.config = settings


_install_stubs()


def _load(name):
    full = f"web_dashboard.services.{name}"
    mod = sys.modules.get(full)
    if mod is None or not getattr(mod, "__file__", "").startswith(_ROOT):
        sys.modules.pop(full, None)
        mod = importlib.import_module(full)
    return mod


wlc = _load("workload_credentials_service")
bpi = _load("bt_platform_identity")
epml = _load("epml_service")
pra = _load("pra_api_service")
ps = _load("ps_api_service")
eapi = _load("entitle_public_api")
esvc = _load("entitle_service")


_IDENTITY = {
    "wlc_identity_platform": "azure",
    "wlc_identity_audience": "https://management.azure.com/",
    "wlc_service_name": "Weaver_Workload_Credentials",
    "bt_platform_site_id": _SITE,
}


@contextlib.contextmanager
def _config(**values):
    """Run with config answering from ``values`` and a constant identity token."""
    _install_stubs()
    _CFG.clear()
    _CFG.update(values)
    original = bpi.wlc._workload_token
    calls = []

    def _fake_token():
        calls.append(1)
        return _FAKE_JWT

    bpi.wlc._workload_token = _fake_token
    try:
        yield calls
    finally:
        bpi.wlc._workload_token = original
        _CFG.clear()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = str(self._body)
        self.content = b"x"

    def json(self):
        return self._body


class _FakeClient:
    """Records every request; answers from ``answers[path]`` or 200 {}."""

    def __init__(self, answers=None):
        self.calls = []
        self.headers = {}
        self.answers = answers or {}

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.answers.get(url, _FakeResp(200, {"access_token": "oauth-tok"}))

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.answers.get(url, _FakeResp(200, []))


# ── the facade ────────────────────────────────────────────────────────────────

def test_headers_carry_the_token_and_the_shared_service_name():
    with _config(**_IDENTITY):
        h = bpi.headers("pra")
    assert h == {"Authorization": f"Bearer {_FAKE_JWT}",
                 "X-BT-Service-Name": "Weaver_Workload_Credentials"}


def test_a_product_override_wins_and_whitespace_is_stripped():
    with _config(**_IDENTITY, ps_workload_service_name="  ps-only \n"):
        assert bpi.headers("ps")["X-BT-Service-Name"] == "ps-only"
        assert bpi.headers("pra")["X-BT-Service-Name"] == "Weaver_Workload_Credentials"


def test_an_override_satisfies_a_blank_shared_service_name():
    cfg = dict(_IDENTITY, wlc_service_name="", entitle_workload_service_name="ent")
    with _config(**cfg):
        assert bpi.missing("entitle") == []
        assert "wlc_service_name" in bpi.missing("pra")


def test_an_incomplete_identity_names_what_is_missing_and_fetches_nothing():
    with _config(wlc_identity_platform="azure") as calls:
        try:
            bpi.headers("epml")
            raise AssertionError("expected IdentityError")
        except bpi.IdentityError as exc:
            assert "wlc_service_name" in str(exc)
            assert "wlc_identity_audience" in str(exc)
            assert "EPM-L" in str(exc)
    assert calls == [], "a token was fetched for an identity known to be incomplete"


def test_unknown_auth_modes_read_as_native():
    for raw in ("", "oauth", "pat", "token", "WORKLOADD", "entra"):
        with _config(pra_auth_mode=raw):
            assert bpi.auth_mode("pra") == bpi.AUTH_MODE_NATIVE, raw
    with _config(pra_auth_mode=" Workload "):
        assert bpi.uses_workload("pra")


def test_the_site_is_substituted_and_falls_back_through_wlc_then_epml():
    with _config(wlc_site_id="wlc-site"):
        assert bpi.resolve_url("https://x/site/{site}/pra/") == "https://x/site/wlc-site/pra"
    with _config(epml_site_id=" {epml-site}/ "):
        assert bpi.site_id() == "epml-site"
    with _config(bt_platform_site_id="shared", wlc_site_id="wlc-site"):
        assert bpi.site_id() == "shared"


def test_a_missing_site_is_a_clear_error_not_a_literal_brace_in_a_url():
    with _config():
        try:
            bpi.resolve_url("https://api.beyondtrust.io/site/{site}/pra", "pra")
            raise AssertionError("expected IdentityError")
        except bpi.IdentityError as exc:
            assert "site id" in str(exc)
        # A template without {site} needs none.
        assert bpi.resolve_url("https://tenant.beyondtrustcloud.com/") == \
            "https://tenant.beyondtrustcloud.com"


def test_every_product_has_a_default_url_except_epml():
    assert set(bpi.DEFAULT_URLS) == {"pra", "ps", "entitle"}
    for url in bpi.DEFAULT_URLS.values():
        assert "{site}" in url and url.startswith("https://api.beyondtrust.io/site/")


def test_identity_missing_matches_the_wlc_workload_requirements():
    """_missing() was refactored onto identity_missing(); the WLC answer must not move."""
    with _config(wlc_auth_mode="workload", wlc_identity_platform="gcp"):
        assert wlc._missing() == ["wlc_site_id", "wlc_service_name",
                                  "wlc_identity_audience"]
        assert wlc.identity_missing() == ["wlc_service_name", "wlc_identity_audience"]


def test_unverified_claims_are_for_display_only():
    claims = bpi.unverified_claims(_FAKE_JWT)
    assert claims == {"sub": "workload"}
    assert bpi.unverified_claims("not-a-jwt") == {}


# ── EPM-L ─────────────────────────────────────────────────────────────────────

def test_epml_workload_sends_no_pat_and_uses_the_shared_site():
    with _config(**_IDENTITY, epml_auth_mode="workload", epml_pat="PAT_should_not_be_sent"):
        client = epml._client()
        try:
            assert client.headers["authorization"] == f"Bearer {_FAKE_JWT}"
            assert client.headers["x-bt-service-name"] == "Weaver_Workload_Credentials"
            assert str(client.base_url).rstrip("/").endswith(f"/site/{_SITE}/epm/linux")
        finally:
            _run(client.aclose())


def test_epml_native_still_sends_the_pat():
    with _config(epml_pat="PAT_abc", epml_site_id=_SITE) as calls:
        client = epml._client()
        try:
            assert client.headers["authorization"] == "Bearer PAT_abc"
            assert "x-bt-service-name" not in client.headers
        finally:
            _run(client.aclose())
    assert calls == []


def test_epml_workload_rejection_does_not_talk_about_a_pat():
    class R:
        status_code = 401
        text = '{"error":"Access denied"}'
    with _config(**_IDENTITY, epml_auth_mode="workload"):
        try:
            epml._raise_for_api_error(R(), "list_packages")
            raise AssertionError("expected EpmlError")
        except epml.EpmlError as exc:
            assert "Product Scopes" in str(exc)
            assert "PAT" not in str(exc)


# ── PRA ───────────────────────────────────────────────────────────────────────

def test_pra_workload_makes_no_oauth_call_and_uses_the_workload_url():
    with _config(**_IDENTITY, pra_auth_mode="workload", bt_api_host="pra.example.com",
                 bt_client_id="cid", bt_client_secret="sec"):
        client = _FakeClient()
        host, auth = _run(pra._auth(client))
        assert pra.configured()
    assert client.calls == [], "workload mode still asked /oauth2/token"
    assert host == f"https://api.beyondtrust.io/site/{_SITE}/pra"
    assert auth["Authorization"] == f"Bearer {_FAKE_JWT}"


def test_pra_native_is_unchanged():
    with _config(bt_api_host="pra.example.com", bt_client_id="cid", bt_client_secret="sec") as calls:
        client = _FakeClient()
        host, auth = _run(pra._auth(client))
    assert calls == []
    assert host == "https://pra.example.com"
    assert client.calls[0][1] == "https://pra.example.com/oauth2/token"
    assert auth == {"Authorization": "Bearer oauth-tok"}


def test_pra_workload_with_a_broken_identity_raises_a_pra_error():
    with _config(pra_auth_mode="workload"):
        try:
            _run(pra._auth(_FakeClient()))
            raise AssertionError("expected PRAApiError")
        except pra.PRAApiError as exc:
            assert "PRA" in str(exc)


# ── Password Safe ─────────────────────────────────────────────────────────────

def test_ps_workload_skips_the_token_but_still_signs_in():
    with _config(**_IDENTITY, ps_auth_mode="workload"):
        client = _FakeClient()
        _run(ps._sign_in(client))
        base = ps._base_url()
    assert [c[1] for c in client.calls] == ["Auth/SignAppIn"]
    assert client.headers["Authorization"] == f"Bearer {_FAKE_JWT}"
    assert client.headers["X-BT-Service-Name"] == "Weaver_Workload_Credentials"
    assert base == (f"https://api.beyondtrust.io/site/{_SITE}/passwordsafe"
                    "/BeyondTrust/api/public/v3")


def test_ps_workload_tolerates_a_gateway_without_signappin():
    with _config(**_IDENTITY, ps_auth_mode="workload"):
        client = _FakeClient({"Auth/SignAppIn": _FakeResp(404)})
        _run(ps._sign_in(client))      # must not raise


def test_ps_workload_names_the_scopes_on_a_rejected_signin():
    with _config(**_IDENTITY, ps_auth_mode="workload"):
        client = _FakeClient({"Auth/SignAppIn": _FakeResp(401)})
        try:
            _run(ps._sign_in(client))
            raise AssertionError("expected PSApiError")
        except ps.PSApiError as exc:
            assert "Product Scopes" in str(exc)


def test_ps_native_is_unchanged():
    with _config(pscli_api_url="ps.example.com", pscli_client_id="cid",
                 pscli_client_secret="sec") as calls:
        client = _FakeClient()
        _run(ps._sign_in(client))
    assert calls == []
    assert [c[1] for c in client.calls] == ["Auth/Connect/Token", "Auth/SignAppIn"]
    assert client.headers["Authorization"] == "Bearer oauth-tok"


def test_a_ps_tenant_override_ignores_workload_mode():
    tenant = ps.tenant_creds("cust.example.com", "tcid", "tsec")
    with _config(**_IDENTITY, ps_auth_mode="workload") as calls:
        client = _FakeClient()
        _run(ps._sign_in(client, tenant))
        assert ps._base_url(tenant) == "https://cust.example.com/BeyondTrust/api/public/v3"
    assert calls == [], "a customer tenant presented this install's identity"
    assert client.calls[0][2]["data"]["client_id"] == "tcid"


# ── Entitle ───────────────────────────────────────────────────────────────────

def test_entitle_workload_auth_uses_the_identity_and_the_workload_url():
    with _config(**_IDENTITY, entitle_auth_mode="workload", entitle_api_token="tok"):
        base, headers = eapi.auth()
        assert eapi.configured()
        api_url, svc_headers = esvc._auth()
    assert base == api_url == f"https://api.beyondtrust.io/site/{_SITE}/entitle"
    assert headers["Authorization"] == svc_headers["Authorization"] == f"Bearer {_FAKE_JWT}"


def test_entitle_native_is_unchanged():
    with _config(entitle_api_url="https://api.us.entitle.io/v1", entitle_api_token="tok"):
        assert eapi.auth() == ("https://api.us.entitle.io", {"Authorization": "Bearer tok"})
        # The machine-request client shares that base. It used to keep the FULL
        # entitle_api_url, so with the shipped /v1 default every request went to
        # /v1/public/v1/accessRequests — a 404 for every machine elevation.
        assert esvc._auth() == ("https://api.us.entitle.io", {"Authorization": "Bearer tok"})


def test_an_entitle_tenant_cfg_ignores_workload_mode():
    tenant_cfg = {"entitle_api_url": "https://api.ca.entitle.io/v1",
                  "entitle_api_token": "cust"}.get
    with _config(**_IDENTITY, entitle_auth_mode="workload") as calls:
        assert eapi.auth(lambda k: tenant_cfg(k, "")) == (
            "https://api.ca.entitle.io", {"Authorization": "Bearer cust"})
    assert calls == []


# ── the hybrid: Terraform keeps the stored credentials ────────────────────────

def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def test_terraform_paths_never_use_the_platform_identity():
    """The sra, passwordsafe and entitle providers take client credentials or an API
    key only. Wiring the identity into their env would hand them a JWT they reject."""
    for path in (("web_dashboard", "services", "terraform_pra_service.py"),
                 ("web_dashboard", "services", "ps_resource_service.py"),
                 ("web_dashboard", "services", "entitle_registration_service.py"),
                 ("web_dashboard", "services", "btapi_service.py"),
                 ("web_dashboard", "services", "password_safe_runner.py")):
        assert "bt_platform_identity" not in _read(*path), "/".join(path)
    assert '"TF_VAR_bt_client_secret"' in _read(
        "web_dashboard", "services", "terraform_pra_service.py")


# ── the Settings panels ───────────────────────────────────────────────────────

_NEW_FIELDS = {
    "PRAFeatureConfig": ("pra_auth_mode", "pra_workload_api_url", "pra_workload_service_name"),
    "PasswordSafeFeatureConfig": ("ps_auth_mode", "ps_workload_api_url",
                                  "ps_workload_service_name"),
    "EPMLFeatureConfig": ("epml_auth_mode", "epml_workload_service_name"),
    "EntitleFeatureConfig": ("entitle_auth_mode", "entitle_workload_api_url",
                             "entitle_workload_service_name"),
    "BtPlatformIdentityFeatureConfig": ("bt_platform_site_id", "wlc_identity_platform",
                                        "wlc_service_name", "wlc_identity_audience",
                                        "wlc_identity_token_file", "wlc_entra_client_id"),
}


def _model_body(setup, name):
    body = setup[setup.index(f"class {name}(BaseModel):"):]
    return body[:body.index("\nclass ", 1)]


def test_every_new_binding_is_declared_on_its_model_and_in_config():
    """A field bound in the template but absent from its model is DISCARDED on save."""
    html = settings_source()
    setup = _read("web_dashboard", "api", "setup.py")
    config = _read("web_dashboard", "config.py")
    for model, fields in _NEW_FIELDS.items():
        body = _model_body(setup, model)
        for field in fields:
            assert f"panelCfg.{field}" in html, f"{field} is not on any panel"
            assert f"    {field}: str" in body, f"{field} is not on {model}"
            assert f"    {field}: str" in config, f"{field} is not in config.py"


def test_the_identity_fields_moved_off_the_wlc_model():
    """Two models owning one key = two panels overwriting each other on save."""
    setup = _read("web_dashboard", "api", "setup.py")
    wlc_body = _model_body(setup, "WorkloadCredentialsFeatureConfig")
    for field in _NEW_FIELDS["BtPlatformIdentityFeatureConfig"]:
        assert f"    {field}: str" not in wlc_body, field


def test_the_shared_panel_is_registered_config_only_with_a_unique_test_method():
    setup = _read("web_dashboard", "api", "setup.py")
    html = settings_source()
    assert '"bt_platform_identity": BtPlatformIdentityFeatureConfig' in setup
    only = setup[setup.index("_CONFIG_ONLY_FEATURES = {"):]
    assert '"bt_platform_identity"' in only[:only.index("}")]
    assert "{ key: 'bt_platform_identity'" in html
    # A duplicate Alpine method key silently replaces the first definition.
    assert html.count("async testBtIdentity()") == 1
    assert "/api/setup/bt-platform-identity/test" in html
    assert '@router.post("/bt-platform-identity/test")' in setup


def test_the_identity_test_endpoint_calls_no_beyondtrust_endpoint():
    src = _read("web_dashboard", "services", "bt_platform_identity.py")
    fn = src[src.index("def test_identity("):]
    fn = fn[:fn.index("\ndef ", 1)] if "\ndef " in fn[1:] else fn
    assert "_request" not in fn and "httpx" not in fn and "generate" not in fn


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
