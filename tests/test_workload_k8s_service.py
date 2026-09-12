"""Behaviour tests for workload_k8s_service — the RBAC is what is under test.

The token is not the interesting part of this feature and neither is the plumbing. What
makes a vaulted ServiceAccount token worth demonstrating is that it is SCOPED, so these
pin the scoping and the two ways it could silently stop being true:

  * a Deployer bound with a ClusterRoleBinding instead of a RoleBinding would grant write
    access to the whole cluster, and would pass every other check in the consumer play;
  * anything reaching `ps_k8s_token_service.register` or `_entitle_k8s_rbac_manifest` would
    drag in a ClusterRoleBinding to **cluster-admin**, which is the one thing this feature
    exists not to do.

Plus the input validation, because namespace and ServiceAccount come off an HTTP request
and are interpolated into a YAML manifest.

Runs under pytest or standalone:  python tests/test_workload_k8s_service.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import yaml
    from web_dashboard.services import workload_k8s_service as svc
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _docs(profile, namespace="ci", sa="deploy-bot"):
    """The rendered manifest, parsed. Parsed rather than string-matched: the difference
    between a RoleBinding and a ClusterRoleBinding is one word in one field, and a
    substring check for 'RoleBinding' matches BOTH — which is exactly how this test could
    have passed while the Deployer granted cluster-wide write."""
    text = svc.workload_rbac_manifest(profile=profile, namespace=namespace,
                                      service_account=sa)
    return [d for d in yaml.safe_load_all(text) if d]


def _of_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


# ── the profiles bind what they claim ─────────────────────────────────────────

def test_the_deployer_is_namespace_scoped():
    """A RoleBinding pointing at a ClusterRole grants that role INSIDE the binding's own
    namespace only. That is the entire mechanism behind the Deployer's refusal elsewhere,
    and a ClusterRoleBinding to `edit` would look identical in the namespace it is meant
    to work in."""
    docs = _docs("deployer", namespace="ci")
    assert not _of_kind(docs, "ClusterRoleBinding"), (
        "the Deployer renders a ClusterRoleBinding — that grants `edit` on the WHOLE "
        "cluster and passes every assertion the namespace-scoped version does")
    bindings = _of_kind(docs, "RoleBinding")
    assert len(bindings) == 1, f"expected one RoleBinding, got {len(bindings)}"
    b = bindings[0]
    assert b["metadata"]["namespace"] == "ci", (
        "the RoleBinding carries no namespace, so it would be created wherever kubectl's "
        "current context points")
    assert b["roleRef"]["kind"] == "ClusterRole"
    assert b["roleRef"]["name"] == "edit", b["roleRef"]


def test_the_reader_is_cluster_wide_and_read_only():
    docs = _docs("reader", namespace="fleet", sa="scanner")
    assert not _of_kind(docs, "RoleBinding"), (
        "the Reader renders a RoleBinding, so a fleet scan could not read cluster-scoped "
        "resources at all — Nodes among them")
    bindings = _of_kind(docs, "ClusterRoleBinding")
    assert len(bindings) == 1
    assert bindings[0]["roleRef"]["name"] == "view", bindings[0]["roleRef"]


def test_no_profile_binds_a_write_or_admin_role():
    """The property that has to hold for every profile, present and future. `view` omits
    Secrets upstream and `edit` cannot touch RBAC; `admin`, `cluster-admin` and a wildcard
    Role are each a different way of losing the whole point."""
    for profile in svc.VALID_PROFILES:
        for d in _docs(profile):
            ref = d.get("roleRef") or {}
            assert ref.get("name") not in ("cluster-admin", "admin"), (
                f"{profile} binds {ref.get('name')!r}")
            # A Role or ClusterRole DEFINED here would be this feature's own opinion about
            # what a consumer needs, re-auditable on every Kubernetes release. Both
            # profiles reference an upstream default instead.
            assert d.get("kind") not in ("Role", "ClusterRole"), (
                f"{profile} defines its own {d.get('kind')} rather than referencing an "
                f"upstream default")


def test_the_subject_is_the_service_account_and_carries_its_namespace():
    """A ServiceAccount subject MUST have a namespace and MUST NOT have an apiGroup. Both
    mistakes are accepted silently by Kubernetes and match nothing — the binding exists,
    reads correctly, and grants the token nothing at all."""
    for profile in svc.VALID_PROFILES:
        docs = _docs(profile, namespace="ci", sa="deploy-bot")
        binding = (_of_kind(docs, "RoleBinding") + _of_kind(docs, "ClusterRoleBinding"))[0]
        subjects = binding["subjects"]
        assert len(subjects) == 1, subjects
        s = subjects[0]
        assert s["kind"] == "ServiceAccount"
        assert s["name"] == "deploy-bot"
        assert s["namespace"] == "ci", "a ServiceAccount subject needs its namespace"
        assert "apiGroup" not in s, (
            "a ServiceAccount subject must NOT carry an apiGroup — it is accepted and "
            "matches nothing")


def test_the_manifest_creates_the_identity_and_no_secret():
    """The ServiceAccount and its namespace, and nothing else. In bound mode the API server
    mints through the TokenRequest API, so a token Secret here would be a long-lived
    credential in the cluster that the plugin's label-scoped sweep never collects."""
    for profile in svc.VALID_PROFILES:
        docs = _docs(profile)
        kinds = sorted(d["kind"] for d in docs)
        assert "Secret" not in kinds, f"{profile} renders a Secret: {kinds}"
        assert _of_kind(docs, "ServiceAccount"), f"{profile} creates no ServiceAccount"
        assert _of_kind(docs, "Namespace"), (
            f"{profile} does not create the namespace, so onboarding into one that does "
            f"not exist yet fails on the ServiceAccount")


