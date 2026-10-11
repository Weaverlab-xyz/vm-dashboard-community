"""Unit tests for choosing the IdP managed clusters federate with (k8s_oidc_provider).

Entra was the only IdP a managed cluster could trust. EKS takes any OIDC issuer and a
GCP workforce pool can front any OIDC IdP, so the provider is now a setting: entra
(unchanged), okta, pingone, or a generic oidc issuer. These pin:
- Entra resolves exactly as before (entra: username prefix, no groups prefix,
  device-code), so existing EKS associations and bindings are untouched;
- every other provider prefixes its GROUPS on EKS, because IdPs other than Entra emit
  group names and one named system:masters must not become that group;
- AKS stays Entra whatever the setting says;
- disable undoes the provider a cluster was federated WITH, and enable refuses to
  switch a federated cluster to another provider in place.

Stubs the DB / sqlalchemy imports so k8s_service loads without an app/DB (same as
test_entra_federation). Runs under pytest or standalone.
"""
import asyncio
import os
import sys
import types

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)
sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm = types.ModuleType("sqlalchemy.orm")
_orm.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm)
_db = types.ModuleType("web_dashboard.database")
_db.Job = type("Job", (), {})
_db.K8sCluster = type("K8sCluster", (), {"id": None, "cloud": None})
sys.modules.setdefault("web_dashboard.database", _db)

_CONF: dict = {}
_conf_stub = types.ModuleType("web_dashboard.services.config_service")
_conf_stub.get = lambda key: _CONF.get(key, "")
_conf_stub.set = lambda key, val: _CONF.__setitem__(key, val)
sys.modules.setdefault("web_dashboard.services.config_service", _conf_stub)

# The EKS leg calls aws_service lazily; a recording stub keeps boto3 out of it.
_AWS_CALLS: list = []
_aws = types.ModuleType("web_dashboard.services.aws_service")
_aws.associate_eks_oidc = lambda name, region, **kw: (
    _AWS_CALLS.append(("associate", name, region, kw)) or {"update_id": "u1", "already": False})
_aws.disassociate_eks_oidc = lambda name, region, config_name="entra": (
    _AWS_CALLS.append(("disassociate", name, region, config_name)) or {"removed": True})
_aws.describe_eks_oidc_status = lambda name, region, config_name="entra": "ACTIVE"
sys.modules.setdefault("web_dashboard.services.aws_service", _aws)

from web_dashboard.services import k8s_service as k  # noqa: E402

_EKS_KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: k8s-demo
clusters:
- name: k8s-demo
  cluster: {server: 'https://127.0.0.1:6443', certificate-authority-data: TEST_CA_B64}
contexts:
- name: k8s-demo
  context: {cluster: k8s-demo, user: k8s-demo}
