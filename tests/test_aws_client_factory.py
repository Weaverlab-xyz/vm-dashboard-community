"""aws_service.aws_client: the one way to make a boto3 client, and why.

What these pin:

- off the Workload Credentials tier it is exactly ``boto3.client(service, **_aws_kwargs)``
  (``_aws_kwargs_pinned`` with ``pinned``), so stored keys and the SPIFFE identity are
  unchanged;
- on it, the client signs with the lease's shared refreshable credentials, never a fixed
  copy, so a client made at the start of a job and used in a ten-minute waiter renews its
  credential instead of outliving a short lease. Checked against real botocore: a client
  whose lease expires signs its next call with the next lease;
- a lease that cannot be issued is an AWSError, as it was from ``_aws_kwargs``;
- nothing in web_dashboard/ builds a client from ``_aws_kwargs`` directly any more.

Run: python tests/test_aws_client_factory.py   (or under pytest)
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="aws-client-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-aws-client-tests")

import boto3  # noqa: E402

from web_dashboard.services import aws_service  # noqa: E402
from web_dashboard.services import workload_credential_lease as lease  # noqa: E402


def _utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Patch:
    """Swap attributes for the length of a block."""

    def __init__(self, obj, **attrs):
        self.obj, self.attrs = obj, attrs

    def __enter__(self):
        self.saved = {k: getattr(self.obj, k) for k in self.attrs}
        for k, v in self.attrs.items():
            setattr(self.obj, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)


def _rotating_lease(seconds):
    """A fake _fresh_lease that issues a new key each time, valid for ``seconds``."""
    n = [0]

    def fresh(cloud, purpose):
        n[0] += 1
        return ({"access_key_id": f"ASIAKEY{n[0]}", "secret_access_key": "s",
                 "session_token": "t"}, _utc() + timedelta(seconds=seconds))
    return fresh, n


def _on_tier(fresh):
    """Patches that put AWS on the dynamic tier with ``fresh`` as the lease."""
    return (_Patch(lease, dynamic_enabled=lambda cloud: cloud == "aws", _fresh_lease=fresh,
                   default_purpose=lambda cloud: "provision",
                   _effective_purpose=lambda cloud, purpose: purpose or "provision"),
            _Patch(aws_service, _aws_kwargs=lambda region: {"region_name": region or "us-east-1"}))


def test_off_the_dynamic_tier_it_is_boto3_client_with_aws_kwargs():
    calls = []
    with _Patch(lease, dynamic_enabled=lambda cloud: False), \
            _Patch(aws_service, _aws_kwargs=lambda region: {"region_name": region,
                                                            "aws_access_key_id": "AKIA"},
                   _aws_kwargs_pinned=lambda region: {"region_name": "pinned-" + region}), \
            _Patch(boto3, client=lambda service, **kw: calls.append((service, kw)) or "c"):
        assert aws_service.aws_client("ec2", "eu-west-1") == "c"
        aws_service.aws_client("rds", "ap-south-1", pinned=True)
    assert calls == [("ec2", {"region_name": "eu-west-1", "aws_access_key_id": "AKIA"}),
                     ("rds", {"region_name": "pinned-ap-south-1"})], calls


def test_on_the_dynamic_tier_the_client_signs_with_the_shared_refreshable_credentials():
    lease.invalidate("aws")
    fresh, _ = _rotating_lease(1800)
    a, b = _on_tier(fresh)
    with a, b:
        client = aws_service.aws_client("sts", "us-east-1")
        creds = lease.aws_refreshable_credentials()
        assert client._request_signer._credentials is creds
        assert client.meta.region_name == "us-east-1"
        again = aws_service.aws_client("ec2", "us-west-2")
        assert again._request_signer._credentials is creds, "a second credentials object"
    lease.invalidate("aws")


def test_a_client_renews_its_credential_when_the_lease_expires():
    lease.invalidate("aws")
    fresh, issued = _rotating_lease(2)
    a, b = _on_tier(fresh)
    with a, b:
        client = aws_service.aws_client("sts", "us-east-1")
        creds = client._request_signer._credentials
        # Shrink the windows so a 2-second lease exercises them; the real ones are minutes.
        creds._advisory_refresh_timeout, creds._mandatory_refresh_timeout = 1, 0
        first = creds.get_frozen_credentials().access_key
        assert creds.get_frozen_credentials().access_key == first, "refreshed too early"
        time.sleep(2.2)
        second = creds.get_frozen_credentials().access_key
    assert first != second and issued[0] >= 2, (first, second, issued[0])
    lease.invalidate("aws")


def test_a_lease_that_cannot_be_issued_is_an_aws_error():
    lease.invalidate("aws")

    def broken(cloud, purpose):
        raise lease.LeaseUnavailable("no usable aws credential for provision")
    a, b = _on_tier(broken)
    with a, b:
        try:
            aws_service.aws_client("ec2", "us-east-1")
            raise AssertionError("expected AWSError")
        except aws_service.AWSError as exc:
            assert "Workload Credentials" in str(exc)
    lease.invalidate("aws")


def test_nothing_builds_a_client_from_aws_kwargs_directly():
    offenders = []
    pattern = re.compile(r"boto3\.client\([^)]*\*\*\s*(aws_service\.)?_aws_kwargs", re.S)
    for dirpath, _, files in os.walk(os.path.join(_ROOT, "web_dashboard")):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            # The factory's own fallback is the one place allowed to.
            src = src.replace("boto3.client(service, **kwargs)", "")
            if pattern.search(src):
                offenders.append(os.path.relpath(path, _ROOT))
    assert not offenders, f"fixed-credential clients: {offenders}"


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
