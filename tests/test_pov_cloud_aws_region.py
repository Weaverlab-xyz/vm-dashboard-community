"""The POV cloud driver builds its AWS clients in the region it was given.

``aws_service._aws_kwargs`` lets the configured ``aws_region`` override the region its
caller passes — right for the demo consoles, which always target the default region, and
wrong for a POV cloud template, which names a region of its own. Before the fix every
``pov_cloud_aws`` call went through ``_get_ec2`` and a template naming us-west-2 built in
the configured us-east-2 instead, with nothing to say so.

What these pin:

- a POV read for us-west-2 builds its EC2 client for us-west-2 while ``aws_region`` is
  us-east-2;
- the demo consoles' ``_get_ec2`` still answers with the configured region;
- the POV cost estimate's Pricing client stays in us-east-1, where the API lives.

boto3 itself is stubbed: only the ``region_name`` each client is built with matters.

Run: python tests/test_pov_cloud_aws_region.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-cloud-aws-region")

try:
    import boto3
except ModuleNotFoundError as exc:  # pragma: no cover — boto3 absent outside CI
    try:
        import pytest
        pytest.skip(f"boto3 unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.services import aws_service, config_service  # noqa: E402
from web_dashboard.services import pov_cloud_aws, pov_cloud_cost  # noqa: E402

CONFIGURED = "us-east-2"
_CONFIG = {
    "aws_region": CONFIGURED,
    # A stored key, so _aws_kwargs answers without reaching for federation.
    "aws_access_key_id": "AKIATEST",
    "aws_secret_access_key": "secret",
}


class _FakeEC2:
    def describe_instances(self, **_kw):
        return {"Reservations": []}


def _install():
    """Stub config and boto3; return the list each built client's (service, region) lands in."""
    built = []
    saved = (config_service.get, boto3.client)

    def fake_get(key, default=None):
        return _CONFIG.get(key, default if default is not None else "")

    def fake_client(service, **kw):
        built.append((service, kw.get("region_name")))
        return _FakeEC2()

    config_service.get = fake_get
    boto3.client = fake_client
    return built, saved


def _restore(saved):
    config_service.get, boto3.client = saved


def test_pov_read_uses_template_region_not_configured_default():
    built, saved = _install()
    try:
        assert pov_cloud_aws._describe_sync("us-west-2", []) == []
    finally:
        _restore(saved)
    assert built == [("ec2", "us-west-2")], built


def test_pov_pinned_client_falls_back_to_configured_when_blank():
    built, saved = _install()
    try:
        aws_service._get_ec2_pinned("")
    finally:
        _restore(saved)
    assert built == [("ec2", CONFIGURED)], built


def test_demo_console_client_still_follows_configured_region():
    built, saved = _install()
    try:
        aws_service._get_ec2("us-west-2")
    finally:
        _restore(saved)
    assert built == [("ec2", CONFIGURED)], built


def test_pricing_client_stays_in_pricing_region():
    built, saved = _install()
    try:
        pov_cloud_cost._client()
    finally:
        _restore(saved)
    assert built == [("pricing", pov_cloud_cost._PRICING_REGION)], built


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
