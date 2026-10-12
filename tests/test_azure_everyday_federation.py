"""Azure with wlc_azure_everyday_source = federation.

What these pin:

- outside a job, Azure calls use the dashboard's SPIFFE identity (a client-assertion
  credential for the app in azure_client_id), and the Workload Credentials lease is not
  consulted at all;
- inside a job, the lease's service-principal password, as before;
- federation that is not usable fails closed, naming why, and never falls back to the
  lease: the lease is the privileged credential;
- the credential cache holds both at once, so the request path and a job in the same
  process do not evict each other;
- the grant script never gives the everyday app a role that can write role assignments.

Run: python tests/test_azure_everyday_federation.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="az-everyday-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-azure-everyday-tests")

try:
    from azure.identity import ClientAssertionCredential, ClientSecretCredential
except ImportError:  # pragma: no cover
    print("SKIP: azure-identity not installed")
    sys.exit(0)

from web_dashboard.services import azure_service  # noqa: E402
from web_dashboard.services import cloud_federation, config_service  # noqa: E402
from web_dashboard.services import workload_credential_lease as lease  # noqa: E402

CONF = {"azure_client_id": "everyday-app", "azure_tenant_id": "tenant",
        "azure_subscription_id": "sub"}


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


def _federation_mode(*, reason="", quad=None, lease_calls=None):
    """Patches for federation mode. ``quad`` is what the lease returns in a job."""
    def azure_credentials(purpose="", **kw):
        if lease_calls is not None:
            lease_calls.append(lease.default_purpose("azure"))
        return quad
    return (
        _Patch(lease, dynamic_enabled=lambda cloud: cloud == "azure",
               everyday_from_federation=lambda cloud: cloud == "azure",
               has_distinct_readonly=lambda cloud: False,
               azure_credentials=azure_credentials),
        _Patch(cloud_federation, reason=lambda cloud: reason),
        _Patch(config_service, get=lambda key, *a, **k: CONF.get(key, "")),
    )


def _creds():
    return asyncio.run(azure_service._ensure_creds())


def test_outside_a_job_it_is_the_spiffe_identity_and_the_lease_is_not_asked():
    azure_service.invalidate_credentials()
    calls = []
    a, b, c = _federation_mode(quad={"client_id": "x"}, lease_calls=calls)
    with a, b, c:
        cred, sub = _creds()
    assert isinstance(cred, ClientAssertionCredential), type(cred)
    assert sub == "sub"
    assert calls == [], f"the lease was consulted on the request path: {calls}"


def test_inside_a_job_it_is_the_lease():
    azure_service.invalidate_credentials()
    quad = {"client_id": "target-app", "client_secret": "pw", "tenant_id": "tenant",
            "subscription_id": "sub"}
    a, b, c = _federation_mode(quad=quad)
    with a, b, c, lease.provisioning():
        cred, _ = _creds()
    assert isinstance(cred, ClientSecretCredential), type(cred)


def test_unusable_federation_fails_closed_and_never_reaches_the_lease():
    azure_service.invalidate_credentials()
    calls = []
    a, b, c = _federation_mode(reason="a stored key is configured, and it wins",
                               quad={"client_id": "x"}, lease_calls=calls)
    with a, b, c:
        try:
            _creds()
            raise AssertionError("expected AzureError")
        except azure_service.AzureError as exc:
            assert "a stored key is configured" in str(exc)
            assert "only inside jobs" in str(exc)
    assert calls == [], "fell back to the lease"


def test_the_cache_holds_both_credentials():
    azure_service.invalidate_credentials()
    quad = {"client_id": "target-app", "client_secret": "pw", "tenant_id": "tenant",
            "subscription_id": "sub"}
    a, b, c = _federation_mode(quad=quad)
    with a, b, c:
        everyday, _ = _creds()
        with lease.provisioning():
            job, _ = _creds()
        assert _creds()[0] is everyday, "the request-path credential was rebuilt"
        with lease.provisioning():
            assert _creds()[0] is job, "the job credential was rebuilt"
    assert everyday is not job
    azure_service.invalidate_credentials()


def test_the_grant_script_keeps_role_assignment_writers_off_the_everyday_app():
    src = open(os.path.join(_ROOT, "scripts", "wlc", "setup-azure-apps.sh"),
               encoding="utf-8").read()
    assert '--everyday-sp)' in src
    assert 'copy_roles "$EVERYDAY_OBJECT_ID" "everyday" 1' in src
    assert 'copy_roles "$TARGET_SP_OBJECT_ID" "target" 0' in src
    for role in ("Owner", "User Access Administrator", "Role Based Access Control Administrator"):
        assert f'"{role}"' in src, role


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
