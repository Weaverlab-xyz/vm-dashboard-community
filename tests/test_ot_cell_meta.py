"""Structural invariants of the OT cell deploy endpoints (api/ot.py).

The cell is a parent/child job pair: an ``ot_cell_deploy`` parent that drives one
VM-deploy child — ``gce_deploy`` / ``ec2_deploy`` / ``azure_deploy`` per cloud.
Both halves of that contract have failure modes that leave no trace at request
time, so they are pinned statically here (the same technique
tests/test_worker_dispatch.py uses), for every cloud's endpoint:

* the child MUST be created ``status="queued"`` — a pending deploy child would be
  claimed by the runner and deployed a SECOND time alongside the parent;
* the child's metadata must carry the keys the orchestrator and destroy path key
  off (``ot_cell``, ``ot_params``, plus everything that cloud's vm service
  ``run()`` rebuilds the deploy from) — a dropped key means wiring or teardown
  silently skips, or the runner KeyErrors mid-deploy;
* the parent's metadata must carry ``children`` — that key is what the repo-wide
  queued-children rule in test_worker_dispatch matches on — and ``cloud``, which
  is what run_cell_deploy dispatches on;
* the cell's VM must never get an external IP (GCP/Azure carry the flag; EC2 has
  none — the subnet decides, which the form and model docstring pin instead) and
  the GCP cell must carry the ``ot-sim`` tag (the tag-scoped Purdue-zoning hook).

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


# Every cloud's cell endpoint, its child job type, and the child-metadata keys that
# cloud's vm service run() (plus the OT orchestrator) reads back. GCP/Azure runners
# rebuild the request from "req"; the AWS runner reads flat keys.
_CLOUD_ENDPOINTS = {
    "gcp": ("deploy_cell", "gce_deploy",
            ("ot_cell", "ot_params", "req", "instance_name",
             "project_id", "zone", "region")),
    "aws": ("deploy_cell_aws", "ec2_deploy",
            ("ot_cell", "ot_params", "ami_id", "instance_name", "instance_type",
             "region", "subnet_id", "security_group_ids", "workgroup")),
    "azure": ("deploy_cell_azure", "azure_deploy",
              ("ot_cell", "ot_params", "req", "vm_name", "image_id",
               "location", "resource_group")),
}


def test_the_child_is_queued_and_carries_the_ot_keys():
    for cloud, (fn_name, child_type, needed_keys) in _CLOUD_ENDPOINTS.items():
        calls = list(_create_job_calls(_fn(fn_name)))
        child = [c for c in calls if c[0] == child_type]
        assert len(child) == 1, f"{cloud}: expected exactly one {child_type} create, got {calls}"
        _, status, keys = child[0]
        assert status == "queued", (
            f"{cloud}: the cell's {child_type} child must be created status='queued' — "
            "pending would be claimed by the runner and deployed twice")
        for needed in needed_keys:
            assert needed in keys, f"{cloud}: child metadata lost the {needed!r} key"


def test_the_parent_declares_its_children_and_cloud():
    for cloud, (fn_name, _child_type, _keys) in _CLOUD_ENDPOINTS.items():
        calls = list(_create_job_calls(_fn(fn_name)))
        parents = [c for c in calls if c[0] == "ot_cell_deploy"]
        assert len(parents) == 1, f"{cloud}: expected exactly one ot_cell_deploy create"
        assert "children" in parents[0][2], (
            f"{cloud}: the parent's metadata must carry `children` — the key the "
            "repo-wide queued-children rule (test_worker_dispatch) matches on")
        assert "cloud" in parents[0][2], (
            f"{cloud}: the parent's metadata must carry `cloud` — what "
            "run_cell_deploy dispatches the child's vm service on")


def test_every_cloud_persists_the_protocol_list():
    """A cell gets one PRA tunnel per protocol, and the worker reads the list off
    ot_params. An endpoint that persisted only the old singular `protocol` would
    silently give a multi-vendor cell exactly one tunnel."""
    for cloud, (fn_name, _child, _keys) in _CLOUD_ENDPOINTS.items():
        src = ast.unparse(_fn("_validate_cell_protocols"))
        assert "resolve_cell_protocols" in src, (
            "the endpoint no longer resolves the protocol list through ot_service")
        body = ast.unparse(_fn(fn_name))
        assert "'protocols': protocols" in body or '"protocols": protocols' in body, (
            f"{cloud}: ot_params does not carry the resolved `protocols` list")
        assert "_validate_cell_protocols" in body, (
            f"{cloud}: the endpoint does not validate its protocols, so a cell could "
            "be deployed asking for a protocol the image never simulates")


def test_the_cell_form_offers_only_simulated_protocols():
    """The picker is fed by cellPresets(), which filters on the `cell` flag the
    presets endpoint sends. Offering an unsimulated protocol would provision a
    tunnel to a dead port — a session failure that reads as a firewall block."""
    for page in ("gcp", "aws", "azure"):
        tmpl = open(os.path.join(_ROOT, "web_dashboard", "templates", page, "index.html"),
                    encoding="utf-8").read()
        assert "cellPresets()" in tmpl, (
            f"{page}: the cell protocol picker no longer filters to simulated protocols")
        assert "p.cell" in tmpl, (
            f"{page}: cellPresets() no longer reads the presets' `cell` flag")


def test_the_cell_vm_is_private_and_tagged():
    fn = _fn("deploy_cell")
    src = ast.unparse(fn)
    assert "create_external_ip=False" in src, (
        "the GCP cell VM must never get an external IP — access is PRA-brokered only")
    assert "'ot-sim'" in src or '"ot-sim"' in ast.dump(fn), (
        "the GCP cell VM must carry the ot-sim network tag (the Purdue-zoning hook)")
    # Azure has the same knob; EC2 has none (the subnet decides), which the model
    # docstring and the form's subnet help text pin instead.
    azure_src = ast.unparse(_fn("deploy_cell_azure"))
    assert "create_public_ip=False" in azure_src, (
        "the Azure cell VM must never get a public IP — access is PRA-brokered only")


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
    there silently reverts the model's default for every UI deploy. The AWS and
    Azure cells budget the same 4 GB (t3.medium / Standard_B2s)."""
    models = open(os.path.join(_ROOT, "web_dashboard", "models", "ot.py"),
                  encoding="utf-8").read()
    assert 'machine_type: str = "e2-medium"' in models, (
        "OTCellDeployRequest.machine_type is no longer e2-medium")
    assert 'instance_type: str = "t3.medium"' in models, (
        "OTCellDeployRequestAWS.instance_type is no longer t3.medium")
    assert 'vm_size: str = "Standard_B2s"' in models, (
        "OTCellDeployRequestAzure.vm_size is no longer Standard_B2s")
    gcp_tmpl = open(os.path.join(_ROOT, "web_dashboard", "templates", "gcp", "index.html"),
                    encoding="utf-8").read()
    assert "machine_type: 'e2-small'" not in gcp_tmpl, (
        "a GCP-page form still defaults its machine type to e2-small")
    # AWS/Azure: assert the OT form's own default is the 4 GB shape (presence, not
    # absence — the pages legitimately carry smaller types elsewhere, e.g. the
    # Packer BUILD instance).
    for page, ot_default in (("aws", "instance_type: 't3.medium'"),
                             ("azure", "vm_size: 'Standard_B2s'")):
        tmpl = open(os.path.join(_ROOT, "web_dashboard", "templates", page, "index.html"),
                    encoding="utf-8").read()
        assert ot_default in tmpl, (
            f"the {page}-page OT form no longer defaults to the 4 GB cell size ({ot_default})")


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
