"""The Portainer page's "Just-in-time access (Entitle)" card.

The pairing logic is pinned in test_portainer_adapter_pairing.py and the adapter's own
contract in test_portainer_adapter.py. What is pinned HERE is the wiring that makes it
reachable safely from a button — the seams that span Python, Jinja and JS, where
nothing fails at import time and the first sign of a mistake is a terraform apply
against real cloud resources, or a Portainer API token staged in a cloud secret store
for a function nothing will ever call:

  * the routes existing, gated, and pre-flighted BEFORE anything is queued
  * the job type being registered in all four places the worker reads
  * the card reading only fields the API actually returns
  * the node firewall admitting the adapter, and the teardown taking it away again

Source/AST checks, like the sibling suites: these are the assertions a unit test of
either half alone cannot make. Under pytest or standalone:
    python tests/test_portainer_adapter_button.py
"""
import ast
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "containers", "index.html")
_API = os.path.join(_ROOT, "web_dashboard", "api", "containers.py")
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "portainer_adapter_service.py")
_NODE = os.path.join(_ROOT, "web_dashboard", "services", "portainer_node_service.py")
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")
_MODELS = os.path.join(_ROOT, "web_dashboard", "models", "containers.py")
_CONFIG = os.path.join(_ROOT, "web_dashboard", "config.py")
_WORKLOAD = os.path.join(_ROOT, "web_dashboard", "functions", "fnworkloads",
                         "portainer_access.py")

JOB_TYPE = "portainer_adapter_pair"


def _read(path):
    return open(path, encoding="utf-8").read()


def _route(decorator):
    """A handler's source, anchored on its route decorator — the path string alone
    also appears in the module's route list up top."""
    api = _read(_API)
    assert decorator in api, f"no such route decorator: {decorator}"
    return api.split(decorator)[1].split("\n@router.")[0]


# ── The routes exist, and are gated ──────────────────────────────────────────

def test_all_three_routes_exist():
    api = _read(_API)
    for decorator in ('@router.get("/portainer/adapter"',
                      '@router.post("/portainer/adapter-pair"',
                      '@router.post("/portainer/adapter-retire"'):
        assert decorator in api, decorator


def test_the_status_route_is_read_only_and_the_others_are_not():
    assert 'require_permission("containers", "read")' in _route(
        '@router.get("/portainer/adapter"')
    assert 'require_permission("containers", "write")' in _route(
        '@router.post("/portainer/adapter-pair"')
    assert 'require_permission("containers", "delete")' in _route(
        '@router.post("/portainer/adapter-retire"')


def test_deploying_the_adapter_needs_the_cloud_function_scope():
    """The pairing writes a cloud_functions row and runs a real Terraform apply — the
    same thing POST /api/functions does. Without this a holder of containers:write
    alone would have a way around that scope."""
    api = _read(_API)
    assert "def _require_function_write(" in api
    assert "cloud_function" in api.split("def _require_function_write(")[1][:900]
    for decorator in ('@router.post("/portainer/adapter-pair"',
                      '@router.post("/portainer/adapter-retire"'):
        assert "_require_function_write(current_user)" in _route(decorator), decorator


def test_admins_and_unrestricted_users_pass_the_scope_check():
    """{} / NULL permissions mean unrestricted in this app (legacy installs), so the
    check must not turn into a lockout."""
    body = _read(_API).split("def _require_function_write(")[1][:900]
    assert "is_effective_admin" in body
    assert "if perms and" in body


# ── Nothing is queued before the pre-flights run ─────────────────────────────

def test_every_preflight_runs_before_anything_is_queued():
    body = _route('@router.post("/portainer/adapter-pair"')
    for check in ("_require_function_write", "preflight", "find_adapter"):
        assert body.index(check) < body.index("start_pairing"), check


def test_the_endpoint_refuses_a_duplicate_rather_than_redeploying():
    """cloud_function_service.deploy does NOT look a name up and every deploy starts
    from an empty Terraform directory, so a second pairing leaves a duplicate row
    wedged in 'deploying' behind an "already exists" apply failure. The name is
    deterministic, so the caller has to ask first."""
    body = _route('@router.post("/portainer/adapter-pair"')
    assert "find_adapter(db)" in body
    assert "status_code=409" in body


