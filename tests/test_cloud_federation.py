"""The dashboard's cloud credentials, federated through its own SPIFFE identity.

docs/design/dashboard-workload-identity.md, Slice 3 (services/cloud_federation). No cloud
is reachable from a test run: STS is botocore's own Stubber, and the Azure and GCP
credentials are checked by what they are built from, not by an exchange. Pinned:

  * federation is used only when EVERY condition holds — and a stored key always wins,
    so switching it on changes nothing until a key is retired on purpose;
  * AWS: AssumeRoleWithWebIdentity gets the file's token, the role and the session name,
    is cached to five minutes before expiry, and reaches boto3 kwargs, Terraform's
    provider and state backend, and Packer — with the session token;
  * Azure: one constructor; a secret when there is one, otherwise an assertion that
    re-reads the CURRENT file on every token request; Terraform gets the OIDC variables;
    no other module constructs a ClientSecretCredential any more;
  * GCP: the external_account config names the file, the provider and STS (and the SA
    to impersonate), is written beside the token without secrets, and reaches the SDK,
    Terraform and the Packer upload, which no longer needs a JSON key;
  * Settings is told which rung each cloud uses.

Run: python tests/test_cloud_federation.py   (or under pytest)
"""
import ast
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="cloud-federation-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-cloud-federation-tests")
for _var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(_var, None)

try:
    import azure.identity  # noqa: F401
    import boto3  # noqa: F401
    import botocore  # noqa: F401
    import google.auth  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from botocore.stub import Stubber  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import (aws_service, cloud_federation, config_service,  # noqa: E402
                                    dashboard_identity, terraform_provider_env)

Base.metadata.create_all(bind=engine)

ROLE = "arn:aws:iam::123456789012:role/vm-dashboard"
GCP_AUD = ("//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/"
           "p/providers/dash")
_SETTINGS = ("dashboard_spiffe_identity_enabled", "dashboard_spiffe_aud_aws",
             "dashboard_spiffe_aud_azure", "dashboard_spiffe_aud_gcp",
             "aws_federation_role_arn", "gcp_federation_service_account",
             "aws_access_key_id", "aws_secret_access_key", "aws_region",
             "azure_client_id", "azure_client_secret", "azure_tenant_id",
             "azure_subscription_id", "gcp_service_account_json", "gcp_project_id")


def _jwt(sub="spiffe://dash.example/dashboard", tag="a"):
    import base64
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'ES256'})}.{enc({'sub': sub, 'tag': tag})}.sig{tag}"


def _fresh(**settings):
    """A clean slate: a new token dir holding all three tokens, these settings only."""
    cloud_federation.clear_cache()
    os.environ["SPIFFE_TOKEN_DIR"] = tempfile.mkdtemp(prefix="spiffe-tokens-")
    for cloud in cloud_federation.CLOUDS:
        _write(cloud, _jwt(tag=cloud))
    for key in _SETTINGS:
        config_service.set(key, "")
    for key, value in settings.items():
        config_service.set(key, "1" if value is True else str(value))
    return os.environ["SPIFFE_TOKEN_DIR"]


def _write(cloud, token):
    with open(cloud_federation.token_path(cloud), "w", encoding="ascii") as fh:
        fh.write(token)


AWS_ON = dict(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_aws=True,
              aws_federation_role_arn=ROLE)
AZURE_ON = dict(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_azure=True,
                azure_client_id="app-id", azure_tenant_id="tenant-id",
                azure_subscription_id="sub-id")
GCP_ON = dict(dashboard_spiffe_identity_enabled=True, dashboard_spiffe_aud_gcp=GCP_AUD,
              gcp_project_id="proj-1")


class _StubbedSts:
    """Replaces cloud_federation._sts_client with a real, Stubber-checked STS client."""

    def __init__(self):
        self.calls = 0
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def __call__(self, region):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        client = boto3.client("sts", region_name=region or "us-east-1",
                              config=Config(signature_version=UNSIGNED))
        stub = Stubber(client)
        stub.add_response(
            "assume_role_with_web_identity",
            {"Credentials": {"AccessKeyId": "ASIAFEDERATED0000", "SecretAccessKey": "fed-secret",
                             "SessionToken": "fed-session", "Expiration": self.expiry}},
            {"RoleArn": ROLE, "RoleSessionName": "vm-dashboard",
             "WebIdentityToken": _read_token("aws")})
        stub.activate()
        self.calls += 1
        return client


def _read_token(cloud):
    with open(cloud_federation.token_path(cloud), encoding="ascii") as fh:
        return fh.read()


STS = _StubbedSts()
cloud_federation._sts_client = STS


# ── when federation is used at all ────────────────────────────────────────────

