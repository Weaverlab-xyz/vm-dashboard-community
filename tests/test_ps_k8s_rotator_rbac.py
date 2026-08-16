"""Unit tests for the Password Safe rotator RBAC manifest + the ownership handoff in
k8s_service.

The manifest is transcribed from the rotation plugin's scripts/rbac.yaml, and the
least-privilege claim is the part worth pinning: Bound mode's ClusterRole must carry NO
rule on Secrets at all — it rotates via the TokenRequest subresource, so granting it
Secret reads would silently widen what the functional account can exfiltrate from every
namespace.

The handoff (`_resolve_pra_sa_token`) is the other invariant: once a cluster's token is
a Password Safe managed account, the dashboard must READ instead of MINT — a minted
Secret carries no plugin labels, so the rotation sweep never deletes it and it stays a
permanent, unrotatable cluster-admin credential.

Stubs the DB / sqlalchemy imports so k8s_service loads without an app/DB (same as
test_entra_group). Runs under pytest or standalone.
"""
import asyncio
import os
import sys
import types

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
_db.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db)

from web_dashboard.services import k8s_service as k  # noqa: E402


# ── the ClusterRole rules ────────────────────────────────────────────────────────

def test_longlived_carries_the_secret_lifecycle_verbs():
    m = k._ps_rotator_rbac_manifest(mode="longlived", subject_kind="User",
                                    subject_name="u@example.com")
    assert "name: password-safe-token-rotator\n" in m
    assert '"secrets"' in m
    for verb in ("create", "get", "list", "delete"):
        assert f'"{verb}"' in m


def test_bound_has_no_secrets_rule_at_all():
    # The least-privilege property that makes Bound worth choosing.
    m = k._ps_rotator_rbac_manifest(mode="bound", subject_kind="User",
                                    subject_name="u@example.com")
    assert "password-safe-token-rotator-bound" in m
    assert '"secrets"' not in m
    assert '"serviceaccounts/token"' in m


def test_both_modes_can_read_serviceaccounts():
    for mode in ("longlived", "bound"):
        m = k._ps_rotator_rbac_manifest(mode=mode, subject_kind="User", subject_name="x")
        assert '"serviceaccounts"' in m


# ── the binding subject ──────────────────────────────────────────────────────────

def test_a_user_subject_carries_the_rbac_api_group():
    m = k._ps_rotator_rbac_manifest(mode="longlived", subject_kind="User",
                                    subject_name="psafe@proj.iam.gserviceaccount.com")
    assert "kind: ClusterRoleBinding" in m
    assert "- kind: User\n  name: psafe@proj.iam.gserviceaccount.com\n" in m
    # A User/Group subject without apiGroup is silently accepted and matches nothing.
    assert "apiGroup: rbac.authorization.k8s.io" in m.split("subjects:")[1]


def test_a_serviceaccount_subject_carries_a_namespace_and_no_api_group():
    m = k._ps_rotator_rbac_manifest(mode="longlived", subject_kind="ServiceAccount",
                                    subject_name="password-safe-rotator",
                                    subject_namespace="beyondtrust")
    subjects = m.split("subjects:")[1]
    assert "namespace: beyondtrust" in subjects
    # The inverse mistake: a ServiceAccount subject with the RBAC apiGroup is invalid.
    assert "apiGroup" not in subjects


def test_no_subject_means_role_only_no_binding():
    # Applying the role alone is harmless and idempotent; Password Safe's own Verify
    # Functional Account then names what is still missing.
    m = k._ps_rotator_rbac_manifest(mode="longlived", subject_kind="User", subject_name="")
    assert "kind: ClusterRole\n" in m
    assert "ClusterRoleBinding" not in m


# ── the per-cloud subject sources ────────────────────────────────────────────────

def _with_cfg(values, fn):
    orig = k._cfg
    k._cfg = lambda key, default="": values.get(key, default)
    try:
        return fn()
    finally:
        k._cfg = orig