def test_two_identities_on_one_cluster_do_not_collide():
    """A ClusterRoleBinding name is CLUSTER-scoped. Two Readers whose ServiceAccounts share
    a name in different namespaces would otherwise write the same object, and the second
    onboard would silently re-point the first — the first identity's token keeps working
    and starts reading through somebody else's grant."""
    a = _docs("reader", namespace="ci", sa="scanner")
    b = _docs("reader", namespace="ops", sa="scanner")
    name_a = _of_kind(a, "ClusterRoleBinding")[0]["metadata"]["name"]
    name_b = _of_kind(b, "ClusterRoleBinding")[0]["metadata"]["name"]
    assert name_a != name_b, (
        f"both render the binding {name_a!r} — the second onboard would overwrite the first")
    # And the two profiles must not collide either, for the same reason.
    dep = _of_kind(_docs("deployer", namespace="ci", sa="scanner"), "RoleBinding")[0]
    assert dep["metadata"]["name"] != name_a


def test_binding_names_are_legal_kubernetes_names():
    """Rendered from operator input, so they have to stay DNS-1123 even at the length cap —
    a truncation landing on a dash produces a name the API server refuses, and the failure
    arrives at apply time with everything else already correct."""
    label = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    long_ns, long_sa = "n" * 60, "s" * 60
    for profile in svc.VALID_PROFILES:
        docs = _docs(profile, namespace=long_ns, sa=long_sa)
        binding = (_of_kind(docs, "RoleBinding") + _of_kind(docs, "ClusterRoleBinding"))[0]
        name = binding["metadata"]["name"]
        assert len(name) <= 63, f"{profile}: {len(name)} characters"
        assert label.match(name), f"{profile}: {name!r} is not a DNS-1123 label"


# ── input validation ─────────────────────────────────────────────────────────

def test_a_namespace_cannot_inject_yaml():
    """Request-supplied and interpolated into a manifest. Without validation a namespace of
    "default\\n  foo: bar" adds arbitrary keys to the ServiceAccount it renders."""
    for bad in ("default\n  foo: bar", "Default", "has space", "-leading", "trailing-",
                "", "n" * 64, "ns/../other"):
        for field in ("namespace", "service_account"):
            kwargs = {"profile": "deployer", "namespace": "ci",
                      "service_account": "deploy-bot", field: bad}
            try:
                svc.workload_rbac_manifest(**kwargs)
            except svc.WorkloadK8sError:
                continue
            raise AssertionError(f"{field}={bad!r} was accepted")


def test_an_unknown_profile_is_refused():
    """Note what is NOT on this list: "Deployer" and "deployer ". Casing and surrounding
    whitespace are NORMALISED rather than refused, which is deliberate and is checked
    separately below — the property that matters is not strictness, it is that the renderer
    and `onboard` normalise identically. A name that is not a profile at all is a different
    matter and must be refused, which is what this pins."""
    for bad in ("", "admin", "cluster-admin", None, "reader-plus", "edit", "view",
                "deploy", "deployer,reader"):
        try:
            svc.workload_rbac_manifest(profile=bad, namespace="ci",
                                        service_account="deploy-bot")
        except svc.WorkloadK8sError:
            continue
        raise AssertionError(f"profile={bad!r} was accepted")


def test_profile_casing_is_normalised_the_same_way_everywhere():
    """`onboard` lowercases before it validates and stores; the manifest renderer and
    `profile_summary` lowercase before they look up. If any one of the three did not, a
    request for "Deployer" would either be refused at one end and accepted at the other, or
    stored in a casing the renderer cannot resolve — and the failure would land at apply
    time on a row that looks fine.

    Read off the SOURCE for `onboard`, which needs a database and a cluster to run."""
    mixed = _docs("  Deployer ")
    plain = _docs("deployer")
    assert [d["kind"] for d in mixed] == [d["kind"] for d in plain], (
        "the renderer resolves '  Deployer ' to something other than 'deployer'")
    assert svc.profile_summary("READER") == svc.profile_summary("reader") != ""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "workload_k8s_service.py"), encoding="utf-8").read()
    onboard = src[src.index("def onboard("):src.index("def start_rotate(")]
    assert 'profile = (profile or "").strip().lower()' in onboard, (
        "onboard does not normalise the profile the way the renderer does, so a row can "
        "be stored in a casing the manifest lookup cannot resolve")


