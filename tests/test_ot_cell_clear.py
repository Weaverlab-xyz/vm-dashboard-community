"""The exit a FAILED OT cell never had (api/ot.clear_cell + ot_service.cell_resource_alive).

A cell's inventory record is its VM-deploy child row. Retiring one was the sole
privilege of the cloud's Destroy button, which resolves the row by resource name
among ``completed`` deploys — so a cell whose VM deploy FAILED had no exit at all:
the cards list it (neither destroyed nor cancelled), both card buttons were gated
on ``completed``, the cloud destroy route cannot see a failed row, and
``DELETE /api/jobs/{id}`` refuses any status but queued/pending/running. There is
no Terraform state to fall back on either — the cell VM is an SDK deploy, and the
wiring that does use Terraform never runs when the VM never arrives.

What has teeth here is not that the exit exists but that it stays HONEST, because
"clear the record" is precisely how a billable orphan becomes invisible:

* the liveness probe's three states must stay three — a probe that cannot answer
  must return None, never False, or a credentials outage would silently retire
  rows for VMs that are still running;
* AWS must probe by NAME tag: the instance id is exactly what a deploy that died
  inside RunInstances does not have, which is why EC2's own destroy route cannot
  reach these rows;
* a live resource must be REFUSED, and an unanswerable probe must need an explicit
  force — not be assumed gone;
* the row must keep its ``failed`` status (the destroy runners'
  set_completed-with-destroyed would rewrite a failed deploy into a successful one).

Run: python tests/test_ot_cell_clear.py   (or under pytest)
"""
import ast
import asyncio
import builtins
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "ot_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")
_AZURE_VM = os.path.join(_ROOT, "web_dashboard", "services", "azure_vm_service.py")
_PAGES = {cloud: os.path.join(_ROOT, "web_dashboard", "templates", cloud, "index.html")
          for cloud in ("gcp", "aws", "azure")}


