"""Wiring tests for the Workload Lab's Kubernetes tab.

Text-and-registry checks rather than behaviour: every one of these pins a connection that
fails LATE and quietly if it is missed — a job type the worker will not claim, a tab that
renders without its Alpine factory, an auto-delete kind that silently never deletes a
ServiceAccount whose tokens keep authenticating.

No app imports, so it runs on a checkout without the requirements installed.
Runs under pytest or standalone:  python tests/test_workload_k8s_wiring.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _tab() -> str:
    """The Kubernetes tab as one string: the page shell plus its own partial."""
    return chr(10).join(
        _read("web_dashboard", "templates", "workload_lab", name)
        for name in ("index.html", "_kubernetes.html"))


# ── the job type is registered in all three places ────────────────────────────

def test_the_job_type_is_claimable_dispatched_and_tiered():
    """Three separate registries in jobs_worker, and missing any one fails differently.

    Absent from the handled-types tuple the worker never claims the row and the job sits
    queued forever; absent from the dispatch chain it is claimed and then errors as an
    unknown type; absent from every tier tuple `_TIER_OF[job_type]` raises a KeyError at
    claim time. None of the three is caught by anything else here.
    """
    worker = _read("web_dashboard", "jobs_worker.py")
    assert worker.count('"workload_k8s_token"') >= 3, (
        "workload_k8s_token must appear in the handled-types tuple, a tier tuple and the "
        f"dispatch chain; found {worker.count(chr(34) + 'workload_k8s_token' + chr(34))}")
    assert 'job_type == "workload_k8s_token"' in worker, "no dispatch branch"
    assert "workload_k8s_service.run(" in worker, "the dispatch branch calls nothing"


def test_the_job_is_medium_not_light():
    """It runs kubectl in-process AND a terraform for the managed system, so LIGHT would
    be wrong — that tier is for a parent awaiting HEAVY children, or a job with no local
    process at all. Pinned because the tier is a comment away from looking arbitrary, and
    the cost of getting it wrong (a MEDIUM-weight job holding a LIGHT slot) is a runner
    that oversubscribes its own plugin cache.

    Checked by POSITION: the tier tuples are ordered MEDIUM then LIGHT in the file, so the
    entry has to fall inside the MEDIUM one. Matching on a nearby comment would pass if
    somebody moved the entry and left the comment.
    """
    worker = _read("web_dashboard", "jobs_worker.py")
    medium = worker.index("MEDIUM_TYPES = (")
    light = worker.index("LIGHT_TYPES = (")
    assert medium < light, "the tier tuples are no longer ordered MEDIUM then LIGHT"
    # The tier registration is the occurrence between the two tuple headers.
    between = worker[medium:light]
    assert '"workload_k8s_token"' in between, (
        "workload_k8s_token is not in MEDIUM_TYPES — it applies RBAC with kubectl and "
        "creates a Password Safe managed system, so it is not a LIGHT job")
    # And absent from LIGHT. Both halves are needed: the entry could be added to LIGHT
    # without being removed from MEDIUM, and `_TIER_OF` is built by iterating the tiers in
    # order, so the later one silently wins — the first assertion alone would still pass.
    light_tuple = worker[light:worker.index(")", worker.index("\n)", light))]
    assert '"workload_k8s_token"' not in light_tuple, (
        "workload_k8s_token is ALSO in LIGHT_TYPES, which is the tier that actually "
        "applies — _TIER_OF is built tier by tier and the last one wins")


# ── the router is mounted and gated ───────────────────────────────────────────

def test_the_router_is_registered_and_flag_gated():
    main = _read("web_dashboard", "main.py")
    assert "from .api import workload_k8s as workload_k8s_api" in main
    assert "app.include_router(workload_k8s_api.router" in main
    # The route gate is k8s_management_enabled; password_safe_enabled cannot be expressed
    # there (one flag per gate) and is enforced per-endpoint instead.
    idx = main.index("app.include_router(workload_k8s_api.router")
    assert '_feature_gate("k8s_management_enabled")' in main[idx:idx + 400], (
        "the router is mounted without its feature gate, so it would serve on an "
        "instance where the Kubernetes page does not exist")


def test_every_endpoint_requires_both_flags():
    """`_require_enabled` checks k8s AND Password Safe, and every endpoint calls it.

    The router's own gate covers only the first. An endpoint that skipped this would
    onboard against a Password Safe the operator has switched off, and fail inside the
    plugin rather than at the click.
    """
    api = _read("web_dashboard", "api", "workload_k8s.py")
    assert "password_safe_enabled" in api and "k8s_management_enabled" in api
    # Count route decorators against calls to the gate. Equality rather than ">=": a new
    # endpoint added without the call is exactly the regression this pins.
    routes = api.count("@router.")
    gates = api.count("_require_enabled()")
    assert routes > 0, "no routes found — the assertion below would be vacuous"
    assert gates == routes + 1, (
        f"{routes} routes but {gates - 1} call _require_enabled() (plus its definition) — "
        f"every endpoint must require both flags")


def test_the_router_never_returns_a_token():
    """No endpoint may serve the credential, and `/consumer` is the one that would.

    The whole mechanism rests on the consumer retrieving with its OWN Password Safe client
    id, because that is what records which build read the token. An endpoint here that
    proxied the value would be a second, unaudited way to read it — and would make the
    audit trail say only that the dashboard retrieved.
    """
    api = _read("web_dashboard", "api", "workload_k8s.py")
    for banned in ("current_token", "_ps_sa_token", "get_managed_account_password",
                   "change_managed_account_password"):
        assert banned not in api, (
            f"api/workload_k8s.py references {banned!r} — nothing in this router may read "
            f"or return the credential")


# ── the tab renders and is self-contained ─────────────────────────────────────

def test_the_tab_is_self_contained_and_declares_its_alpine_factory():
    """Markup and factory in the one file, which is what test_template_scripts.py and
    test_templates_parse.py require of any template naming an x-data helper. An earlier
    draft of this page split a factory into a sibling file and broke both checkers."""
    partial = _read("web_dashboard", "templates", "workload_lab", "_kubernetes.html")
    assert 'x-data="workloadK8sTab()"' in partial
    assert "function workloadK8sTab()" in partial, (
        "the partial names an x-data helper it does not define")


def test_the_tab_is_gated_on_both_capabilities_and_not_on_a_new_preview_flag():
    """"Do not change the settings menu" still holds: Settings owns exactly two toggles
    for this page. The tab renders on the two CAPABILITY flags it needs and adds no third
    preview flag — and crucially does not join feature_flags._DERIVED, which would stop
    `workload_lab_enabled` resolving as all-preview and make
    tests/test_permission_catalog.py demand a new RBAC scope for the page."""
    shell = _read("web_dashboard", "templates", "workload_lab", "index.html")
    assert "{% if k8s_management_enabled and password_safe_enabled %}" in shell
    flags = _read("web_dashboard", "services", "feature_flags.py")
    derived = flags[flags.index("_DERIVED = {"):flags.index("_DERIVED = {") + 400]
    for non_preview in ("k8s_management_enabled", "password_safe_enabled"):
        assert non_preview not in derived, (
            f"{non_preview} was added to _DERIVED — workload_lab would stop resolving as "
            f"all-preview and the page would need an RBAC scope of its own")
    # And there is no third toggle in the Settings catalogue.
    assert "workload_k8s_enabled" not in flags


def test_the_two_kubernetes_surfaces_cross_link():
    """Which of the two paths an operator can have depends on who runs their control
    plane, so each has to point at the other. The SPIRE panel's link is the one that
    matters most: it is where somebody with a managed cluster finds out this path exists
    before spending an afternoon on the one that cannot work for them."""
    spire = _read("web_dashboard", "templates", "workload_lab", "_spire.html")
    k8s = _read("web_dashboard", "templates", "workload_lab", "_kubernetes.html")
    assert "$dispatch('select-tab', 'kubernetes')" in spire, (
        "the SPIRE tab does not point at the Kubernetes tab for managed clusters")
    assert "$dispatch('select-tab', 'spire')" in k8s, (
        "the Kubernetes tab does not point back at the SPIRE tab")
    # And the container has to listen, or both links are silently inert.
    shell = _read("web_dashboard", "templates", "workload_lab", "index.html")
    assert "@select-tab.window=" in shell, (
        "nothing listens for select-tab, so both cross-links do nothing when clicked")


def test_an_unknown_tab_slug_is_ignored():
    """The cross-links target tabs that may be switched off, so select() has to refuse a
    slug this render does not have. Without the guard, clicking through to a disabled tab
    sets activeTab to a slug with no panel and the page shows its header and nothing
    else — which reads as broken rather than as a feature being off."""
    shell = _read("web_dashboard", "templates", "workload_lab", "index.html")
    assert "if (!this.slugs.includes(slug)) return;" in shell, (
        "select() does not guard an unknown slug")


# ── inventory and the auto-delete timer ───────────────────────────────────────

def test_the_inventory_kind_is_emitted_and_reapable():
    """Four files have to agree, and a gap in any of them is silent.

    `inventory_service` has to emit the row; `expiry_policy` has to list the kind as
    reapable AND give it a reapable state, or the sweep refuses it; `expiry_reaper` needs
    BOTH a teardown branch and a `_resolve_row` prefix — without the second the page never
    renders Extend, so a row the sweep will destroy cannot be postponed. `certlab` and
    `spirelab` were each exactly that bug.
    """
    inv = _read("web_dashboard", "services", "inventory_service.py")
    assert "def _workloadk8s_item(" in inv and '"kind": "workloadk8s"' in inv
    # `items.append(...)`, not the bare call: `def _workloadk8s_item(row) -> dict:`
    # CONTAINS the string "_workloadk8s_item(row)", so matching on that alone passed while
    # the call site was deleted and the kind appeared on no page. Found by mutation
    # testing, which is the third time that exact shape of vacuous assertion has turned up
    # on this branch.
    assert "items.append(_workloadk8s_item(" in inv, "the item builder is never called"
    assert "/workload-lab#kubernetes" in inv, "the row has no detail_href to the tab"

    policy = _read("web_dashboard", "services", "expiry_policy.py")
    assert '"workloadk8s"' in policy[policy.index("REAPABLE_KINDS = ("):
                                     policy.index("REAPABLE_KINDS = (") + 300]
    assert '"workloadk8s": frozenset(' in policy, "the kind has no reapable state set"

    reaper = _read("web_dashboard", "services", "expiry_reaper.py")
    assert 'kind == "workloadk8s"' in reaper, "the reaper has no teardown branch"
    assert 'prefix == "workloadk8s"' in reaper, (
        "_resolve_row has no branch, so the page cannot render Extend for a kind the "
        "sweep will happily destroy")
    assert "workload_k8s_service.start_decommission(" in reaper


def test_the_reapable_state_is_active_only():
    """A FAILED onboard may have created the ServiceAccount and binding without ever
    reaching Password Safe, so the row names no managed account to deregister while the
    in-cluster half really is there. Destroying on that record leaves the binding behind
    and reports success."""
    policy = _read("web_dashboard", "services", "expiry_policy.py")
    start = policy.index('"workloadk8s": frozenset(')
    entry = policy[start:policy.index("}", start) + 1]
    assert '"active"' in entry
    assert '"failed"' not in entry, (
        "a failed onboard must not be reapable — see the comment in expiry_policy")


def test_the_inventory_page_labels_the_kind():
    """A missing entry is not an error — the raw kind renders — but 'workloadk8s' in the
    Kind column is what this pins against, and it is what 'certlab' and 'pov' each did."""
    page = _read("web_dashboard", "templates", "inventory", "list.html")
    assert "workloadk8s:" in page, "the Kind column would render the raw slug"


# ── the model holds no credential ─────────────────────────────────────────────

def test_the_model_stores_no_credential_and_no_kubeconfig():
    """The rule both sibling models state. A kubeconfig column is the specific temptation
    here: it would be a copy of the credential with no expiry and no audit trail, which is
    the artefact this whole feature exists to remove."""
    db = _read("web_dashboard", "database.py")
    start = db.index("class WorkloadK8sToken(Base):")
    # The next TOP-LEVEL class, anchored at column 0. Searching for a bare "class " cut the
    # slice short at the phrase "the class docstring" inside a column comment, which
    # silently dropped the last third of the model — and the "is missing" half of this test
    # is what noticed. A slice that ends early makes the banned-word half weaker too.
    end = db.index(chr(10) + "class ", start + 10)
    model = db[start:end]
    # COLUMN DECLARATIONS ONLY, not the whole class body. The docstring explains at length
    # why there is deliberately no kubeconfig column, so scanning the prose finds the very
    # word it is promising the absence of — a checker reading commentary as code, which is
    # a false positive that would have been "fixed" by weakening the rule.
    columns = [ln.strip() for ln in model.splitlines() if "= Column(" in ln]
    assert columns, "no column declarations found — the assertion below would be vacuous"
    declared = chr(10).join(columns).lower()
    for banned in ("kubeconfig", "password", "bearer", "credential", "token_value"):
        assert banned not in declared, (
            f"WorkloadK8sToken declares a column matching {banned!r} — the row carries "
            f"ids and names only")
    # `ps_account_name` is `<ns>/<sa>`, a NAME, and `ps_tf_state` is scrubbed state. Both
    # are allowed and neither matches the list above, which is the point of scanning the
    # declarations rather than the free text around them.
    # The ids and the state that teardown needs, though, must be there.
    for needed in ("ps_account_id", "ps_system_id", "ps_tf_state", "expires_at",
                   "profile", "namespace", "service_account"):
        assert needed in model, f"WorkloadK8sToken is missing {needed}"


def test_the_docs_and_persona_are_wired():
    readme = _read("docs", "README.md")
    assert "workload-kubernetes.md" in readme, "the guide is in no index"
    guide = _read("docs", "workload-kubernetes.md")
    # The four boundaries the design says to state plainly. Each is a claim somebody would
    # otherwise make wrongly in a demo.
    assert "rotation does not revoke" in guide.lower()
    assert "whoever can retrieve" in guide.lower()
    assert "600" in guide, "the TokenRequest floor is not stated"
    assert "cannot revoke certificates" in guide.lower()
    # And it cross-links with the SPIFFE guide both ways.
    assert "spiffe.md" in guide
    assert "workload-kubernetes.md" in _read("docs", "spiffe.md")
    personas = _read("web_dashboard", "services", "personas.py")
    assert "devops-workload-cluster-token" in personas
    assert "/workload-lab#kubernetes" in personas


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
