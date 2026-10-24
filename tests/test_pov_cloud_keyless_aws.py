"""The POV cloud provider on AWS with no stored key: the dashboard's SPIFFE identity.

docs/profiles/pov/public-cloud.md → Keyless AWS. The credential resolution itself is
services/cloud_federation (pinned by tests/test_cloud_federation.py); what is pinned here
is the POV half of it:

  * the panel's model declares every field its "SPIFFE identity" choice binds — an
    undeclared bound field is DISCARDED on save, and this choice writes three;
  * Test connection names which credential answered, and shows the assumed role, so an
    operator can tell federation took over rather than inferring it — and a stored key is
    never echoed in full;
  * with nothing configured it gives federation's own reason, not "add an access key";
  * STS refusing the TOKEN (the issuer's keys could not be fetched) is told apart from
    the trust policy refusing the ROLE, because they are fixed in different consoles;
  * the panel never deletes a stored key on a radio change — only an explicit click,
    which must mark the secret touched or the save silently keeps it.

No cloud is reachable: the STS client and the thread hop are stubbed.

Run: python tests/test_pov_cloud_keyless_aws.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-keyless-aws-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-keyless-aws")

try:
    import boto3  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.api.setup import PovCloudFeatureConfig  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import (aws_service, cloud_federation,  # noqa: E402
                                    config_service, dashboard_identity, pov_cloud_aws)
from _settings_src import settings_source

Base.metadata.create_all(bind=engine)



class _Patch:
    """Minimal monkeypatch that works standalone as well as under pytest."""

    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


def _run_verify(patch, *, source, active=True, reason="", key_id="",
                result=(1, ""), raises=None):
    patch.set(pov_cloud_aws, "configured", lambda: True)
    patch.set(pov_cloud_aws, "default_region", lambda: "us-east-2")
    patch.set(cloud_federation, "source", lambda cloud: source)
    patch.set(cloud_federation, "active", lambda cloud: active)
    patch.set(cloud_federation, "reason", lambda cloud: reason)
    real_get = config_service.get
    patch.set(config_service, "get",
              lambda k, *a, **kw: key_id if k == "aws_access_key_id" else real_get(k, *a, **kw))

    async def _to_thread(fn, *args):
        if raises:
            raise raises
        return result
    patch.set(aws_service, "_to_thread", _to_thread)
    return asyncio.run(pov_cloud_aws.verify())


# ── the panel model ───────────────────────────────────────────────────────────

def test_the_panel_model_declares_what_the_spiffe_choice_writes():
    fields = PovCloudFeatureConfig.model_fields
    for name in ("aws_federation_role_arn", "dashboard_spiffe_identity_enabled",
                 "dashboard_spiffe_aud_aws"):
        assert name in fields, (
            f"PovCloudFeatureConfig does not declare {name}; the POV cloud panel binds it "
            f"and _write_feature would discard it on save")
    assert fields["dashboard_spiffe_identity_enabled"].annotation is bool
    assert fields["dashboard_spiffe_aud_aws"].annotation is bool


def test_the_spiffe_fields_are_the_same_keys_federation_reads():
    """One setting shown in two places, never a POV copy that federation does not read."""
    assert cloud_federation.AWS_ROLE_ARN == "aws_federation_role_arn"
    assert dashboard_identity.AUD_AWS == "dashboard_spiffe_aud_aws"


# ── Test connection ───────────────────────────────────────────────────────────

def test_a_federated_pass_names_the_identity_and_the_assumed_role():
    patch = _Patch()
    try:
        arn = "arn:aws:sts::123456789012:assumed-role/vm-dashboard-pov/vm-dashboard"
        ok, msg = _run_verify(patch, source=cloud_federation.SOURCE, result=(1, arn))
    finally:
        patch.undo()
    assert ok, msg
    assert arn in msg and "SPIFFE identity" in msg, msg


def test_a_stored_key_pass_names_the_key_without_echoing_it():
    patch = _Patch()
    try:
        ok, msg = _run_verify(patch, source="stored key", active=False,
                              key_id="AKIAABCDEFGH12345678",
                              result=(1, "arn:aws:iam::123456789012:user/pov"))
    finally:
        patch.undo()
    assert ok, msg
    assert "AKIA…5678" in msg, msg
    assert "AKIAABCDEFGH12345678" not in msg, msg


def test_nothing_configured_gives_federations_reason_not_just_add_a_key():
    patch = _Patch()
    try:
        patch.set(pov_cloud_aws, "configured", lambda: False)
        patch.set(cloud_federation, "reason", lambda cloud: "no AWS role ARN to assume is set")
        ok, msg = asyncio.run(pov_cloud_aws.verify())
    finally:
        patch.undo()
    assert not ok
    assert "SPIFFE identity" in msg and "no AWS role ARN to assume is set" in msg, msg


def test_a_refusal_says_which_credential_was_refused():
    patch = _Patch()
    try:
        ok, msg = _run_verify(patch, source=cloud_federation.SOURCE,
                              raises=aws_service.AWSError("AccessDenied"))
    finally:
        patch.undo()
    assert not ok
    assert "SPIFFE identity" in msg and "AccessDenied" in msg, msg


def test_no_region_visible_is_a_failure_even_when_the_credential_works():
    patch = _Patch()
    try:
        ok, msg = _run_verify(patch, source=cloud_federation.SOURCE, result=(0, "arn:x"))
    finally:
        patch.undo()
    assert not ok and "us-east-2" in msg, msg


# ── STS refusing the token vs the role ────────────────────────────────────────

class _Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _assume_refused(code):
    patch = _Patch()

    class _Sts:
        def assume_role_with_web_identity(self, **kw):
            raise _Refused(code)
    try:
        cloud_federation.clear_cache()
        patch.set(cloud_federation, "_cfg",
                  lambda k: "arn:aws:iam::123456789012:role/r" if k == cloud_federation.AWS_ROLE_ARN else "")
        patch.set(cloud_federation, "_read_token", lambda cloud: "a.b.c")
        patch.set(cloud_federation, "_sts_client", lambda region: _Sts())
        patch.set(dashboard_identity, "issuer", lambda: "https://pov-agents.example.com/spiffe")
        try:
            cloud_federation._aws_assume()
        except cloud_federation.FederationError as exc:
            return str(exc)
        raise AssertionError("an STS refusal did not raise FederationError")
    finally:
        patch.undo()
        cloud_federation.clear_cache()


def test_an_invalid_token_points_at_the_issuers_keys_not_the_trust_policy():
    msg = _assume_refused("InvalidIdentityToken")
    assert "https://pov-agents.example.com/spiffe/keys" in msg, msg
    assert "trust policy" not in msg, msg


def test_a_role_refusal_still_points_at_the_trust_policy():
    msg = _assume_refused("AccessDenied")
    assert "trust policy" in msg, msg


# ── the panel never drops a key on its own ────────────────────────────────────

def _method(src, name):
    start = src.index(f"    {name}(")
    end = src.index("\n    },", start)
    return src[start:end]


def test_choosing_spiffe_ticks_the_identity_and_never_clears_a_key():
    src = settings_source()
    body = _method(src, "choosePovAwsSource")
    assert "dashboard_spiffe_identity_enabled = true" in body
    assert "dashboard_spiffe_aud_aws = true" in body
    assert "aws_access_key_id" not in body and "aws_secret_access_key" not in body, (
        "choosing SPIFFE must not touch the stored key — removing it is an explicit click")


def test_removing_the_stored_key_marks_the_secret_touched():
    """savePanelConfig leaves an UNTOUCHED blank secret out of the payload, so a removal
    that forgot the touch list would save 200 and keep the key — and the key wins."""
    src = settings_source()
    body = _method(src, "removePovAwsStoredKey")
    assert "panelSecretsTouched.push('aws_secret_access_key')" in body, body
    assert "aws_access_key_id = ''" in body, body


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