def test_every_condition_must_hold_and_says_which_one_does_not():
    _fresh(**AWS_ON)
    assert cloud_federation.active("aws")
    cases = [
        (dict(AWS_ON, dashboard_spiffe_identity_enabled=""), "identity is off"),
        (dict(AWS_ON, dashboard_spiffe_aud_aws=""), "audience is not ticked"),
        (dict(AWS_ON, aws_federation_role_arn=""), "no AWS role ARN"),
        (dict(AWS_ON, aws_access_key_id="AKIA", aws_secret_access_key="s"), "stored key"),
    ]
    for settings, why in cases:
        _fresh(**settings)
        assert not cloud_federation.active("aws") and why in cloud_federation.reason("aws"), (
            settings, cloud_federation.reason("aws"))
    _fresh(**AWS_ON)
    os.unlink(cloud_federation.token_path("aws"))
    assert "does not exist" in cloud_federation.reason("aws")
    _fresh(**dict(GCP_ON, gcp_project_id=""))
    assert "gcp_project_id" in cloud_federation.reason("gcp")
    _fresh(**dict(AZURE_ON, azure_tenant_id=""))
    assert "tenant id" in cloud_federation.reason("azure")


def test_settings_is_told_which_rung_each_cloud_uses():
    _fresh(**dict(AWS_ON, **GCP_ON, azure_client_secret="s"))
    src = dashboard_identity.status()["sources"]
    assert src["aws"] == cloud_federation.SOURCE and src["gcp"] == cloud_federation.SOURCE
    assert src["azure"] == "stored key"
    _fresh()
    assert dashboard_identity.status()["sources"]["aws"] == "not configured"


# ── AWS ───────────────────────────────────────────────────────────────────────

def test_aws_assumes_the_role_with_the_files_token_and_caches_it():
    _fresh(**AWS_ON)
    STS.calls = 0
    now = time.time()
    creds = cloud_federation.aws_credentials(now)
    assert creds == {"access_key_id": "ASIAFEDERATED0000", "secret_access_key": "fed-secret",
                     "session_token": "fed-session"}
    cloud_federation.aws_credentials(now + 60)
    assert STS.calls == 1, "a fresh credential was fetched again"
    cloud_federation.aws_credentials(now + 3600 - 200)
    assert STS.calls == 2, "a credential inside its last five minutes was reused"


def test_boto3_kwargs_use_federation_only_with_no_stored_key():
    _fresh(**AWS_ON)
    kw = aws_service._aws_kwargs("us-east-1")
    assert kw["aws_session_token"] == "fed-session" and kw["aws_access_key_id"] == "ASIAFEDERATED0000"
    _fresh(**dict(AWS_ON, aws_access_key_id="AKIASTORED", aws_secret_access_key="s"))
    kw = aws_service._aws_kwargs("us-east-1")
    assert kw["aws_access_key_id"] == "AKIASTORED" and "aws_session_token" not in kw
    _fresh()
    kw = aws_service._aws_kwargs("us-east-1")
    assert "aws_access_key_id" not in kw, "off: boto3's default chain, as before"


def test_a_refused_assume_role_is_an_error_not_a_fall_through():
    _fresh(**AWS_ON)
    real = cloud_federation._sts_client

    def refusing(region):
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        c = boto3.client("sts", region_name="us-east-1",
                         config=Config(signature_version=UNSIGNED))
        s = Stubber(c)
        s.add_client_error("assume_role_with_web_identity",
                           service_error_code="InvalidIdentityToken")
        s.activate()
        return c
    cloud_federation._sts_client = refusing
    try:
        aws_service._aws_kwargs("us-east-1")
        raise AssertionError("a refused federation fell through to the default chain")
    except aws_service.AWSError as exc:
        assert "InvalidIdentityToken" in str(exc) and _read_token("aws") not in str(exc)
    finally:
        cloud_federation._sts_client = real


def test_terraform_and_packer_get_the_session_token():
    _fresh(**AWS_ON)
    env = terraform_provider_env.aws_env()
    assert env == {"AWS_ACCESS_KEY_ID": "ASIAFEDERATED0000", "AWS_SECRET_ACCESS_KEY": "fed-secret",
                   "AWS_SESSION_TOKEN": "fed-session"}
    from web_dashboard.services import storage_service, terraform
    real = storage_service.active_backend
    storage_service.active_backend = lambda: "s3"
    try:
        backend_type, _cfg, backend_env = terraform._backend_settings("/tmp/deploy-x")
    finally:
        storage_service.active_backend = real
    assert backend_type == "s3" and backend_env.get("AWS_SESSION_TOKEN") == "fed-session", (
        "the S3 state backend authenticates separately from the provider")
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "packer_build_service.py"),
               encoding="utf-8").read()
    assert "cloud_federation.aws_subprocess_env()" in src


# ── Azure ─────────────────────────────────────────────────────────────────────

