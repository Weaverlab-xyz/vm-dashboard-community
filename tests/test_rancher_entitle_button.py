"""The Rancher node row's "Register in Entitle" / "Deregister" actions.

The service behind them is pinned in `test_rancher_entitle_register.py`. What is pinned
HERE is the wiring across Python, Jinja and JS — the seams where nothing fails at import
time and the first sign of a mistake is an orphaned integration in a customer's Entitle
tenant:

  * **the Register guard.** `register_rancher_in_entitle` is not idempotent: a second
    register overwrites `entitle_rancher_tfstate` and leaves the first integration alive
    in Entitle with nothing able to remove it. Hiding Register once an integration exists
    is the only thing standing between a deploy-time auto-register and one careless
    click. This is the most important assertion in the file.
  * **the permission.** The route is on the k8s router and wants `k8s:write`, while every
    other control on that tab is `containers:*`.
  * **the fields.** A renamed response field leaves an `x-show` reading undefined, which
    Alpine treats as false — so the control silently disappears rather than erroring.

Source/AST checks, in the style of `test_clouddb_adapter_button.py`. Under pytest or
standalone:
    python tests/test_rancher_entitle_button.py
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
_K8S_API = os.path.join(_ROOT, "web_dashboard", "api", "k8s.py")
_MODELS = os.path.join(_ROOT, "web_dashboard", "models", "containers.py")
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py")
_DOCS = os.path.join(_ROOT, "docs", "integrations", "rancher.md")

ROUTE = "/api/k8s/rancher/entitle-register"


def _read(path):
    return open(path, encoding="utf-8").read()


def _handler():
    """`get_rancher_node`'s source, anchored on its route decorator."""
    api = _read(_API)
    return api.split('@router.get("/rancher", response_model=RancherNodeResponse)')[1] \
              .split("\n@router.")[0]


def _actions_cell():
    """The Rancher node row's Actions cell."""
    page = _read(_PAGE)
    row = page.split('<template x-for="n in rancherNodes"')[1].split("</template>")[0]
    return row.split('text-sm text-right">')[1]


def _method(name):
    page = _read(_PAGE)
    return page.split(f"async {name}()")[1].split("\n    async ")[0]


# ── The guard against orphaning an integration ───────────────────────────────

def test_register_is_hidden_once_an_integration_exists():
    """THE assertion. register_rancher_in_entitle overwrites entitle_rancher_tfstate,
    which is the only handle deregister has on the existing integration — so a second
    register leaves the first alive in Entitle, unreachable. The hazard itself is pinned
    in test_rancher_entitle_register; this is the guard.
    """
    cell = _actions_cell()
    register = cell.split('@click="registerRancherEntitle()"')[1].split("</button>")[0]
    assert "!rancherEntitle.integration_id" in register, (
        "Register must be hidden when an integration already exists, or one click "
        "orphans it in Entitle")


def test_deregister_is_shown_only_when_there_is_something_to_remove():
    cell = _actions_cell()
    dereg = cell.split('@click="deregisterRancherEntitle()"')[1].split("</button>")[0]
    assert "rancherEntitle.integration_id" in dereg
    assert "!rancherEntitle.integration_id" not in dereg


def test_the_two_actions_are_mutually_exclusive():
    """One integration, one state: never both buttons at once, or the pair reads as a
    choice rather than a state."""
    cell = _actions_cell()
    register = cell.split('@click="registerRancherEntitle()"')[1].split("</button>")[0]
    dereg = cell.split('@click="deregisterRancherEntitle()"')[1].split("</button>")[0]
    assert "!rancherEntitle.integration_id" in register
    assert "!rancherEntitle.integration_id" not in dereg


def test_register_needs_a_running_node():
    """The route 400s on a missing rancher_server_url/api_token, so offering it against
    a stopped node is a button that can only fail."""
    cell = _actions_cell()
    register = cell.split('@click="registerRancherEntitle()"')[1].split("</button>")[0]
    assert "n.status === 'RUNNING'" in register


def test_the_state_is_visible_even_to_someone_who_cannot_act():
    """The chip is gated on the integration id ALONE — not on the permission — so a
    containers-only reader can still see whether the node is registered."""
    cell = _actions_cell()
    chip = cell.split("Entitle ✓")[0]
    assert 'x-show="rancherEntitle.integration_id"' in chip
    assert "can_register" not in chip.split('x-show="rancherEntitle.integration_id"')[1]