def _load():
    """ot_service standalone — it imports its collaborators lazily inside
    functions, so the module itself needs no app dependencies."""
    spec = importlib.util.spec_from_file_location("ot_service_cell_clear", _SVC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fn_src(path, name):
    """The function's CODE, re-rendered by ast.unparse.

    The docstring is dropped: these assertions ask what the function does, and a
    docstring that merely NAMES a call (this file's subjects all explain which
    calls they deliberately avoid) would otherwise satisfy — or violate — a check
    about the code. ast.unparse also normalises string quoting to single quotes,
    so the assertions below are written that way.
    """
    src = open(path, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return chr(10).join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


def _probe(ot, cloud, meta, stub):
    """Run cell_resource_alive with its lazy `from . import ...` satisfied by fakes."""
    real_import = builtins.__import__

    def _stub_import(name, glob=None, loc=None, fromlist=(), level=0):
        if level == 1 and glob is not None and glob.get("__name__") == ot.__name__:
            return stub
        return real_import(name, glob, loc, fromlist, level)

    loop = asyncio.new_event_loop()
    builtins.__import__ = _stub_import
    try:
        return loop.run_until_complete(ot.cell_resource_alive(cloud, meta))
    finally:
        builtins.__import__ = real_import
        loop.close()


def _azure_stub(answer):
    stub = types.ModuleType("stub")

    async def get_vm(rg, name):
        if isinstance(answer, Exception):
            raise answer
        return answer

    stub.azure_service = types.SimpleNamespace(get_vm=get_vm)
    return stub


# ── the probe's three states ──────────────────────────────────────────────────

def test_a_live_azure_vm_probes_true_and_a_gone_one_false():
    ot = _load()
    meta = {"vm_name": "ot-cell-azure", "resource_group": "vm-cli-rg"}
    assert _probe(ot, "azure", meta, _azure_stub({"vm_name": "ot-cell-azure"})) is True
    assert _probe(ot, "azure", meta, _azure_stub(None)) is False


def test_an_unanswerable_probe_is_none_and_never_false():
    """The safety property. False means "provably gone" and retires the record;
    anything we could not ask must be None so the caller has to make the operator
    assert it. A credentials outage returning False would retire rows for VMs
    that are still running and still billing."""
    ot = _load()
    meta = {"vm_name": "ot-cell-azure", "resource_group": "vm-cli-rg"}
    assert _probe(ot, "azure", meta, _azure_stub(RuntimeError("no creds"))) is None
    # No placement recorded (a row too old, or one that died before it was written)
    # is equally unanswerable — not "gone".
    assert _probe(ot, "azure", {"vm_name": "x"}, _azure_stub(None)) is None
    assert _probe(ot, "gcp", {"instance_name": "x"}, types.ModuleType("stub")) is None
    assert _probe(ot, "aws", {"instance_name": "x"}, types.ModuleType("stub")) is None
    # No name at all: nothing to ask about.
    assert _probe(ot, "azure", {"resource_group": "rg"}, _azure_stub(None)) is None


def test_aws_probes_by_name_tag_not_by_instance_id():
    """The instance id is exactly what a deploy that failed inside RunInstances
    never got — it is why EC2's destroy route cannot reach these rows at all. The
    Name tag is on the row regardless, so the probe has to use it."""
    ot = _load()
    seen = {}

    async def find_instances_by_tag(region, *, name_tag, states):
        seen.update(region=region, name_tag=name_tag, states=states)
        return [{"instance_id": "i-123"}]

    stub = types.ModuleType("stub")
    stub.aws_service = types.SimpleNamespace(find_instances_by_tag=find_instances_by_tag)
    # Deliberately no instance_id in the metadata — the failed-deploy shape.
    meta = {"instance_name": "ot-cell-aws", "region": "us-east-2"}
    assert _probe(ot, "aws", meta, stub) is True
    assert seen["name_tag"] == "ot-cell-aws"
    # A terminated instance is not a billable orphan and must not block a clear.
    assert "terminated" not in seen["states"]
    assert "running" in seen["states"] and "stopped" in seen["states"]


def test_gcp_probes_the_instance_in_its_zone():
    ot = _load()
    stub = types.ModuleType("stub")

    async def describe_instances(project, zone, names):
        assert (project, zone, names) == ("proj", "us-east1-b", ["ot-cell-01"])
        return [{"instance_name": "ot-cell-01"}]

    stub.gcp_service = types.SimpleNamespace(describe_instances=describe_instances)
    meta = {"instance_name": "ot-cell-01", "project_id": "proj", "zone": "us-east1-b"}
    assert _probe(ot, "gcp", meta, stub) is True


# ── the endpoint's refusals ───────────────────────────────────────────────────

def test_the_clear_endpoint_refuses_a_live_resource_and_demands_force_when_unsure():
    src = _fn_src(_API, "clear_cell")
    assert "cell_resource_alive" in src, "clear must probe the cloud, not trust the row"
    assert "alive is True" in src and "status_code=409" in src, \
        "a VM that still exists must be refused, not quietly un-tracked"
    # Two tokens, not one expression: ast.unparse parenthesises `and not` as
    # `and (not force)`, and that rendering is not what this test is about.
    assert "alive is None" in src and "not force" in src, \
        "an unanswerable probe must need an explicit force, not be assumed gone"


def test_the_clear_endpoint_sends_a_deployed_cell_to_destroy():
    """Destroy is the path that removes the VM and the PRA / Password Safe wiring.
    Clear must never become a way around it for a cell that actually deployed."""
    src = _fn_src(_API, "clear_cell")
    assert "child.status == 'completed'" in src and 'status_code=400' in src


def test_clearing_keeps_the_row_failed():
    """`destroyed` alone retires the card. The destroy runners mark it with
    set_completed(deploy_job, meta), which on a FAILED deploy would also rewrite
    the row into a successful one — erasing the only record of what went wrong."""
    src = _fn_src(_API, "clear_cell")
    assert "update_metadata(db, vm_job_id, {'destroyed': True})" in src
    for rewriter in ("set_completed(", "set_cancelled(", "set_failed("):
        assert rewriter not in src, f"clear must not change the row's status ({rewriter})"


def test_clearing_releases_the_gateway_after_marking_the_row():
    """teardown_jumpoint_host_if_idle counts live rows and takes no "exclude me"
    argument, so the `destroyed` flag has to land first or the row being retired
    counts itself and the host is never reclaimed. Every cloud's _run_destroy
    orders it the same way for the same reason."""
    src = _fn_src(_API, "clear_cell")
    assert "teardown_jumpoint_host_if_idle" in src
    assert src.index("{'destroyed': True}") < src.index('teardown_jumpoint_host_if_idle')


# ── the leak that made the gateway unrecoverable ──────────────────────────────

def test_a_failed_azure_deploy_keeps_its_partial_result():
    """The shared Gateway host is acquired at step 1, BEFORE the VM. Its reference
    lives in `result` (jumpoint_mode / jumpoint_host_id / jumpoint_region) and the
    release reads exactly those keys — so a failure path that dropped `result`
    left a billable Gateway VM no code path could reclaim. Same for a vaulted
    Windows admin-password reference."""
    src = _fn_src(_AZURE_VM, "_run_deploy")
    for call in ('set_failed(db, job_id, str(e), result)',
                 "set_failed(db, job_id, f'Unexpected error: {e}', result)"):
        assert call in src, f"the deploy's failure path must persist `result`: {call}"


# ── the card ──────────────────────────────────────────────────────────────────

def test_every_cell_page_offers_clear_on_a_failed_cell():
    """The half that was actually missing: the row was already listed, and both
    of its buttons were gated on `completed`, so the card had no action at all."""
    for cloud, path in _PAGES.items():
        html = open(path, encoding="utf-8").read()
        assert "clearOTCell(c)" in html, f"{cloud} page has no Clear button"
        assert 'x-show="c.status !== \'completed\'"' in html, \
            f"{cloud} page's Clear is not offered on a non-completed cell"
        assert "/api/ot/cell/${encodeURIComponent(c.vm_job_id)}" in html, \
            f"{cloud} page's Clear does not call the cell-clear endpoint"
        # Keyed on the job id, never the resource name: the name is what the
        # cloud destroy routes key on, and it is not unique across retries.
        assert "async clearOTCell(c, force)" in html, \
            f"{cloud} page's Clear cannot retry with force"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
