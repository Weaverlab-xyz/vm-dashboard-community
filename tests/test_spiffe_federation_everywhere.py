"""SPIFFE federation as the way to hold no cloud secret, with or without Workload Credentials.

What these pin:

- AWS can take its everyday credential from the SPIFFE identity
  (wlc_aws_everyday_source = federation): no everyday lease, the provisioning lease only
  in jobs, and a broken federation fails closed rather than falling back to the lease;
- a federated AWS client renews its role session instead of keeping a snapshot;
- a key-less install is not "unconfigured": the AWS tiles and the image registry
  preflight accept federation or Workload Credentials;
- GCP's SDK path reads gcp_credentials_json like everything else does;
- the issuer check reports what a cloud would trip over, without exception text;
- the trust modules pin the exact subject on every cloud.

Run: python tests/test_spiffe_federation_everywhere.py   (or under pytest)
"""
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spiffe-fed-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spiffe-federation-tests")
for _var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(_var, None)

from web_dashboard.services import aws_service  # noqa: E402
from web_dashboard.services import cloud_federation, config_service  # noqa: E402
from web_dashboard.services import dashboard_identity, feature_flags  # noqa: E402
from web_dashboard.services import workload_credential_lease as lease  # noqa: E402


class _Patch:
    def __init__(self, obj, **attrs):
        self.obj, self.attrs = obj, attrs

    def __enter__(self):
        self.saved = {k: getattr(self.obj, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.obj, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)


def _conf(values):
    return _Patch(config_service, get=lambda key, *a, **k: values.get(key, ""))


def _src(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── AWS: everyday credential from the SPIFFE identity ────────────────────────

def _aws_fed_lease():
    """Lease config: AWS on the dynamic tier, everyday from federation, and an everyday
    secret named too, which this mode must ignore."""
    return _Patch(lease,
                  _cfg=lambda key, default="": {
                      "wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-everyday",
                      "wlc_aws_everyday_source": "federation"}.get(key, default),
                  _cfg_bool=lambda key: key in ("workload_credentials_enabled",
                                                "wlc_aws_enabled"))


def test_aws_federation_mode_has_no_everyday_lease():
    with _aws_fed_lease():
        assert lease.everyday_from_federation("aws")
        assert lease.purposes_for("aws") == (lease.PURPOSE_PROVISION,)
        assert lease.warm_purposes_for("aws") == ()
        assert lease.credentials("aws") is None, "an everyday lease was served"
        assert lease.aws_subprocess_env() is None


def test_aws_everyday_calls_fail_closed_when_federation_is_unusable():
    lease.invalidate()
    with _aws_fed_lease(), _conf({}), \
            _Patch(cloud_federation, reason=lambda cloud: "no AWS role ARN to assume is set"):
        try:
            aws_service._aws_kwargs("us-east-1")
            raise AssertionError("expected AWSError")
        except aws_service.AWSError as exc:
            assert "no AWS role ARN" in str(exc) and "only inside jobs" in str(exc)


def test_aws_jobs_still_take_the_provisioning_lease():
    lease.invalidate()
    triple = {"access_key_id": "ASIAJOB", "secret_access_key": "s", "session_token": "t"}
    with _aws_fed_lease(), _conf({}), \
            _Patch(lease, _serve_or_mint=lambda cloud, purpose, name, *, force: triple):
        with lease.provisioning():
            kwargs = aws_service._aws_kwargs("us-east-1")
    lease.invalidate()
    assert kwargs["aws_access_key_id"] == "ASIAJOB"


# ── AWS: federated clients renew the role session ────────────────────────────

class _STS:
    """Issues a new key per call, each valid for ``seconds``."""

    def __init__(self, seconds):
        self.seconds, self.calls = seconds, 0

    def assume_role_with_web_identity(self, **kw):
        self.calls += 1
        return {"Credentials": {
            "AccessKeyId": f"ASIAFED{self.calls}", "SecretAccessKey": "s",
            "SessionToken": "t",
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=self.seconds)}}


def _federated_aws(sts):
    return (_Patch(cloud_federation, reason=lambda cloud: "",
                   _read_token=lambda cloud: "jwt", _sts_client=lambda region: sts,
                   _cfg=lambda key: {"aws_federation_role_arn": "arn:aws:iam::1:role/d"}.get(key, "")),
            _Patch(lease, dynamic_enabled=lambda cloud: False),
            _Patch(aws_service, _aws_kwargs=lambda region: {
                "region_name": "us-east-1", "aws_access_key_id": "snap",
                "aws_secret_access_key": "s", "aws_session_token": "t"}))


def test_a_federated_client_signs_with_refreshable_credentials():
    cloud_federation.clear_cache()
    sts = _STS(3600)
    a, b, c = _federated_aws(sts)
    with a, b, c:
        client = aws_service.aws_client("sts", "us-east-1")
        creds = client._request_signer._credentials
        assert creds is cloud_federation.aws_refreshable_credentials()
        assert creds.get_frozen_credentials().access_key == "ASIAFED1"
    cloud_federation.clear_cache()


def test_the_role_session_is_renewed_before_it_expires():
    cloud_federation.clear_cache()
    sts = _STS(2)
    a, b, c = _federated_aws(sts)
    with a, b, c, _Patch(cloud_federation, _AWS_MARGIN_S=1):
        creds = cloud_federation.aws_refreshable_credentials()
        creds._advisory_refresh_timeout, creds._mandatory_refresh_timeout = 1, 0
        first = creds.get_frozen_credentials().access_key
        time.sleep(2.2)
        second = creds.get_frozen_credentials().access_key
    cloud_federation.clear_cache()
    assert first != second, (first, second)


def test_the_refresh_window_matches_the_cache_margin():
    src = _src("web_dashboard", "services", "cloud_federation.py")
    assert "creds._advisory_refresh_timeout = _AWS_MARGIN_S" in src


# ── A key-less install is configured ─────────────────────────────────────────

def test_aws_tiles_accept_federation_and_workload_credentials():
    with _conf({}):
        with _Patch(cloud_federation, active=lambda cloud: cloud == "aws"), \
                _Patch(lease, dynamic_enabled=lambda cloud: False):
            assert feature_flags.cloud_configured("aws")
        with _Patch(cloud_federation, active=lambda cloud: False), \
                _Patch(lease, dynamic_enabled=lambda cloud: cloud == "aws"):
            assert feature_flags.cloud_configured("aws")
        with _Patch(cloud_federation, active=lambda cloud: False), \
                _Patch(lease, dynamic_enabled=lambda cloud: False):
            assert not feature_flags.cloud_configured("aws")


def test_the_image_registry_preflight_accepts_federation():
    from web_dashboard.services import image_registry_service as reg
    with _conf({}), _Patch(cloud_federation, active=lambda cloud: cloud == "aws"), \
            _Patch(lease, dynamic_enabled=lambda cloud: False):
        result = reg._aws_creds_configured()
    assert result["status"] == "pass" and "SPIFFE" in result["detail"]


def test_gcp_sdk_calls_read_either_stored_key_name():
    src = _src("web_dashboard", "services", "gcp_service.py")
    body = src.split("def _gcp_creds", 1)[1].split("\ndef ", 1)[0]
    assert '_cfg("gcp_service_account_json") or _cfg("gcp_credentials_json")' in body


# ── The issuer check ─────────────────────────────────────────────────────────

ISS = "https://agents.example.com/spiffe"


def _fetcher(doc_issuer=ISS, kids=("k1",), raise_for=None):
    def fetch(url):
        if raise_for and raise_for in url:
            raise ConnectionError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        if url.endswith("/.well-known/openid-configuration"):
            return 200, {"issuer": doc_issuer, "jwks_uri": f"{ISS}/keys"}
        return 200, {"keys": [{"kid": k} for k in kids]}
    return fetch


def _check(fetch, ours=("k1",)):
    with _Patch(dashboard_identity, issuer=lambda: ISS,
                jwks=lambda now=None: [{"kid": k} for k in ours]):
        return dashboard_identity.check_issuer(fetch)


def test_a_reachable_issuer_serving_our_keys_passes():
    result = _check(_fetcher())
    assert result["ok"], result


def test_an_issuer_naming_another_url_fails():
    result = _check(_fetcher(doc_issuer=ISS + "/"))
    bad = [c for c in result["checks"] if not c["ok"]]
    assert not result["ok"] and bad[0]["name"] == "Issuer matches"


def test_an_untrusted_certificate_is_named_without_the_exception_text():
    result = _check(_fetcher(raise_for="openid-configuration"))
    detail = result["checks"][-1]["detail"]
    assert not result["ok"] and "public CA" in detail
    assert "SSL:" not in detail and "verify failed" not in detail


def test_someone_elses_keys_fail():
    result = _check(_fetcher(kids=("other",)))
    assert not result["ok"]
    assert any(c["name"] == "Keys are this dashboard's" and not c["ok"]
               for c in result["checks"])


def test_the_check_needs_write_and_is_not_public():
    src = _src("web_dashboard", "api", "spiffe_oidc.py")
    route = src.split('@admin_router.post("/check-issuer")', 1)[1]
    assert 'require_explicit_permission("agents", "write")' in route.split("\n\n", 1)[0] + route


# ── The trust modules ────────────────────────────────────────────────────────

def test_every_trust_module_pins_the_exact_subject():
    aws = _src("terraform", "dashboard_federation", "aws", "main.tf")
    assert '"${local.issuer_host}:sub" = local.subject' in aws
    assert '"${local.issuer_host}:aud" = "sts.amazonaws.com"' in aws
    gcp = _src("terraform", "dashboard_federation", "gcp", "main.tf")
    assert "attribute_condition = \"assertion.sub == '${local.subject}'\"" in gcp
    azure = _src("terraform", "dashboard_federation", "azure", "main.tf")
    assert 'subject = "spiffe://${var.trust_domain}/dashboard"' in azure
    for src in (aws, gcp):
        assert 'subject     = "spiffe://${var.trust_domain}/dashboard"' in src \
            or 'subject = "spiffe://${var.trust_domain}/dashboard"' in src


def test_the_everyday_aws_role_carries_the_same_deny_as_the_wlc_everyday_secret():
    fed = _src("terraform", "dashboard_federation", "aws", "main.tf")
    wlc = _src("terraform", "workload_credentials", "aws.tf")
    denied = set(re.findall(r'"((?:iam|sts|organizations|account):[\w*]+)"',
                            fed.split('"everyday_only"', 1)[1]))
    wlc_denied = set(re.findall(r'"((?:iam|sts|organizations|account):[\w*]+)"',
                                wlc.split('Sid    = "NoPrivilegeEscalation"', 1)[1]))
    # The federated role is itself assumed with web identity; denying that in its own
    # policy would be meaningless, so it is the one entry left out.
    assert denied == wlc_denied - {"sts:AssumeRoleWithWebIdentity"}, (denied, wlc_denied)


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
