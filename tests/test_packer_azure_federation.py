"""Azure Packer builds with no client secret: the SPIFFE identity as a client assertion.

What these pin:

- the credential ladder: a Workload Credentials lease (in a job), then a stored secret,
  then a build-scoped assertion from the SPIFFE identity, and exactly one of secret and
  assertion is ever set (the azure-arm plugin refuses both; verified against 2.6.4);
- the assertion is minted for the Azure audience with the configured lifetime, clamped;
- with nothing usable the build is refused, naming why federation is not in use;
- every azure-arm template declares client_jwt sensitive and wires it into the source,
  so the assertion stays out of the template file.

Run: python tests/test_packer_azure_federation.py   (or under pytest)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="packer-az-fed-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-packer-azure-federation")

from web_dashboard.services import cloud_federation, dashboard_identity  # noqa: E402
from web_dashboard.services import dashboard_spire  # noqa: E402
from web_dashboard.services import packer_build_service as pbs  # noqa: E402
from web_dashboard.services import packer_service  # noqa: E402
from web_dashboard.services import workload_credential_lease as lease  # noqa: E402

IDS = {"azure_client_id": "app", "azure_tenant_id": "tenant", "azure_subscription_id": "sub"}


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


def _ladder(*, quad=None, stored=None, active=True, why="", assertion="jwt.a.b"):
    conf = dict(IDS, **(stored or {}))
    calls = []

    def build_assertion():
        calls.append("minted")
        return assertion
    patches = (
        _Patch(lease, azure_credentials=lambda *a, **k: quad),
        _Patch(pbs, _cfg=lambda key, default="": conf.get(key, default)),
        _Patch(cloud_federation, active=lambda cloud: active, reason=lambda cloud: why,
               azure_build_assertion=build_assertion),
    )
    return patches, calls


def _resolve(**kw):
    patches, calls = _ladder(**kw)
    a, b, c = patches
    with a, b, c:
        return pbs._azure_build_credentials(), calls


def test_a_lease_wins_and_no_assertion_is_minted():
    quad = {"client_id": "target", "client_secret": "pw", "tenant_id": "t",
            "subscription_id": "s"}
    (cid, secret, jwt, *_), calls = _resolve(quad=quad)
    assert (cid, secret, jwt) == ("target", "pw", "") and calls == []


def test_a_stored_secret_wins_over_federation():
    (_, secret, jwt, *_), calls = _resolve(stored={"azure_client_secret": "stored"})
    assert (secret, jwt) == ("stored", "") and calls == []


def test_with_no_secret_the_identity_becomes_the_assertion():
    (cid, secret, jwt, tenant, sub), calls = _resolve()
    assert (cid, secret, jwt, tenant, sub) == ("app", "", "jwt.a.b", "tenant", "sub")
    assert calls == ["minted"]


def test_nothing_usable_is_refused_with_the_reason():
    try:
        _resolve(active=False, why="the Azure audience is not ticked")
        raise AssertionError("expected PackerError")
    except pbs.PackerError as exc:
        assert "the Azure audience is not ticked" in str(exc)


def test_exactly_one_of_secret_and_assertion_is_ever_set():
    """The azure-arm plugin (2.6.4, checked with packer validate) refuses a template
    with both: 'No valid set of authentication values specified'."""
    for kw in ({}, {"stored": {"azure_client_secret": "s"}},
               {"quad": {"client_id": "t", "client_secret": "p", "tenant_id": "t",
                         "subscription_id": "s"}}):
        (_, secret, jwt, *_), _ = _resolve(**kw)
        assert bool(secret) != bool(jwt), (kw, secret, jwt)


def test_the_assertion_is_minted_for_azure_with_the_clamped_lifetime():
    seen = {}

    def mint(audience, ttl_s, td=None):
        seen.update(audience=audience, ttl_s=ttl_s)
        return "tok.en.x"
    for minutes, expect in (("120", 7200), ("5", 900), ("9999", 28800), ("junk", 7200)):
        with _Patch(cloud_federation, reason=lambda cloud: "",
                    _cfg=lambda key, m=minutes: m if key == cloud_federation.PACKER_ASSERTION_TTL else ""), \
                _Patch(dashboard_spire, trust_domain=lambda: "td", mint_jwt=mint,
                       dashboard_id=lambda td: f"spiffe://{td}/dashboard"), \
                _Patch(dashboard_identity, _check_minted=lambda *a: None,
                       issuer=lambda: "https://i"):
            assert cloud_federation.azure_build_assertion() == "tok.en.x"
        assert seen == {"audience": "api://AzureADTokenExchange", "ttl_s": expect}, (minutes, seen)


def test_an_unusable_federation_mints_nothing():
    with _Patch(cloud_federation, reason=lambda cloud: "a stored key is configured, and it wins"), \
            _Patch(dashboard_spire, mint_jwt=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("minted"))):
        try:
            cloud_federation.azure_build_assertion()
            raise AssertionError("expected FederationError")
        except cloud_federation.FederationError as exc:
            assert "stored key" in str(exc)


def test_every_azure_template_takes_the_assertion_as_a_sensitive_var():
    decls = packer_service._AZURE_SP_VAR_DECLS
    assert 'variable "client_jwt" {\n  default   = ""\n  sensitive = true\n}' in decls
    assert "client_jwt      = var.client_jwt" in packer_service._AZURE_SP_SOURCE_FIELDS
    t = packer_service.generate_azure_template(
        "Canonical", "offer", "sku", "Standard_B2s", "img", False)
    assert "var.client_jwt" in t and "jwt.a.b" not in t
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "packer_service.py"),
               encoding="utf-8").read()
    assert src.count('source "azure-arm" "build" {\\n\'\n        + _AZURE_SP_SOURCE_FIELDS') \
        == src.count('source "azure-arm" "build"')


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