def test_the_preflight_failure_is_a_409_not_a_500():
    """AdapterPairingError carries a message written for the operator; letting it
    escape would bury it in a traceback."""
    body = _route('@router.post("/portainer/adapter-pair"')
    assert "except adapter.AdapterPairingError" in body
    assert "status_code=409" in body


def test_retiring_is_unconditional():
    """An adapter that should not have been deployed is exactly the one that most
    needs removing, so the retire route pre-flights nothing."""
    body = _route('@router.post("/portainer/adapter-retire"')
    assert "preflight" not in body
    assert "ineligible_reason" not in body


# ── The job type, in all four places the worker reads ────────────────────────

def test_the_job_type_is_allowed():
    worker = _read(_WORKER)
    allowed = worker.split("HANDLED_TYPES = (")[1].split("\n)")[0]
    assert f'"{JOB_TYPE}"' in allowed, "the worker would refuse to run the job"


def test_the_job_type_is_heavy():
    """It drives cloudfn_deploy's terraform apply inline, exactly as
    clouddb_adapter_pair does — a light slot would starve the rest of the worker."""
    heavy = _read(_WORKER).split("HEAVY_TYPES = (")[1].split("\n)")[0]
    assert f'"{JOB_TYPE}"' in heavy


def test_the_job_type_is_a_singleton():
    """The pairing refuses to redeploy an adapter that already exists. Two concurrent
    pairings would both look, both find nothing, and both deploy."""
    singleton = _read(_WORKER).split("SINGLETON_TYPES = frozenset((")[1].split("\n))")[0]
    assert f'"{JOB_TYPE}"' in singleton


def test_the_dispatch_branch_calls_the_service():
    worker = _read(_WORKER)
    assert f'job_type == "{JOB_TYPE}"' in worker
    branch = worker.split(f'job_type == "{JOB_TYPE}"')[1].split("elif job_type")[0]
    assert "portainer_adapter_service.run_job(" in branch
    assert "job_id=job_id" in branch and "meta=meta" in branch