def test_azure_uses_a_secret_when_there_is_one():
    from azure.identity import ClientSecretCredential
    _fresh(**AZURE_ON)
    cred = cloud_federation.azure_credential("t", "c", "a-secret")
    assert isinstance(cred, ClientSecretCredential)


def test_azure_asserts_with_the_current_file_on_every_request():
    from azure.identity import ClientAssertionCredential
    _fresh(**AZURE_ON)
    cred = cloud_federation.azure_credential("tenant-id", "app-id", "")
    assert isinstance(cred, ClientAssertionCredential)
    # azure-identity keeps the assertion callback as `_func` and calls it per request.
    callback = cred._func
    first = callback()
    _write("azure", _jwt(tag="rotated"))
    assert callback() != first and callback() == _read_token("azure"), (
        "the assertion is a snapshot, not a read of the file at request time")


def test_azure_with_no_secret_and_no_federation_says_why():
    _fresh()
    try:
        cloud_federation.azure_credential("t", "c", "")
        raise AssertionError("built a credential with nothing to authenticate with")
    except cloud_federation.FederationError as exc:
        assert "identity is off" in str(exc)


def test_terraform_gets_the_azure_oidc_variables():
    _fresh(**AZURE_ON)
    env = terraform_provider_env.azure_env()
    assert env["ARM_USE_OIDC"] == "true"
    assert env["ARM_OIDC_TOKEN_FILE_PATH"] == cloud_federation.token_path("azure")
    assert env["ARM_CLIENT_ID"] == "app-id" and "ARM_CLIENT_SECRET" not in env


def test_no_module_constructs_a_client_secret_credential_itself():
    """One constructor, so no Azure call site can quietly skip federation."""
    services = os.path.join(_ROOT, "web_dashboard", "services")
    offenders = []
    for name in sorted(os.listdir(services)):
        if not name.endswith(".py") or name == "cloud_federation.py":
            continue
        with open(os.path.join(services, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "ClientSecretCredential"):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"build Azure credentials with cloud_federation.azure_credential: {offenders}"


# ── GCP ───────────────────────────────────────────────────────────────────────

def test_the_external_account_config_names_the_file_provider_and_sa():
    _fresh(**dict(GCP_ON, gcp_federation_service_account="dash@proj-1.iam.gserviceaccount.com"))
    info = cloud_federation.gcp_external_account_info()
    assert info["type"] == "external_account" and info["audience"] == GCP_AUD
    assert info["token_url"] == "https://sts.googleapis.com/v1/token"
    assert info["credential_source"] == {"file": cloud_federation.token_path("gcp")}
    assert info["service_account_impersonation_url"] == (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "dash@proj-1.iam.gserviceaccount.com:generateAccessToken")
    creds = cloud_federation.gcp_credentials()
    assert creds is not None and type(creds).__module__ == "google.auth.identity_pool"


def test_the_config_file_is_written_beside_the_token_without_secrets():
    d = _fresh(**GCP_ON)
    cloud_federation.write_gcp_config()
    with open(os.path.join(d, cloud_federation.GCP_CONFIG_FILE), encoding="utf-8") as fh:
        written = fh.read()
    assert json.loads(written) == cloud_federation.gcp_external_account_info()
    assert _read_token("gcp") not in written, "the config must name the token, not hold it"
    config_service.set("dashboard_spiffe_aud_gcp", "")
    cloud_federation.write_gcp_config()
    assert not os.path.exists(os.path.join(d, cloud_federation.GCP_CONFIG_FILE))


def test_terraform_reads_gcp_through_the_config_file():
    _fresh(**GCP_ON)
    cloud_federation.write_gcp_config()
    env = terraform_provider_env.gcp_env()
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == cloud_federation.gcp_config_path()
    assert env["GOOGLE_PROJECT"] == "proj-1" and "GOOGLE_CREDENTIALS" not in env
    _fresh(**dict(GCP_ON, gcp_service_account_json='{"type":"service_account"}'))
    env = terraform_provider_env.gcp_env()
    assert "GOOGLE_CREDENTIALS" in env and "GOOGLE_APPLICATION_CREDENTIALS" not in env


def test_the_gcp_sdk_loader_falls_to_federation_then_adc():
    from web_dashboard.services import gcp_service
    _fresh(**GCP_ON)
    assert type(gcp_service._gcp_creds()).__module__ == "google.auth.identity_pool"
    _fresh()
    assert gcp_service._gcp_creds() is None, "off: ADC, as before"


def test_the_packer_upload_no_longer_needs_a_json_key():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "packer_service.py"),
               encoding="utf-8").read()
    i = src.index("def _upload():\n        from . import cloud_federation")
    body = src[i:src.index("return f\"gs://", i)]
    assert 'credentials.get("gcp_service_account_json")' in body
    assert "cloud_federation.gcp_credentials()" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