# ── the cluster-admin seed stays unreachable ─────────────────────────────────

def test_the_service_never_reaches_the_cluster_admin_seed():
    """The one thing this module must not do, and the reason it does not call
    `ps_k8s_token_service.register`.

    That function's step 2 reads a "current token" as a seed, and on a first registration
    that applies `k8s_service._entitle_k8s_rbac_manifest` — a ClusterRoleBinding to
    cluster-admin — purely to obtain a value it then discards, because a bearer token
    exceeds Password Safe's 128-character create cap. Checked against the SOURCE rather
    than by behaviour: the call would only happen on a first onboard against a real
    cluster, which no test here reaches, so a behavioural check would pass while the
    dangerous path sat one branch away.
    """
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "workload_k8s_service.py"), encoding="utf-8").read()
    # Comments and the module docstring explain at length why these are avoided, so the
    # prose contains every name. Strip it and scan the CODE — the same distinction the
    # model test in test_workload_k8s_wiring.py had to make.
    code = "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for banned in ("_entitle_k8s_rbac_manifest", "_mint_pra_sa_token",
                   "_resolve_pra_sa_token", "cluster-admin",
                   "_register_pravault_mirror", "_reconcile_synced_link"):
        assert banned not in code, (
            f"workload_k8s_service calls {banned!r} — that path binds cluster-admin or "
            f"mirrors to PRA, neither of which belongs here")
    # `ps_k8s_token_service.register` specifically. Matched with a word boundary so the
    # module's legitimate use of `register_managed_system` is not a false positive.
    assert not re.search(r"ps_k8s_token_service\.register\b", code), (
        "workload_k8s_service calls ps_k8s_token_service.register — its seeding step "
        "applies a ClusterRoleBinding to cluster-admin")


def test_the_service_reuses_the_seams_it_is_supposed_to():
    """The counterpart of the test above: skipping `register()` must not mean
    reimplementing the per-cloud address resolution, which is where the GKE
    zone-versus-region trap lives, or the rotator RBAC, which also does the cloud-side
    identity mapping a rotation needs."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "workload_k8s_service.py"), encoding="utf-8").read()
    for needed in ("ps_k8s_token_service._address_for",
                   "ps_k8s_token_service._apply_rbac",
                   "ps_k8s_token_service._rotate_token_once",
                   "ps_resource_service.register_managed_system",
                   "ps_resource_service._validate_k8ssa_dns_name",
                   "k8s_service.resolve_kubeconfig"):
        assert needed in src, f"{needed} is not reused — see the module docstring"


def test_the_managed_system_is_created_without_a_seed():
    """`initial_password` is what would require the cluster-admin binding. The call must
    not pass one — `register_managed_system` accepts a placeholder, and the rotation on
    register is what fills the account."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "workload_k8s_service.py"), encoding="utf-8").read()
    call = src[src.index("ps_resource_service.register_managed_system("):]
    call = call[:call.index(")\n")]
    assert "initial_password" not in call, (
        "the managed system is created with a seed, which is the branch that costs a "
        "cluster-admin ClusterRoleBinding to obtain")
    assert 'method="k8ssa"' in call, "the wrong Password Safe plugin method"


# ── the address ──────────────────────────────────────────────────────────────

def test_the_address_carries_bound_and_the_ttl_floor():
    """The plugin parses this string, and `bound` plus a ttl is what makes the token
    short-lived at all. 600 is the TokenRequest API's own floor — a cluster silently caps
    anything lower, so a smaller ask must come back clamped rather than honoured."""
    from web_dashboard.services import ps_k8s_token_service as ps
    addr = ps.build_address(cloud="aws", region="us-east-2", cluster_name="prod",
                            mode="bound", ttl_seconds=60, namespace="ci")
    parts = addr.split(";")
    assert parts[0] == "eks" and "bound" in parts, addr
    ttl = [p for p in parts if p.startswith("ttl=")]
    assert ttl, f"no ttl in {addr}"
    assert int(ttl[0].split("=")[1]) >= 600, (
        f"{ttl[0]} is below the TokenRequest floor — the cluster would cap it silently")


def test_the_profiles_describe_themselves_for_the_page():
    """The tab and the API render `profile_summary` rather than their own wording, so the
    page cannot claim a binding the service does not create. Each summary has to name the
    binding kind it really uses."""
    assert "RoleBinding" in svc.profile_summary("deployer")
    assert "ClusterRoleBinding" not in svc.profile_summary("deployer")
    assert "ClusterRoleBinding" in svc.profile_summary("reader")
    assert svc.profile_summary("nonexistent") == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