def test_the_service_exposes_exactly_what_the_worker_and_the_api_call():
    """An AST check rather than a grep: a renamed function still imports fine and only
    fails when an operator clicks the button."""
    tree = ast.parse(_read(_SVC))
    names = {n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for required in ("run_job", "start_pairing", "start_retire", "status",
                     "find_adapter", "adapter_name", "preflight",
                     "ineligible_reason", "retire_adapter"):
        assert required in names, required


def test_both_job_directions_go_through_one_type():
    """Retire reuses the pair job type with action="retire", so run_job has to branch
    on it — a missing branch would silently PAIR on a remove click."""
    body = _read(_SVC).split("async def run_job(")[1].split("\nasync def ")[0]
    assert '"retire"' in body
    assert "_run_retire" in body and "_run_pair" in body
    assert '"action"' in _read(_SVC).split("def start_retire(")[1][:700]


# ── The workload the pairing deploys ─────────────────────────────────────────

def test_the_workload_name_matches_the_shipped_workload():
    """ADAPTER_WORKLOAD is a string, and a typo in it deploys nothing while the
    lookup finds nothing — a pairing that reports success and grants forever."""
    svc = _read(_SVC)
    assert 'ADAPTER_WORKLOAD = "portainer_access"' in svc
    assert os.path.exists(_WORKLOAD), "the portainer_access workload is missing"
    assert 'NAME = "portainer_access"' in _read(_WORKLOAD)


def test_the_workload_really_serves_the_entitle_contract():
    """start_entitle_register refuses a workload that is not an adapter, so registering
    a non-adapter would fail late. This is the same flag it reads."""
    assert "ENTITLE_ADAPTER = True" in _read(_WORKLOAD)


def test_every_env_key_the_pairing_sets_is_one_the_workload_reads():
    svc = _read(_SVC)
    workload = _read(_WORKLOAD)
    for key in sorted(set(re.findall(r'"(FN_PORTAINER_[A-Z_]+)"', svc))):
        assert key in workload, f"{key} is set by the pairing but unread by the workload"


def test_the_workloads_required_env_is_satisfied():
    """_check_required_env rejects a deploy that omits it, and the pairing is the only
    caller that can supply it."""
    required = re.search(r"REQUIRED_ENV = \(([^)]*)\)", _read(_WORKLOAD)).group(1)
    svc = _read(_SVC)
    for key in re.findall(r'"(FN_[A-Z_]+)"', required):
        assert key in svc, key


# ── The card ─────────────────────────────────────────────────────────────────

def _card():
    page = _read(_PAGE)
    assert "Just-in-time access (Entitle)" in page, "the card is missing"
    # The heading appears twice (the section comment and the h3), so slice the region
    # rather than splitting on it — [1] would land between the two and see almost none
    # of the markup, which is a test that passes by looking at nothing.
    return page[page.index("Just-in-time access (Entitle)"):
                page.index("Connect a Docker host")]


def test_the_card_offers_deploy_only_when_the_api_says_it_is_viable():
    card = _card()
    assert 'x-show="portainerAdapter.viable && !portainerAdapter.fn_id"' in card


def test_an_unavailable_card_says_why_instead_of_showing_a_dead_button():
    card = _card()
    assert "portainerAdapter.ineligible_reason" in card


def test_every_field_the_card_reads_is_one_the_api_declares():
    """The single most likely drift: a renamed response field leaves an x-show reading
    undefined, which Alpine treats as false — so the control silently disappears."""
    declared = _read(_MODELS).split("class PortainerAdapterResponse(BaseModel):")[1]
    declared = declared.split("\nclass ")[0]
    for field in sorted(set(re.findall(r"portainerAdapter\.([a-z_]+)", _card()))):
        assert f"{field}:" in declared, f"the card reads .{field}, the API never sends it"


def test_the_status_route_returns_that_model():
    body = _route('@router.get("/portainer/adapter"')
    assert "PortainerAdapterResponse(**portainer_adapter_service.status(db))" in body


def test_every_status_key_the_model_declares_is_one_the_service_produces():
    """The response model is built by **-splatting the service's dict, so a field the
    service stops producing is a TypeError at request time, not at import."""
    declared = _read(_MODELS).split("class PortainerAdapterResponse(BaseModel):")[1]
    declared = declared.split("\nclass ")[0]
    fields = set(re.findall(r"^    ([a-z_]+):", declared, re.M))
    produced = _read(_SVC).split("def status(")[1].split("\n# ─")[0]
    for field in sorted(fields):
        assert f'"{field}"' in produced, f"the model declares {field}, status() omits it"


def test_the_placement_pickers_only_show_without_a_managed_node():
    """A managed node dictates the placement — a function has to share its region to
    reach its VPC — so a picker there would only offer a deploy that fails every
    grant."""
    card = _card()
    assert "!portainerNodes.length" in card
    assert "portainerAdapterForm.cloud" in card and "portainerAdapterForm.region" in card


def test_the_confirm_says_it_is_armed_and_who_owns_the_approval():
    page = _read(_PAGE)
    body = page.split("async pairPortainerAdapter()")[1].split("\n    async ")[0]
    assert "ARMED" in body, "the confirm must say real accounts get created"
    assert "Entitle owns the approval" in body
    assert "secret store" in body, "say where the API token stays"


def test_the_remove_confirm_names_what_it_takes_away():
    page = _read(_PAGE)
    body = page.split("async removePortainerAdapter()")[1].split("\n    async ")[0]
    for phrase in ("Entitle integration", "API token", "firewall"):
        assert phrase in body, phrase


def test_the_handlers_post_to_the_routes_the_router_declares():
    page = _read(_PAGE)
    for method, path in (("loadPortainerAdapter", "/api/containers/portainer/adapter"),
                         ("pairPortainerAdapter",
                          "/api/containers/portainer/adapter-pair"),
                         ("removePortainerAdapter",
                          "/api/containers/portainer/adapter-retire")):
        # Anchored on the DEFINITION: loadPortainerAdapter is also called from
        # loadPortainerNode's tail, which appears first in the file.
        body = page.split(f"async {method}()")[1].split("\n    async ")[0]
        assert f"'{path}'" in body, f"{method} does not call {path}"
        # And the router really serves it, at that prefix.
        suffix = path.replace("/api/containers", "")
        assert f'"{suffix}"' in _read(_API), suffix


def test_the_card_is_loaded_with_the_node():
    """One tab switch fetches both, so the two can never disagree about whether a
    managed node exists — which is what the placement pickers key off."""
    page = _read(_PAGE)
    body = page.split("async loadPortainerNode()")[1].split("\n    portainerCloudChanged")[0]
    assert "loadPortainerAdapter()" in body


def test_the_dry_run_badge_reads_the_deployed_value():
    """Unset FN_PORTAINER_DRY_RUN means dry run, so a card that inferred "armed" from
    absence would label a no-op adapter as live."""
    card = _card()
    assert "portainerAdapter.dry_run" in card
    produced = _read(_SVC).split("def status(")[1].split("\n# ─")[0]
    assert 'FN_PORTAINER_DRY_RUN' in produced


# ── The node firewall, and giving it back ────────────────────────────────────

def test_the_firewall_merge_admits_the_adapter():
    """The adapter reaches the node at its INTERNAL IP, and a source-restricted
    firewall applies to intra-VPC ingress too — without this every grant times out."""
    node = _read(_NODE)
    assert "def _adapter_cidrs(" in node
    merge = node.split("async def refresh_portainer_firewall(")[1].split("\ndef ")[0]
    assert "_adapter_cidrs()" in merge


def test_the_firewall_readout_attributes_the_extra_range():
    """Otherwise it shows up as an unexplained entry in the Settings panel."""
    body = _read(_NODE).split("def firewall_status(")[1].split("\ndef ")[0]
    assert "_adapter_cidrs()" in body
    assert '"adapter_cidrs"' in body


def test_the_firewall_reads_the_range_by_key_not_by_importing_the_adapter():
    """The firewall path must not acquire a dependency on the Cloud Functions feature
    being installed at all."""
    body = _read(_NODE).split("def _adapter_cidrs(")[1].split("\ndef ")[0]
    assert "portainer_adapter_source_cidr" in body
    # An AST walk, not a substring or line match: this function's docstring explains
    # why it does not import, and prose that begins "from config rather than..." is
    # indistinguishable from an import statement to anything reading text.
    fn = next(n for n in ast.walk(ast.parse(_read(_NODE)))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_adapter_cidrs")
    assert not [n for n in ast.walk(fn)
                if isinstance(n, (ast.Import, ast.ImportFrom))], \
        "the firewall path must not depend on the Cloud Functions feature"


def test_the_config_key_is_declared():
    assert "portainer_adapter_source_cidr" in _read(_CONFIG)
    assert "SOURCE_CIDR_KEY" in _read(_SVC)


def test_the_service_and_the_node_agree_on_the_key_name():
    """Two spellings of the key is how the pairing writes a range the firewall never
    reads — a green pairing and a grant that times out."""
    svc_key = re.search(r'SOURCE_CIDR_KEY = "([a-z_]+)"', _read(_SVC)).group(1)
    node_body = _read(_NODE).split("def _adapter_cidrs(")[1].split("\ndef ")[0]
    assert f'"{svc_key}"' in node_body


# ── Node teardown ────────────────────────────────────────────────────────────

def _teardown():
    return _read(_NODE).split("async def run_teardown(")[1]


def test_the_teardown_retires_the_adapter():
    """Otherwise Stop clears portainer_url and leaves a grantable Entitle integration
    pointed at a host that no longer exists."""
    assert "portainer_adapter_service" in _teardown()
    assert "retire_adapter(" in _teardown()


def test_the_adapter_is_retired_before_the_token_is_cleared():
    """The staged copy of portainer_pat cannot be retired once the original is gone —
    retire_pat_secret reads it to derive nothing, but the adapter authenticates with
    it and the ordering is what keeps the two halves consistent."""
    body = _teardown()
    assert body.index("retire_adapter(") < body.index('"portainer_pat"')


def test_a_broken_entitle_tenant_does_not_block_a_node_teardown():
    """...but it is still said out loud in the result, because what is left behind is
    a grantable integration that can only error."""
    body = _teardown().split("retire_adapter(")[1][:900]
    assert "except Exception" in body
    assert "adapter_note" in body or "adapter_warning" in _teardown()


def test_the_firewall_entry_is_cleared_even_if_the_retirement_failed():
    """Otherwise the next node deployed inherits a dead adapter's range in its
    allow-list."""
    assert "portainer_adapter_source_cidr" in _teardown()


# ── The stored API token ─────────────────────────────────────────────────────
# The adapter authenticates to Portainer with the token in `portainer_pat` and
# nothing else, and it keeps its OWN copy in the cloud's secret store — staged once,
# inside the pairing job. So the two ways a token goes wrong (the dashboard's is bad,
# or only the adapter's copy is stale) both used to end at "retire and pair again".

def test_both_token_routes_exist_and_need_write():
    api = _read(_API)
    for decorator in ('@router.post("/portainer/token"',
                      '@router.post("/portainer/token/stage"'):
        assert decorator in api, decorator
        assert 'require_permission("containers", "write")' in _route(decorator), decorator


def test_minting_does_not_need_the_cloud_function_scope():
    """It writes no cloud_functions row and runs no apply — it restarts at most the
    function that already exists. Requiring the deploy scope to repair a credential
    would put the repair out of reach of the operator holding the credential."""
    for decorator in ('@router.post("/portainer/token"',
                      '@router.post("/portainer/token/stage"'):
        assert "_require_function_write" not in _route(decorator), decorator


def test_the_token_never_comes_back_in_the_response():
    """Portainer shows a token's value exactly once and this dashboard is where it
    is kept — not something that hands it back out into a response body that gets
    logged, cached and rendered."""
    body = _read(_MODELS).split("class PortainerTokenResponse(")[1].split("\nclass ")[0]
    declared = set(re.findall(r"^    (\w+):", body, re.M))
    for field in ("token", "pat", "api_key", "key", "raw_api_key", "value"):
        assert field not in declared, f"PortainerTokenResponse carries {field}"
    assert "token_configured" in declared, declared


def test_the_mint_stores_before_it_stages():
    """Staging can fail on its own (an unreachable secret store, no adapter). Losing
    a minted token to that would be unrecoverable: Portainer will not show it twice,
    so the operator would be left with a live token nothing holds."""
    body = _route('@router.post("/portainer/token"')
    assert body.index("mint_api_token") < body.index("restage_pat")
    # And the staging failure is reported, not raised over the top of the mint.
    after = body.split("restage_pat")[1]
    assert "except Exception" in after and "token is stored" in after


def test_the_stage_route_refuses_when_there_is_nothing_to_stage():
    """Staging an empty value would overwrite the adapter's working copy with
    nothing, turning a stale credential into no credential."""
    body = _route('@router.post("/portainer/token/stage"')
    assert "portainer_pat" in body and "nothing to stage" in body


def test_the_token_handlers_post_to_the_routes_the_router_declares():
    page = _read(_PAGE)
    api = _read(_API)
    for method, path in (("mintPortainerToken", "/api/containers/portainer/token"),
                         ("stagePortainerToken",
                          "/api/containers/portainer/token/stage")):
        body = page.split(f"async {method}()")[1].split("\n    async ")[0]
        assert f"'{path}'" in body, f"{method} does not call {path}"
        assert f'"{path.replace("/api/containers", "")}"' in api, path


def test_every_token_field_the_page_reads_is_one_the_model_declares():
    """The card's own text comes from `note`, which is the only place an operator is
    told WHEN a re-staged token takes effect — a typo there is a silent blank."""
    page = _read(_PAGE)
    declared = set(re.findall(r"^    (\w+):", _read(_MODELS).split(
        "class PortainerTokenResponse(")[1].split("\nclass ")[0], re.M))
    for method in ("mintPortainerToken", "stagePortainerToken"):
        body = page.split(f"async {method}()")[1].split("\n    async ")[0]
        for field in set(re.findall(r"\bdata\.(\w+)", body)):
            assert field in declared, f"{method} reads data.{field}, which is not a field"


def test_the_mint_confirm_says_old_tokens_are_not_revoked():
    """Minting adds a token, it does not replace one. An operator who assumed
    otherwise leaves live credentials behind believing they are gone."""
    page = _read(_PAGE)
    body = page.split("async mintPortainerToken()")[1].split("\n    async ")[0]
    assert "confirm(" in body
    assert "keep working" in body and "revoke" in body


# ── Re-applying the node's ingress rule ──────────────────────────────────────
# The deploy configures the firewall from ONE egress detection and never revisits it.
# When the dashboard's outbound address moves — or the rule is deleted by hand — the
# node keeps running and every caller reports the same unhelpful thing, "unreachable".
# Minting a token repairs that on its way past; the button is the repair on its own.

def _svc_function(name, path=_NODE):
    """A service function's source, by def line."""
    src = _read(path)
    marker = f"\ndef {name}("
    if marker not in src:
        marker = f"\nasync def {name}("
    assert marker in src, f"no such function: {name}"
    body = src.split(marker)[1]
    # Up to the next top-level def, not an inner one.
    for stop in ("\ndef ", "\nasync def "):
        if stop in body:
            body = body.split(stop)[0]
    return body


def test_the_firewall_reapply_route_exists_and_needs_write():
    """It mutates a cloud ingress rule, so read is not enough — and it must not need
    the Cloud Functions scope either: an operator locked out of their own node should
    not need the adapter-deploy permission to get back in."""
    decorator = '@router.post("/portainer/node/firewall"'
    api = _read(_API)
    assert decorator in api, decorator
    body = _route(decorator)
    assert 'require_permission("containers", "write")' in body, body[:400]
    assert "_require_function_write" not in body
    # The read-only breakdown keeps its own verb on the same path.
    assert '@router.get("/portainer/node/firewall"' in api


def test_the_reapply_button_posts_to_the_route_the_router_declares():
    page = _read(_PAGE)
    body = page.split("async reapplyPortainerFirewall()")[1].split("\n    async ")[0]
    assert "'/api/containers/portainer/node/firewall'" in body, body[:400]
    assert '@router.post("/portainer/node/firewall"' in _read(_API)
    # And the button is actually rendered, not just defined.
    assert "reapplyPortainerFirewall()" in page.split("<script")[0] or \
        'reapplyPortainerFirewall()"' in page, "the handler has no button"


def test_every_firewall_field_the_page_reads_is_one_the_service_returns():
    """A field name that is not in the payload is not an error anywhere: the note
    renders blank or says 'no change' about a change. Pin the two halves together."""
    returned = set(re.findall(r'"(\w+)":', _svc_function("firewall_status")))
    returned |= set(re.findall(r'"(\w+)":', _svc_function("reapply_firewall")))
    body = _read(_PAGE).split("async reapplyPortainerFirewall()")[1].split("\n    async ")[0]
    for field in set(re.findall(r"\bdata\.(\w+)", body)):
        assert field in returned, f"the page reads data.{field}, which is never returned"


def test_the_button_and_the_mint_make_the_SAME_repair():
    """Two implementations of 'detect the egress address and re-apply the rule' would
    drift, and the one that drifted would be the one nobody ran during the outage."""
    assert "reapply_firewall(" in _svc_function("_readmit_egress_and_retry"), \
        "the mint's re-admit no longer goes through reapply_firewall"
    assert "reapply_firewall(" in _route('@router.post("/portainer/node/firewall"')


def test_a_recreated_rule_counts_as_a_change():
    """A deleted rule computes the same source set it always did, so comparing sets
    alone reports 'nothing changed' about the repair that just put it back."""
    assert 'applied.get("created")' in _svc_function("reapply_firewall")


def test_the_reapply_reports_a_closed_firewall_rather_than_a_silent_success():
    """Fail-closed is the contract, so an empty merged set is a real outcome, not an
    error — and it means the node is now reachable by nobody. Saying only 'ingress
    re-applied' would read as a fix."""
    body = _read(_PAGE).split("async reapplyPortainerFirewall()")[1].split("\n    async ")[0]
    assert "data.opened" in body and "CLOSED" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
