"""Static invariants for the remote-agent lease.

The dangerous failure here is silent. If an agent job type ever became claimable by
the local job runner, both would execute the same row — the worker in the dashboard's
network, the agent in the customer's — and neither would notice. Nothing would error;
the work would just happen twice, in two places, against two different sets of targets.

So these assertions are structural and read the source rather than the behaviour:

  * ``AGENT_JOB_TYPES`` and ``jobs_worker.HANDLED_TYPES`` are disjoint;
  * ``lease_one`` claims ``queued``, never ``pending``;
  * ``create_job`` forces ``queued`` when ``agent_id`` is set, so the invariant lives
    in the one funnel every job row passes through rather than in each call site;
  * no call site anywhere passes ``agent_id`` with a contradicting ``status``;
  * the agent API never reaches for ``require_permission``, whose empty-permissions
    fallback means *unrestricted*.

Pure: parses the modules with ``ast`` rather than importing them, so it runs with no
FastAPI, no SQLAlchemy and no database. Runs under pytest, or standalone:
    python tests/test_agent_lease_invariants.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web_dashboard")
_WORKER = os.path.join(_WEB, "jobs_worker.py")
_AGENT_SERVICE = os.path.join(_WEB, "services", "agent_service.py")
_JOB_SERVICE = os.path.join(_WEB, "services", "job_service.py")
_AGENT_API = os.path.join(_WEB, "api", "agent.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tuple_constant(path, name):
    """Read a module-level tuple/list of string literals via ast, so the module never
    has to be imported (jobs_worker pulls in SQLAlchemy)."""
    tree = ast.parse(_read(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if not isinstance(node.value, (ast.Tuple, ast.List)):
                    raise AssertionError(f"{name} is not a literal tuple/list")
                return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError(f"{name} not found in {os.path.basename(path)}")


def _function_source(path, name):
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_read(path), node)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


# ── the race ──────────────────────────────────────────────────────────────────

def test_agent_job_types_are_disjoint_from_the_worker():
    """THE invariant. An overlap means both executors claim the same row."""
    agent = _tuple_constant(_AGENT_SERVICE, "AGENT_JOB_TYPES")
    worker = _tuple_constant(_WORKER, "HANDLED_TYPES")
    overlap = agent & worker
    assert not overlap, (
        f"job type(s) claimable by BOTH the local runner and a remote agent: "
        f"{sorted(overlap)} — they would execute twice, in two different networks")


def test_agent_job_types_is_not_empty():
    """Guard the guard: an empty tuple would make the disjointness test vacuous."""
    assert _tuple_constant(_AGENT_SERVICE, "AGENT_JOB_TYPES")


def test_lease_claims_queued_and_never_pending():
    """jobs_worker._claim_one owns 'pending'. If lease_one drifted onto it, an agent
    would start stealing the local runner's work."""
    src = _function_source(_AGENT_SERVICE, "lease_one")
    assert 'Job.status == "queued"' in src, "lease_one must claim status='queued'"
    assert 'Job.status == "pending"' not in src, (
        "lease_one must never claim 'pending' — that is the local runner's queue")


def test_lease_is_scoped_to_the_calling_agent():
    """The IDOR guard. Without the agent_id filter, any enrolled agent leases any
    other agent's work — the highest-severity bug this API could have."""
    src = _function_source(_AGENT_SERVICE, "lease_one")
    assert src.count("Job.agent_id == agent.id") >= 2, (
        "lease_one must filter on agent_id in BOTH the select and the claiming update")


def test_the_claim_is_a_conditional_update_not_a_read_then_write():
    """The rowcount IS the lock, exactly as in jobs_worker._claim_one. A select
    followed by an unconditional write would let two pollers both win."""
    src = _function_source(_AGENT_SERVICE, "lease_one")
    assert ".update(" in src and "claimed == 1" in src, (
        "lease_one must claim with a conditional UPDATE and check its rowcount")


def test_ownership_check_exists_for_per_job_endpoints():
    src = _function_source(_AGENT_SERVICE, "owned_job")
    assert "job.agent_id != agent.id" in src, (
        "owned_job must reject a job belonging to another agent")


# ── the funnel ────────────────────────────────────────────────────────────────

def test_create_job_forces_queued_for_agent_rows():
    """Forced in create_job, not requested by callers: a caller that forgot would
    create a 'pending' row that the local runner happily claims."""
    src = _function_source(_JOB_SERVICE, "create_job")
    assert re.search(r"if\s+agent_id:\s*\n\s*status\s*=\s*[\"']queued[\"']", src), (
        "create_job must force status='queued' whenever agent_id is set")