# ── The permission ───────────────────────────────────────────────────────────

def test_both_buttons_are_gated_on_the_permission_the_route_requires():
    cell = _actions_cell()
    for action in ("registerRancherEntitle", "deregisterRancherEntitle"):
        body = cell.split(f'@click="{action}()"')[1].split("</button>")[0]
        assert "rancherEntitle.can_register" in body, action


def test_the_route_really_requires_k8s_write():
    """The gate is only meaningful if it matches. This is the scope the handler declares."""
    k8s = _read(_K8S_API)
    route = k8s.split('@router.post("/rancher/entitle-register"')[1].split("\n@router.")[0]
    assert 'require_permission("k8s", "write")' in route


def test_the_permission_is_computed_with_the_shared_predicate():
    """`has_permission` is deliberately the ONE implementation of this rule (its own
    docstring says so, and test_vm_suspend_schedule pins it). A second hand-rolled
    effective_permissions_dict check here would drift, invisibly, in both directions."""
    api = _read(_API)
    assert "has_permission" in api.split("from .auth import")[1].split("\n")[0], (
        "import has_permission from .auth rather than re-deriving the rule")
    handler = _handler()
    assert 'has_permission(current_user, "k8s", "write")' in handler
    # And no second implementation crept in beside it. An AST walk for the ATTRIBUTE
    # ACCESS, not a substring: the handler's own comment names
    # effective_permissions_dict to explain why it does not use it, which no text check
    # can tell apart from the thing it is forbidding.
    fn = next(n for n in ast.walk(ast.parse(api))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "get_rancher_node")
    assert not [n for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and n.attr == "effective_permissions_dict"]


# ── The fields the buttons read ──────────────────────────────────────────────

def _declared():
    decl = _read(_MODELS).split("class RancherNodeResponse(BaseModel):")[1]
    return decl.split("\nclass ")[0]


def test_every_field_the_row_reads_is_one_the_api_declares():
    """The likeliest drift: a renamed field leaves an x-show reading undefined, which
    Alpine treats as false — the control vanishes instead of erroring."""
    declared = _declared()
    page = _read(_PAGE)
    # The page's state object maps API field -> local name, so check the API names.
    for field in sorted(set(re.findall(r"data\.(entitle_[a-z_]+)", page))):
        assert f"{field}:" in declared, f"the page reads {field}, the API never sends it"


def test_every_local_field_is_seeded_and_populated():
    """Seeded in the Alpine state (so the first render has defined fields) AND filled
    from the payload."""
    page = _read(_PAGE)
    seed = page.split("rancherEntitle: {")[1].split("}")[0]
    fill = page.split("this.rancherEntitle = {")[1].split("};")[0]
    for field in sorted(set(re.findall(r"rancherEntitle\.([a-z_]+)", page))):
        assert field in seed, f"rancherEntitle.{field} is never seeded"
        assert field in fill, f"rancherEntitle.{field} is never populated from the API"


def test_both_return_points_of_the_handler_carry_the_entitle_state():
    """`get_rancher_node` returns twice — the early not-configured shell and the full
    payload. A field set on only one path defaults to a wrong answer on the other, and
    "not registered" is exactly the wrong answer that hides Deregister."""
    handler = _handler()
    returns = handler.split("return RancherNodeResponse(")[1:]
    assert len(returns) == 2, f"expected 2 return points, found {len(returns)}"
    for seg in returns:
        # A bounded window rather than up-to-the-first-`)`: the call spans several
        # lines and contains nested parens (`len(nodes)`, `if cloud == "gcp"`), so
        # splitting on `)` truncates the argument list mid-way.
        assert "**entitle" in seg[:400], (
            "both returns must spread the resolved Entitle state")


def test_the_entitle_state_is_resolved_before_the_early_return():
    handler = _handler()
    assert handler.index("entitle = {") < handler.index("if not account:")


def test_the_status_is_read_from_config_not_guessed():
    handler = _handler()
    assert "entitle_rancher_integration_id" in handler
    assert "entitle_registration_enabled" in handler


def test_the_handler_makes_no_cloud_call_for_the_entitle_state():
    """The tab polls this endpoint; the three fields are two config reads and a
    predicate, and must stay that cheap."""
    handler = _handler()
    block = handler.split("entitle = {")[1].split("}")[0]
    assert "await" not in block


# ── The handlers ─────────────────────────────────────────────────────────────