users:
- name: k8s-demo
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: [eks, get-token, --cluster-name, k8s-demo, --region, us-east-2]
"""

_OKTA = {"k8s_oidc_provider": "okta",
         "k8s_oidc_issuer_url": "https://example.okta.com/oauth2/default",
         "k8s_oidc_client_id": "0oaKUBE",
         "k8s_oidc_rbac_group_id": "k8s-admins",
         "entra_rbac_group_id": "ENTRA-OID"}


class _Row:
    def __init__(self, cid, cloud, name="demo"):
        self.id, self.cloud, self.name = cid, cloud, name


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **kw):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **kw):
        return _FakeQuery(self._rows)


def _with_cfg(mapping):
    """Swap k._cfg (and the cluster-side helpers) for stubs; returns a restore callable."""
    orig = (k._cfg, k.resolve_kubeconfig, k._run_cluster_command)
    commands = []

    async def _run(kubeconfig, command, target_cloud=None):
        commands.append(command)
        return "ENTRA_BOUND_OK"

    k._cfg = lambda key, default="": mapping.get(key, default)
    k.resolve_kubeconfig = lambda db, cid: _EKS_KUBECONFIG
    k._run_cluster_command = _run

    def restore():
        k._cfg, k.resolve_kubeconfig, k._run_cluster_command = orig
        _CONF.clear()
        _AWS_CALLS.clear()
    restore.commands = commands
    return restore


def test_provider_defaults_to_entra():
    restore = _with_cfg({})
    try:
        assert k._configured_oidc_provider() == "entra"
    finally:
        restore()


def test_unknown_provider_is_refused():
    restore = _with_cfg({"k8s_oidc_provider": "github"})
    try:
        try:
            k._configured_oidc_provider()
            raise AssertionError("expected K8sError for an unknown provider")
        except k.K8sError:
            pass
    finally:
        restore()


def test_entra_settings_are_unchanged():
    # Existing EKS associations were made with entra: and no groups prefix; their
    # bindings name the bare group OID. Changing either would orphan them.
    restore = _with_cfg({"entra_oidc_client_id": "C", "azure_tenant_id": "T"})
    try:
        s = k._oidc_settings("entra")
    finally:
        restore()
    assert s["issuer"] == "https://login.microsoftonline.com/T/v2.0"
    assert s["username_claim"] == "oid"
    assert s["username_prefix"] == "entra:" and s["groups_prefix"] == ""
    assert s["grant_type"] == "device-code"


def test_okta_settings_prefix_users_and_groups():
    restore = _with_cfg(_OKTA)
    try:
        s = k._oidc_settings("okta")
    finally:
        restore()
    assert s["issuer"] == "https://example.okta.com/oauth2/default"
    assert s["client_id"] == "0oaKUBE"
    assert s["username_claim"] == "sub" and s["groups_claim"] == "groups"
    assert s["username_prefix"] == "okta:" and s["groups_prefix"] == "okta:"


def test_non_entra_settings_need_issuer_and_client():
    for missing in ("k8s_oidc_issuer_url", "k8s_oidc_client_id"):
        cfg = {kk: v for kk, v in _OKTA.items() if kk != missing}
        restore = _with_cfg(cfg)
        try:
            try:
                k._oidc_settings("okta")
                raise AssertionError(f"expected K8sError without {missing}")
            except k.K8sError:
                pass
        finally:
            restore()


def test_non_entra_issuer_must_be_https():
    restore = _with_cfg({**_OKTA, "k8s_oidc_issuer_url": "http://example.okta.com"})
    try:
        try:
            k._oidc_settings("okta")
            raise AssertionError("expected K8sError for an http issuer")
        except k.K8sError:
            pass
    finally:
        restore()


def test_pingone_issuer_is_taken_as_given():
    # No trailing-slash surgery: EKS compares the issuer string with the token's iss.
    issuer = "https://auth.pingone.eu/11111111-2222-3333-4444-555555555555/as"
    restore = _with_cfg({"k8s_oidc_issuer_url": issuer, "k8s_oidc_client_id": "P"})
    try:
        s = k._oidc_settings("pingone")
    finally:
        restore()
    assert s["issuer"] == issuer and s["groups_prefix"] == "pingone:"


def test_authcode_grant_drops_the_device_code_flag():
    restore = _with_cfg({**_OKTA, "k8s_oidc_grant_type": "authcode"})
    try:
        out = k._oidc_login_kubeconfig(_EKS_KUBECONFIG, k._oidc_settings("okta"))
    finally:
        restore()
    args = yaml.safe_load(out)["users"][0]["user"]["exec"]["args"]
    assert "--oidc-issuer-url=https://example.okta.com/oauth2/default" in args
    assert "--oidc-client-id=0oaKUBE" in args
    assert not any(a.startswith("--grant-type") for a in args)


def test_device_code_is_the_default_grant():
    restore = _with_cfg(_OKTA)
    try:
        out = k._oidc_login_kubeconfig(_EKS_KUBECONFIG, k._oidc_settings("okta"))
    finally:
        restore()
    assert "--grant-type=device-code" in yaml.safe_load(out)["users"][0]["user"]["exec"]["args"]


def test_bad_grant_type_is_refused():
    restore = _with_cfg({**_OKTA, "k8s_oidc_grant_type": "password"})
    try:
        try:
            k._oidc_settings("okta")
            raise AssertionError("expected K8sError for an unsupported grant")
        except k.K8sError:
            pass
    finally:
        restore()


def test_legacy_enable_marker_means_entra():
    restore = _with_cfg(_OKTA)
    try:
        _CONF["k8s_entra_fed_c1"] = "1"          # written before the provider choice
        assert k._federated_provider("c1") == "entra"
        _CONF["k8s_entra_fed_c1"] = "okta"
        assert k._federated_provider("c1") == "okta"
        assert k._federated_provider("c2") == ""
    finally:
        restore()


def test_aks_is_always_entra():
    restore = _with_cfg(_OKTA)
    try:
        assert k.cluster_oidc_provider(_Row("c1", "azure")) == "entra"
        assert k.cluster_oidc_provider(_Row("c2", "aws")) == "okta"
    finally:
        restore()


def test_rbac_subject_per_cloud_and_provider():
    restore = _with_cfg({**_OKTA, "gcp_workforce_pool_id": "pool"})
    try:
        assert k._rbac_group_subject(_Row("a", "aws"), "okta", "k8s-admins") == "okta:k8s-admins"
        assert k._rbac_group_subject(_Row("a", "aws"), "entra", "OID") == "OID"
        assert k._rbac_group_subject(_Row("z", "azure"), "entra", "OID") == "OID"
        assert k._rbac_group_subject(_Row("g", "gcp"), "okta", "k8s-admins") == (
            "principalSet://iam.googleapis.com/locations/global/workforcePools/pool/group/k8s-admins")
    finally:
        restore()


def test_default_group_follows_the_provider():
    restore = _with_cfg(_OKTA)
    try:
        assert k._default_rbac_group("okta") == "k8s-admins"
        assert k._default_rbac_group("entra") == "ENTRA-OID"
    finally:
        restore()


def test_bind_on_eks_names_the_prefixed_group():
    restore = _with_cfg(_OKTA)
    try:
        res = asyncio.run(k.bind_entra_group(_FakeDB([_Row("c1", "aws")]), "c1"))
        assert res["provider"] == "okta" and res["subject"] == "okta:k8s-admins"
        assert "--group=okta:k8s-admins" in restore.commands[-1]
        assert _CONF["k8s_entra_group_c1"] == "k8s-admins"
    finally:
        restore()


def test_enable_eks_associates_the_configured_provider():
    restore = _with_cfg(_OKTA)
    try:
        res = asyncio.run(k.enable_entra_federation(_FakeDB([_Row("c1", "aws")]), "c1"))
        assert res["provider"] == "okta" and res["config_name"] == "okta"
        (verb, name, region, kw), = _AWS_CALLS
        assert (verb, name, region) == ("associate", "k8s-demo", "us-east-2")
        assert kw["issuer_url"] == "https://example.okta.com/oauth2/default"
        assert kw["client_id"] == "0oaKUBE"
        assert kw["username_prefix"] == "okta:" and kw["groups_prefix"] == "okta:"
        assert kw["config_name"] == "okta"
        assert _CONF["k8s_entra_fed_c1"] == "okta"
    finally:
        restore()


def test_enable_refuses_to_switch_provider_in_place():
    # EKS takes one OIDC provider per cluster: an Entra-federated cluster has to be
    # disabled before it can trust Okta, or the associate fails half-way on AWS.
    restore = _with_cfg(_OKTA)
    try:
        _CONF["k8s_entra_fed_c1"] = "1"
        try:
            asyncio.run(k.enable_entra_federation(_FakeDB([_Row("c1", "aws")]), "c1"))
            raise AssertionError("expected K8sError when switching provider")
        except k.K8sError as e:
            assert "Entra ID" in str(e) and "Okta" in str(e)
        assert _AWS_CALLS == []
    finally:
        restore()


def test_disable_undoes_the_provider_it_was_federated_with():
    # Settings now name Okta, but the cluster was federated with Entra: the "entra"
    # identity-provider config is the one to disassociate.
    restore = _with_cfg(_OKTA)
    try:
        _CONF.update({"k8s_entra_fed_c1": "1", "k8s_entra_fed_eks_c1": "k8s-demo|us-east-2"})
        asyncio.run(k.disable_entra_federation(_FakeDB([_Row("c1", "aws")]), "c1"))
        assert _AWS_CALLS == [("disassociate", "k8s-demo", "us-east-2", "entra")]
        assert _CONF["k8s_entra_fed_c1"] == ""
    finally:
        restore()


def test_enable_aks_stays_native_entra():
    restore = _with_cfg(_OKTA)
    try:
        res = asyncio.run(k.enable_entra_federation(_FakeDB([_Row("c1", "azure")]), "c1"))
        assert res["native"] is True and res["provider"] == "entra"
        assert _AWS_CALLS == []
    finally:
        restore()


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
