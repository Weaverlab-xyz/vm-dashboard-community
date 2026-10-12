"""The AWS Secrets Manager backend signs with the dashboard's own AWS identity.

What these pin:

- every Secrets Manager client in the backend comes from ``aws_service.aws_client``,
  so a key-less install (SPIFFE federation, or a Workload Credentials lease) can use
  ``aws_sm://`` references; before, a bare ``boto3.client`` saw only the container's
  environment and never the dashboard's configured credential;
- the vault's own region is passed pinned, so the wizard's ``aws_region`` does not
  override ``secrets_aws_region`` or a registered vault's region;
- a dashboard AWS credential that is itself an ``aws_sm://`` reference is refused by
  name instead of recursing;
- the OCI signing key and its passphrase are on the Secrets page, so they can be
  migrated to a vault and are covered by the staleness scan.

Run: python tests/test_aws_sm_dashboard_identity.py   (or under pytest)
"""
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="aws-sm-identity-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-aws-sm-identity")

from web_dashboard.services import aws_service, secret_hygiene  # noqa: E402
from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402

_SRC = os.path.join(_ROOT, "web_dashboard", "services", "secrets_backend_service.py")


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


def test_the_client_is_the_dashboards_aws_identity_with_the_vault_region_pinned():
    seen = []

    def aws_client(service, region="", *, pinned=False):
        seen.append((service, region, pinned))
        return "client"
    with _Patch(aws_service, aws_client=aws_client):
        assert sbs._aws_sm_client("eu-west-1") == "client"
        assert sbs._aws_sm_client(None) == "client"
    assert seen == [("secretsmanager", "eu-west-1", True), ("secretsmanager", "", True)]


def test_no_secrets_manager_client_bypasses_the_factory():
    src = open(_SRC, encoding="utf-8").read()
    assert not re.search(r"boto3\.client\(", src), "a bare boto3 client is back"
    assert src.count("_aws_sm_client(region)") >= 10


def test_an_aws_credential_stored_as_an_aws_sm_reference_is_refused_not_recursed():
    depth = []

    def aws_client(service, region="", *, pinned=False):
        # What _aws_kwargs does when aws_secret_access_key is an aws_sm:// reference:
        # resolve it, which needs a Secrets Manager client.
        depth.append(1)
        return sbs._aws_sm_client(region)
    with _Patch(aws_service, aws_client=aws_client):
        try:
            sbs._aws_sm_client("us-east-1")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "itself an aws_sm:// reference" in str(exc)
    assert len(depth) == 1
    # The guard is released afterwards: the next call is not refused.
    with _Patch(aws_service, aws_client=lambda *a, **k: "client"):
        assert sbs._aws_sm_client("us-east-1") == "client"


def test_the_oci_signing_key_is_on_the_secrets_page():
    registry = {k for k, _ in secret_hygiene.SECRET_REGISTRY}
    assert {"oci_private_key", "oci_private_key_passphrase"} <= registry
    assert {"oci_private_key", "oci_private_key_passphrase"} <= secret_hygiene.SECRET_KEYS


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