def test_the_handlers_post_the_declared_actions_to_the_route():
    valid = re.search(r"VALID_ENTITLE_CLUSTER_ACTIONS = \(([^)]*)\)", _read(_SVC)).group(1)
    actions = set(re.findall(r'"(\w+)"', valid))
    for method, action in (("registerRancherEntitle", "register"),
                           ("deregisterRancherEntitle", "deregister")):
        body = _method(method)
        assert f"'{ROUTE}'" in body, f"{method} does not post to {ROUTE}"
        assert f"action: '{action}'" in body, f"{method} sends the wrong action"
        assert action in actions, f"{action!r} is not an action the service accepts"


def test_the_route_the_handlers_call_exists():
    assert '@router.post("/rancher/entitle-register"' in _read(_K8S_API)


def test_both_handlers_confirm_first():
    """Each one changes a customer-visible integration; neither should fire on a
    mis-click."""
    for method in ("registerRancherEntitle", "deregisterRancherEntitle"):
        assert "confirm(" in _method(method), method


def test_the_register_confirm_says_what_kind_of_access_it_grants():
    body = _method("registerRancherEntitle")
    assert "EPHEMERAL" in body or "ephemeral" in body
    assert "rancher_allowed_source_cidrs" in body, (
        "name the firewall setting — registration succeeds while grants fail without it")


def test_the_deregister_confirm_says_what_survives():
    body = _method("deregisterRancherEntitle")
    for phrase in ("node", "unaffected"):
        assert phrase in body, phrase


def test_the_page_uses_its_own_toast_not_the_k8s_pages_flash():
    """`this.flash()` is the k8s page's helper and does not exist here — it would throw
    inside the catch, swallowing the real error."""
    for method in ("registerRancherEntitle", "deregisterRancherEntitle"):
        body = _method(method)
        assert "toast(" in body, method
        assert "this.flash(" not in body, method


def test_the_state_is_refreshed_by_the_call_that_already_fetches_the_node():
    """One source, so the buttons and the endpoint cannot disagree about whether a
    registration exists — which is what keeps the Register guard honest."""
    page = _read(_PAGE)
    body = page.split("async loadRancher()")[1].split("\n    rancherCloudChanged")[0]
    assert "this.rancherEntitle = {" in body


# ── The state the page explains, and the warning it carries ──────────────────

def test_the_page_explains_all_three_states():
    """Registered, not-registered-with-the-flag-on, and flag-off are three different
    situations with three different remedies. The middle one is why this control
    exists: a deploy auto-registers best-effort and only logs a warning."""
    page = _read(_PAGE)
    block = page.split("Entitle registration state")[1].split("Reachability is governed")[0]
    assert 'x-show="rancherEntitle.integration_id"' in block
    assert "rancherEntitle.enabled && !rancherEntitle.integration_id" in block
    assert 'x-show="!rancherEntitle.enabled"' in block
    assert "entitle_registration_enabled" in block


def test_the_page_distinguishes_all_three_reachability_states():
    """"Empty" means UNKNOWN here, never "none needed" — so agent-brokered, allowed,
    and ranges-unknown have to be three separate messages. Collapsing the last two is
    how a registration that cannot grant reads as healthy."""
    page = _read(_PAGE)
    block = page.split("Entitle registration state")[1].split("Reachability is governed")[0]
    assert "rancherEntitle.private" in block, "agent-brokered needs no ranges at all"
    assert "rancherEntitle.source_cidrs.length" in block
    assert "!rancherEntitle.source_cidrs.length" in block
    assert "entitle_source_cidrs" in block, "name the key that fixes it"


def test_the_unknown_ranges_state_says_registration_still_succeeded():
    """The confusing part, and the reason the warning exists: the integration is real
    and healthy-looking while being unable to grant."""
    page = _read(_PAGE)
    block = page.split("Entitle registration state")[1].split("Reachability is governed")[0]
    # Whitespace-normalised: the copy is hard-wrapped, so a phrase check against the
    # raw markup fails on wherever the line happened to break.
    warn = " ".join(block.split("!rancherEntitle.source_cidrs.length")[1].split())
    assert "Entitle's API" in warn
    assert "time out" in warn


