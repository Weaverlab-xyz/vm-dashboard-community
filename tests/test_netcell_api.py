"""The cell's deploy contract, read out of api/netcell.py rather than exercised.

AST-parsed the way tests/test_ot_cell_meta.py parses api/ot.py, because the properties
that matter here are structural and are the ones a well-meaning refactor breaks:

  * **The cell is a plain deploy.** `job_type="gce_deploy"`, and *pending* rather than
    queued -- there is no parent to drive it, which is the simplification this whole
    feature is built on. A `queued` child here would simply never run.
  * **No external IP, ever.** The demo's claim is that the device carries no inbound
    rule and is still reachable. `create_external_ip=True` would make the headline false
    while everything still appeared to work.
  * **The forced onboarding method reaches the request.** If the cell stops passing
    `passwordsafe_method`, it silently onboards on the GCP default -- a platform that
    can never manage a VyOS guest -- and nothing fails until a rotation is attempted.
  * **The marker is stamped.** The tab, the tile and the cells list all key on it; a
    cell without it is invisible to every surface that should show it.
  * **Entitle is not wired.** Its SSH integration creates ephemeral accounts with
    useradd, which is not how VyOS manages users.

Runs under pytest, or standalone:
    python tests/test_netcell_api.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-netcell-api")

_API = os.path.join(_ROOT, "web_dashboard", "api", "netcell.py")


def _tree():
    with open(_API, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _call(name):
    """The one call to ``name`` in the deploy handler, as an ast.Call."""
    found = [n for n in ast.walk(_tree())
             if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id == name)
                  or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]
    assert found, f"api/netcell.py makes no {name}(...) call"
    return found


def _kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _const(call, name):
    node = _kw(call, name)
    return node.value if isinstance(node, ast.Constant) else None


# -- the deploy request --------------------------------------------------------

def test_the_child_request_never_asks_for_an_external_ip():
    req = _call("GCPDeployRequest")[0]
    assert _const(req, "create_external_ip") is False, (
        "the cell's deploy request does not pin create_external_ip=False — the demo's "
        "'no inbound rule' claim would be false while everything still appeared to work")


def test_the_request_carries_the_forced_onboarding_method():
    req = _call("GCPDeployRequest")[0]
    node = _kw(req, "passwordsafe_method")
    assert node is not None, (
        "the cell no longer passes passwordsafe_method — it would onboard on the GCP "
        "default, a platform that can never manage a VyOS guest, and nothing would fail "
        "until a rotation was attempted")
    assert isinstance(node, ast.Attribute) and node.attr == "NETCELL_PS_METHOD", \
        "the method is inlined rather than read from netcell_service.NETCELL_PS_METHOD"


def test_the_cell_does_not_register_in_entitle():
    req = _call("GCPDeployRequest")[0]
    assert _const(req, "register_in_entitle") is False, (
        "the cell opts into Entitle. Its SSH integration creates ephemeral accounts "
        "with useradd, which is not how VyOS manages users")


def test_the_request_is_for_exactly_one_vm():
    req = _call("GCPDeployRequest")[0]
    assert _const(req, "count") == 1, "a cell is one device, not a batch"


# -- the job row ---------------------------------------------------------------

def test_the_job_is_a_plain_pending_gce_deploy():
    job = _call("create_job")[0]
    assert _const(job, "job_type") == "gce_deploy", \
        "the cell is no longer an ordinary gce_deploy row"
    status = _const(job, "status")
    assert status is None, (
        f"the cell's job sets status={status!r}. It must stay the default (pending): "
        "the runner claims pending work, and there is no parent job here to drive a "
        "queued row — it would never run")


def test_the_job_metadata_carries_the_marker_and_the_params():
    job = _call("create_job")[0]
    meta = _kw(job, "metadata")
    assert isinstance(meta, ast.Dict), "create_job's metadata is not a literal dict"
    keys = {k.value for k in meta.keys if isinstance(k, ast.Constant)}
    for required in ("netcell", "netcell_params", "req", "instance_name", "zone"):
        assert required in keys, f"the cell's job metadata has no {required!r} key"


def test_the_marker_is_the_one_the_service_reads():
    """Two spellings of the marker is a cell the tab cannot find."""
    from web_dashboard.services import netcell_service as N
    job = _call("create_job")[0]
    meta = _kw(job, "metadata")
    keys = {k.value for k in meta.keys if isinstance(k, ast.Constant)}
    assert "netcell" in keys and N.is_cell({"netcell": True})


# -- the guards actually run ---------------------------------------------------

def test_every_guard_in_the_service_is_called_by_the_route():
    """A guard nobody calls is a comment."""
    src = open(_API, encoding="utf-8").read()
    for guard in ("image_problem", "release_problem",
                  "pra_preflight_problem", "ps_platform_problem"):
        assert guard in src, f"api/netcell.py never calls {guard}()"


def test_the_guards_run_before_anything_is_created():
    """Ordering is the whole value: a refusal after create_job is a leaked row."""
    src = open(_API, encoding="utf-8").read()
    first_create = src.index("job_service.create_job")
    for guard in ("image_problem", "release_problem", "pra_preflight_problem"):
        assert src.index(guard) < first_create, \
            f"{guard}() is checked after the job row is created"


def test_the_network_tag_is_applied():
    src = open(_API, encoding="utf-8").read()
    assert "NETCELL_NETWORK_TAG" in src, \
        "the cell carries no network tag, so no firewall rule can name the cells"


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