def test_no_call_site_passes_agent_id_with_a_conflicting_status():
    """Belt and braces on the funnel: even though create_job overrides it, a call site
    that passes status='pending' alongside agent_id is expressing a misunderstanding
    worth failing on."""
    offenders = []
    for dirpath, _dirs, files in os.walk(_WEB):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                tree = ast.parse(_read(path))
            except SyntaxError:                      # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "attr", getattr(fn, "id", ""))
                if name != "create_job":
                    continue
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                if "agent_id" not in kw or "status" not in kw:
                    continue
                value = kw["status"]
                if not (isinstance(value, ast.Constant) and value.value == "queued"):
                    offenders.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
    assert not offenders, (
        f"create_job(agent_id=…) called with a non-'queued' status at: {offenders}")


def test_queued_is_cancellable():
    """A job waiting on an agent that never came back is exactly the row an operator
    reaches for Cancel on; it used to 409."""
    assert '"queued", "pending", "running"' in _function_source(_JOB_SERVICE, "set_cancelled")
    api = _read(os.path.join(_WEB, "api", "jobs.py"))
    assert '("queued", "pending", "running")' in api, (
        "api/jobs.py cancel gate must accept 'queued'")


# ── authorization ─────────────────────────────────────────────────────────────

def test_agent_routes_never_use_require_permission():
    """require_permission treats an empty permission dict as UNRESTRICTED (a
    backward-compat rule for pre-OIDC humans). A machine principal must never touch
    that code path.

    Checked over the AST, not the raw text: the module docstring explains at length
    why this dependency is avoided, and prose about a rule must not read as a
    violation of it.
    """
    tree = ast.parse(_read(_AGENT_API))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            used |= {a.name for a in node.names}
    assert "require_permission" not in used, (
        "api/agent.py must not use require_permission — agents authorize against "
        "AGENT_JOB_TYPES and job ownership, not user permission scopes")


def test_the_agent_dependency_returns_an_agent_not_a_user():
    """The type difference is the guarantee: nothing downstream can mistake an agent
    for a user with permissions attached."""
    src = _function_source(_AGENT_API, "signed_agent")
    assert "-> RemoteAgent" in src.split("\n")[0] or "RemoteAgent:" in src.split(":")[0] + ":", \
        "signed_agent must be annotated as returning RemoteAgent"
    assert "agent_service.authenticate" in src


def test_the_operator_half_is_admin_only():
    """Every route that is not part of the agent protocol must require an admin."""
    src = _read(_AGENT_API)
    tree = ast.parse(src)
    agent_half = {"enroll_agent", "lease_job", "heartbeat", "push_logs", "complete"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in
                        ("get", "post", "delete", "patch") for d in node.decorator_list)
        if not decorated or node.name in agent_half:
            continue
        body = ast.get_source_segment(src, node) or ""
        assert "require_admin" in body, f"{node.name} is not admin-gated"


def test_agent_routes_are_declared_before_the_agent_id_routes():
    """FastAPI matches in declaration order, so a /{agent_id} route declared first
    would bind agent_id='lease' and break the protocol in a way that reads like a
    permissions bug."""
    src = _read(_AGENT_API)
    routes = re.findall(r'@router\.(?:get|post|delete|patch)\("([^"]*)"', src)
    first_param = next((i for i, p in enumerate(routes) if "{agent_id}" in p), len(routes))
    protocol = [i for i, p in enumerate(routes)
                if p in ("/enroll", "/lease") or p.startswith("/jobs/")]
    assert protocol, "no agent-protocol routes found — did they get renamed?"
    assert all(i < first_param for i in protocol), (
        "an agent-protocol route is declared after a /{agent_id} route")


# ── signature freshness ───────────────────────────────────────────────────────

def test_the_nonce_is_recorded_only_after_the_signature_verifies():
    """Order matters: recording first would let an unauthenticated caller who guesses
    an agent id burn that agent's nonces and lock it out of its own queue."""
    src = _function_source(_AGENT_SERVICE, "authenticate")
    verify_at = src.index("verify_request")
    nonce_at = src.index("_consume_nonce")
    assert verify_at < nonce_at, (
        "authenticate() must verify the signature before consuming the nonce")


def test_replay_protection_relies_on_the_unique_constraint():
    """A SELECT-then-INSERT leaves a window in which two copies of one captured
    request both pass."""
    src = _function_source(_AGENT_SERVICE, "_consume_nonce")
    assert "IntegrityError" in src, (
        "_consume_nonce must detect a replay via the unique constraint, not a lookup")


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