def test_the_firewall_merge_admits_entitle():
    """The whole point of the allow-listing: Entitle's ranges have to reach the merged
    set, or registering opens nothing and every grant times out."""
    node = _read(os.path.join(_ROOT, "web_dashboard", "services",
                              "rancher_node_service.py"))
    assert "def _entitle_cidrs(" in node
    merge = node.split("async def refresh_rancher_firewall(")[1].split("\ndef ")[0]
    assert "_entitle_cidrs()" in merge
    status = node.split("def firewall_status(")[1].split("\ndef ")[0]
    assert "_entitle_cidrs()" in status
    assert '"entitle_cidrs"' in status, "attribute the ranges in the readout"


def test_the_ranges_are_only_open_while_an_integration_exists():
    """Symmetry with deregister: no reason to open the node to Entitle before there is
    anything to grant, and a deregister has to close it again."""
    node = _read(os.path.join(_ROOT, "web_dashboard", "services",
                              "rancher_node_service.py"))
    body = node.split("def _entitle_cidrs(")[1].split("\ndef ")[0]
    assert "entitle_rancher_integration_id" in body


def test_agent_brokered_mode_opens_nothing():
    """In private mode the agent reaches the node from inside and Entitle's cloud never
    touches it, so these would be holes that buy nothing."""
    node = _read(os.path.join(_ROOT, "web_dashboard", "services",
                              "rancher_node_service.py"))
    body = node.split("def _entitle_cidrs(")[1].split("\ndef ")[0]
    assert "entitle_rancher_private" in body


def test_the_register_job_reapplies_the_firewall():
    """Otherwise the ranges only land on the next node deploy, and the registration the
    operator just performed still cannot grant."""
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py"))
    body = svc.split("async def run_rancher_entitle_register(")[1].split("\nasync def ")[0]
    assert "refresh_rancher_firewall" in body


def test_the_register_job_reports_an_unresolvable_allow_list():
    """The one case where the job completes and the feature still does not work, so it
    has to be in the RESULT an operator opens — nothing downstream will say it."""
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py"))
    body = svc.split("async def run_rancher_entitle_register(")[1].split("\nasync def ")[0]
    assert "unconfigured_warning" in body
    assert "reachability_warning" in body


def test_the_published_table_is_not_quietly_populated_with_guesses():
    """Inbound-firewall values: too narrow drops grants, too broad exposes a management
    plane. If a range ever lands in this table it must come from BeyondTrust's
    documented list — this test failing is the prompt to check that it did, and to
    delete this test once the provenance is recorded elsewhere."""
    egress = _read(os.path.join(_ROOT, "web_dashboard", "services", "entitle_egress.py"))
    table = egress.split("_PUBLISHED: dict = {")[1].split("}")[0]
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", table), (
        "a range appeared in the published table — confirm it came from the documented "
        "allow-list, then update this test")


def test_the_override_wins_over_the_published_list():
    """A tenant on dedicated addresses, or one that learns of a change before this file
    does, must not be blocked waiting on a release."""
    egress = _read(os.path.join(_ROOT, "web_dashboard", "services", "entitle_egress.py"))
    body = egress.split("def cidrs(")[1].split("\ndef ")[0]
    assert body.index("entitle_source_cidrs") < body.index("_PUBLISHED")


def test_an_empty_range_set_is_reported_as_unknown_not_as_fine():
    """The distinction the whole warning rests on."""
    egress = _read(os.path.join(_ROOT, "web_dashboard", "services", "entitle_egress.py"))
    assert "def configured(" in egress
    assert "def unconfigured_warning(" in egress


def test_the_docs_cover_the_buttons_and_the_reachability_trap():
    docs = _read(_DOCS)
    section = docs.split("## Entitle registration")[1].split("\n## ")[0]
    # Normalised twice over, because both bite: the prose is hard-wrapped, so a phrase
    # check fails on wherever the line happened to break — and the hazard note is a
    # blockquote, whose `>` markers survive a plain whitespace collapse and land in the
    # middle of the sentence.
    flat = " ".join(re.sub(r"(?m)^\s*>\s?", "", section).split())
    assert "Register in Entitle" in flat
    assert "Deregister" in flat
    assert "k8s:write" in flat, "say which permission the buttons need"
    # The allow-listing, and the key that drives it. Deliberately NOT
    # rancher_allowed_source_cidrs: registering now opens the firewall to Entitle
    # itself, so pointing operators at the manual CSV would document the old workaround.
    assert "entitle_source_cidrs" in flat
    assert "entitle_rancher_private" in flat, "the mode where no ranges are needed"
    # And the hazard, so the hidden-Register behaviour reads as deliberate rather than
    # as an oversight someone should "fix".
    assert "Terraform state" in flat or "tfstate" in flat


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