def test_the_subject_kind_and_config_key_per_cloud():
    kind, name, ns = _with_cfg({"k8s_ps_rotator_aks_sp_object_id": "oid-123"},
                               lambda: k._ps_rotator_subject("azure"))
    assert (kind, name) == ("User", "oid-123")

    kind, name, ns = _with_cfg({}, lambda: k._ps_rotator_subject("aws"))
    assert (kind, name) == ("User", "passwordsafe-rotator"), \
        "EKS defaults to the documented access-entry username"

    kind, name, ns = _with_cfg({}, lambda: k._ps_rotator_subject("local"))
    assert kind == "ServiceAccount" and name == "password-safe-rotator"
    assert ns == "beyondtrust"

    kind, name, ns = _with_cfg({}, lambda: k._ps_rotator_subject("gcp"))
    assert (kind, name) == ("User", ""), \
        "GKE's subject is derived from the functional account when config is blank"


# ── the ownership handoff ────────────────────────────────────────────────────────

class _Row:
    def __init__(self, ps_token_account_id=None):
        self.id = "c-1"
        self.cloud = "gcp"
        self.ps_token_account_id = ps_token_account_id


def test_a_managed_cluster_reads_from_password_safe_and_never_mints():
    calls = []

    async def _fake_mint(kubeconfig, target_cloud=""):
        calls.append("mint")
        return "minted-token"

    fake_svc = types.ModuleType("web_dashboard.services.ps_k8s_token_service")

    async def _fake_current(db, cluster_id):
        calls.append("checkout")
        return "ps-held-token"
    fake_svc.current_token = _fake_current
    sys.modules["web_dashboard.services.ps_k8s_token_service"] = fake_svc

    orig = k._mint_pra_sa_token
    k._mint_pra_sa_token = _fake_mint
    try:
        token, source = asyncio.run(k._resolve_pra_sa_token(None, _Row("42"), "kubecfg"))
        assert (token, source) == ("ps-held-token", "password_safe")
        assert calls == ["checkout"], (
            "minting for a managed cluster creates an unlabelled Secret the rotation "
            "sweep never deletes — a permanent cluster-admin credential")

        calls.clear()
        token, source = asyncio.run(k._resolve_pra_sa_token(None, _Row(None), "kubecfg"))
        assert (token, source) == ("minted-token", "minted")
        assert calls == ["mint"]
    finally:
        k._mint_pra_sa_token = orig
        del sys.modules["web_dashboard.services.ps_k8s_token_service"]


def test_the_mint_defaults_agree_between_mint_and_revoke():
    """k8s_service.py once read pra_k8s_namespace with default 'kube-system' at mint and
    'pra-access' at revoke — inert while config.py supplied the value first, but arming
    the key in Settings would have made mint and revoke target different namespaces,
    leaving a live cluster-admin token behind. Pin the literals to each other."""
    import re
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py"),
               encoding="utf-8").read()
    defaults = set(re.findall(r"""_cfg\(["']pra_k8s_namespace["'],\s*["']([^"']+)["']\)""",
                              src))
    assert defaults == {"pra-access"}, f"pra_k8s_namespace defaults disagree: {defaults}"


def test_deregister_pra_tunnel_skips_the_revoke_when_ps_managed():
    """Source-level: the SA-revoke block must branch on ps_token_account_id — the plugin
    binds every token to the ServiceAccount's uid, so delete+recreate invalidates every
    token Password Safe has issued."""
    import inspect
    src = inspect.getsource(k.deregister_pra_tunnel)
    assert "ps_token_account_id" in src
    assert src.index("ps_token_account_id") < src.index("_delete_manifest_via_runner")
    assert "pra_vault_account_id" in src
    # And it must NOT unlink the synced pair. The "PRA Vault Token" plugin resolves its
    # account by NAME (a stable k8s-<cluster>-sa), so re-provisioning the tunnel
    # re-creates the account the existing link already points at and the pair resumes on
    # its own. Unlinking here would trade that for a manual re-registration.
    assert "unlink_synced_account" not in src, (
        "removing the tunnel must not unlink the Password Safe pair — the link is what "
        "makes a re-provisioned tunnel resume syncing without operator action")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
