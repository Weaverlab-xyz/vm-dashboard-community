"""Structural invariants of the OT cell deploy endpoint (api/ot.py).

The cell is a parent/child job pair: an ``ot_cell_deploy`` parent that drives one
``gce_deploy`` child. Both halves of that contract have failure modes that leave
no trace at request time, so they are pinned statically here (the same technique
tests/test_worker_dispatch.py uses):

* the child MUST be created ``status="queued"`` — a pending gce_deploy would be
  claimed by the runner and deployed a SECOND time alongside the parent;
* the child's metadata must carry the keys the orchestrator and destroy path key
  off (``ot_cell``, ``ot_params``, ``req``) — a dropped key means wiring or
  teardown silently skips;
* the parent's metadata must carry ``children`` — that key is what the repo-wide
  queued-children rule in test_worker_dispatch matches on;
* the cell's VM must never get an external IP and must carry the ``ot-sim`` tag
  (the forward hook for tag-scoped Purdue-zone firewalling).

Run: python tests/test_ot_cell_meta.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.join(_ROOT, "web_dashboard", "api", "ot.py")


def _fn(name):
    src = open(_API, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in api/ot.py")


def _create_job_calls(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_job":
            kw = {k.arg: k.value for k in n.keywords}
            job_type = kw.get("job_type")
            status = kw.get("status")
            md = kw.get("metadata")
            keys = ({k.value for k in md.keys if isinstance(k, ast.Constant)}
                    if isinstance(md, ast.Dict) else set())
            yield (job_type.value if isinstance(job_type, ast.Constant) else None,
                   status.value if isinstance(status, ast.Constant) else "pending",
                   keys)


def test_the_child_is_queued_and_carries_the_ot_keys():
    calls = list(_create_job_calls(_fn("deploy_cell")))
    child = [c for c in calls if c[0] == "gce_deploy"]
    assert len(child) == 1, f"expected exactly one gce_deploy create, got {calls}"
    _, status, keys = child[0]
    assert status == "queued", (
        "the cell's gce_deploy child must be created status='queued' — pending "
        "would be claimed by the runner and deployed twice")
    for needed in ("ot_cell", "ot_params", "req", "instance_name",
                   "project_id", "zone", "region"):
        assert needed in keys, f"child metadata lost the {needed!r} key"


def test_the_parent_declares_its_children():
    calls = list(_create_job_calls(_fn("deploy_cell")))
    parents = [c for c in calls if c[0] == "ot_cell_deploy"]
    assert len(parents) == 1
    assert "children" in parents[0][2], (
        "the parent's metadata must carry `children` — the key the repo-wide "
        "queued-children rule (test_worker_dispatch) matches on")


def test_the_cell_vm_is_private_and_tagged():
    fn = _fn("deploy_cell")
    src = ast.unparse(fn)
    assert "create_external_ip=False" in src, (
        "the cell VM must never get an external IP — access is PRA-brokered only")
    assert "'ot-sim'" in src or '"ot-sim"' in ast.dump(fn), (
        "the cell VM must carry the ot-sim network tag (the Purdue-zoning hook)")


def test_the_rewire_endpoint_creates_no_children_parent():
    """Rewire re-drives an EXISTING child; naming child ids in its metadata would
    put it under the queued-children rule and misclassify it as a bulk parent."""
    calls = list(_create_job_calls(_fn("rewire_cell")))
    assert calls, "rewire_cell no longer enqueues an ot_cell_deploy job"
    for job_type, _, keys in calls:
        assert job_type == "ot_cell_deploy"
        assert "children" not in keys
        assert "rewire_child_job_id" in keys


def test_the_default_machine_type_is_e2_medium_everywhere():
    """e2-small (2 GB) proved too tight for Docker + PLC sim + FUXA on a live cell
    (ot-cell-01), so the model default is e2-medium — and the FORM must agree: an
    Alpine x-model preselects whatever the JS state holds, so a stale 'e2-small'
    there silently reverts the model's default for every UI deploy."""
    models = open(os.path.join(_ROOT, "web_dashboard", "models", "ot.py"),
                  encoding="utf-8").read()
    assert 'machine_type: str = "e2-medium"' in models, (
        "OTCellDeployRequest.machine_type is no longer e2-medium")
    tmpl = open(os.path.join(_ROOT, "web_dashboard", "templates", "gcp", "index.html"),
                encoding="utf-8").read()
    assert "machine_type: 'e2-small'" not in tmpl, (
        "a GCP-page form still defaults its machine type to e2-small")


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
    sys.exit(1 if failures else 0)
