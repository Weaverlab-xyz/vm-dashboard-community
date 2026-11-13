"""The Demo hub (/demo), the `demo` scope and the Demo Presenter role.

What this file holds in place:

  * **`demo` only ever ADDS a path.** Every cell route still passes on the scope it took
    before (``<cloud>:write``, ``ot:read``), so swapping a route to ``require_cell`` cannot
    revoke anything -- which is the whole argument for the scope having no backfill.
  * **A Demo Presenter runs cells and deploys nothing else.** `demo:write` passes the cell
    gates; it does not pass ``gcp:write``, which is what ``POST /api/gcp/deploy`` takes.
  * **`demo` is explicit.** A map with no `demo` key never satisfies it.
  * **The agent cell is NOT on `demo`.** Minting one issues a token as any non-admin user
    the caller picks -- see api/demo_gates.
  * **The hub and the tiles read one list.** services/demo_cells feeds both.
  * **The hub is the estate profile's.** On a POV instance it 404s, like the cloud consoles.
  * **The launchpad links only to real pages,** and a masked entry carries no href.

Runs under pytest, or standalone:
    python tests/test_demo_hub.py
"""
import ast
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="demo-hub-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-demo-hub")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import demo_gates  # noqa: E402
from web_dashboard.api.auth import has_permission  # noqa: E402
from web_dashboard.database import Base, Job, SessionLocal, User, engine  # noqa: E402
from web_dashboard.services import (demo_cells, demo_launchpad,  # noqa: E402
                                    feature_flags, role_service)

Base.metadata.create_all(bind=engine)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _user(perms=None, *, admin=False, workgroups=("demo",)):
    u = User(id=str(uuid.uuid4()), username="u-" + uuid.uuid4().hex[:8], is_active=True,
             is_admin=admin)
    u.permissions = json.dumps(perms) if perms else None
    u.workgroups = json.dumps(list(workgroups))
    return u


def _presenter():
    spec = next(r for r in role_service._BUILTIN_ROLES if r["slug"] == "demo-presenter")
    return _user(spec["permissions"])


def _passes(dep, user) -> bool:
    try:
        asyncio.run(dep(current_user=user))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        return False


class _Profile:
    """Pin the install profile for the duration of a block."""

    def __init__(self, profile):
        self.profile = profile

    def __enter__(self):
        self._orig = feature_flags.install_profile
        feature_flags.install_profile = lambda: self.profile
        return self

    def __exit__(self, *exc):
        feature_flags.install_profile = self._orig


# ── the gates ────────────────────────────────────────────────────────────────

def test_the_original_scope_still_passes_every_cell_gate():
    legacy = _user({"gcp": ["read", "write"], "ot": ["read", "write"]})
    assert _passes(demo_gates.require_cell("gcp", "write"), legacy)
    assert _passes(demo_gates.require_cell("ot", "read"), legacy)
    assert _passes(demo_gates.require_cell("ot", "write"), legacy)


def test_demo_passes_a_cell_gate_without_the_cloud_scope():
    p = _presenter()
    assert _passes(demo_gates.require_cell("gcp", "write"), p)
    assert _passes(demo_gates.require_cell("aws", "write"), p)
    assert _passes(demo_gates.require_cell("azure", "write"), p)
    assert _passes(demo_gates.require_cell("ot", "read"), p)


def test_a_presenter_cannot_reach_the_general_cloud_routes():
    """The point of the scope: cells, and nothing else that deploys."""
    p = _presenter()
    for cloud in ("aws", "azure", "gcp", "oci"):
        for level in ("write", "delete"):
            assert not has_permission(p, cloud, level), f"presenter holds {cloud}:{level}"
    # Not config_mgmt:write either: that is the free choice of an agent's token user. A
    # presenter mints through `demo`, as a service account the cell creates.
    assert not has_permission(p, "config_mgmt", "write"), (
        "the presenter holds config_mgmt:write, so could mint an agent as any user")


def test_demo_is_explicit_and_levelled():
    nobody = _user({"vms": ["read"]})
    assert not demo_gates.demo_allows(nobody, "read")
    assert not _passes(demo_gates.require_cell("gcp", "write"), nobody)
    assert not _passes(demo_gates.require_demo_read, nobody)
    reader = _user({"demo": ["read"]})
    assert _passes(demo_gates.require_demo_read, reader)
    assert not _passes(demo_gates.require_cell("gcp", "write"), reader), (
        "demo:read opened a build route")
    assert not demo_gates.demo_allows(_user({"demo": ["read", "write"]}), "use"), (
        "a level `demo` does not offer was granted")
    assert _passes(demo_gates.require_demo_read, _user(admin=True))


def test_the_gate_keeps_the_original_scope_in_its_tag():
    dep = demo_gates.require_cell("gcp", "write")
    assert (dep.permission_scope, dep.permission_level) == ("gcp", "write")
    assert demo_gates.require_demo_read.permission_explicit is True


# ── which routes moved, and which did not ────────────────────────────────────

def _route_gates(rel):
    """{(method, path): source of the handler's signature} for one api module."""
    src = _read("web_dashboard", "api", rel)
    out = {}
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                body = ast.get_source_segment(src, node)
                out[(dec.func.attr, dec.args[0].value)] = body
    return out


def test_the_cell_routes_accept_demo():
    ot = _route_gates("ot.py")
    assert 'require_cell("gcp", "write")' in ot[("post", "/cell")]
    assert 'require_cell("aws", "write")' in ot[("post", "/cell/aws")]
    assert 'require_cell("azure", "write")' in ot[("post", "/cell/azure")]
    for path in ("/cell/{vm_job_id}/probe", "/cell/{vm_job_id}/rewire"):
        assert 'require_cell("ot", "write")' in ot[("post", path)], path
        assert 'cell=True' in ot[("post", path)], path
    assert 'cell=True' in ot[("delete", "/cell/{vm_job_id}")]
    assert 'cell=True' in ot[("get", "/cells")]
    net = _route_gates("netcell.py")
    assert 'require_cell("gcp", "write")' in net[("post", "/cell")]


def test_standalone_tunnels_do_not_accept_demo():
    """They wire real plant gear, not a demo cell."""
    ot = _route_gates("ot.py")
    for key in (("get", "/tunnels"), ("post", "/tunnels"), ("delete", "/tunnels/{slug}")):
        assert "cell=True" not in ot[key] and "require_cell" not in ot[key], key


def test_the_agent_cell_takes_demo_only_through_the_bounded_path():
    """`demo` reaches the agent cell, but never the free choice of token user: the
    presenter path makes its own service account. tests/test_demo_agent_cell.py holds
    that behaviour; this pins that the free choice is the operator's alone."""
    src = _read("web_dashboard", "api", "agentcell.py")
    assert "via_demo(current_user, \"config_mgmt\", \"write\")" in src, (
        "create_agent no longer tells a presenter apart from an operator -- a presenter "
        "would choose any non-admin user as the agent's token user")
    assert "_cell_service_account(" in src


# ── the shared list ──────────────────────────────────────────────────────────

def _job(db, job_type, meta, *, workgroup="demo", status="completed"):
    j = Job(id=str(uuid.uuid4()), job_type=job_type, workgroup=workgroup, status=status,
            created_by="someone")
    j.metadata_dict = meta
    db.add(j)
    return j


def _seed_cells(db):
    db.query(Job).delete()
    _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "plant-gcp"})
    _job(db, "ec2_deploy", {"ot_cell": True, "instance_name": "plant-aws"})
    _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "gone", "destroyed": True})
    _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "other-wg"}, workgroup="ops")
    _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "x"}, status="cancelled")
    _job(db, "gce_deploy", {"netcell": True, "instance_name": "vyos-1"})
    _job(db, "gce_deploy", {"instance_name": "ordinary-vm"})
    db.commit()


def test_ot_cells_are_filtered_like_the_ot_list():
    db = SessionLocal()
    try:
        _seed_cells(db)
        p = _presenter()
        res = demo_cells.ot_cells(db, lambda s: demo_gates.may(p, s, "read"), ["demo"])
        assert res["state"] == "ok"
        names = sorted(c["name"] for c in res["cells"])
        assert names == ["plant-aws", "plant-gcp"], names
        assert {c["manage_href"] for c in res["cells"]} == {"/aws#ot", "/gcp#ot"}
    finally:
        db.close()


def test_a_cloud_reader_sees_only_that_clouds_cells():
    db = SessionLocal()
    try:
        _seed_cells(db)
        u = _user({"gcp": ["read"]})
        res = demo_cells.ot_cells(db, lambda s: demo_gates.may(u, s, "read"), ["demo"])
        assert [c["name"] for c in res["cells"]] == ["plant-gcp"]
        none = _user({"vms": ["read"]})
        res = demo_cells.ot_cells(db, lambda s: demo_gates.may(none, s, "read"), ["demo"])
        assert res["state"] == "forbidden"
    finally:
        db.close()


def test_the_network_cell_preview_reads_unavailable_not_zero():
    db = SessionLocal()
    orig = feature_flags.enabled
    try:
        _seed_cells(db)
        p = _presenter()
        may_read = lambda s: demo_gates.may(p, s, "read")  # noqa: E731
        feature_flags.enabled = lambda f, d=False: False if f == "netcell_enabled" else orig(f, d)
        assert demo_cells.net_cells(db, may_read, ["demo"])["state"] == "unavailable"
        feature_flags.enabled = lambda f, d=False: True if f == "netcell_enabled" else orig(f, d)
        res = demo_cells.net_cells(db, may_read, ["demo"])
        assert [c["name"] for c in res["cells"]] == ["vyos-1"]
    finally:
        feature_flags.enabled = orig
        db.close()


def test_the_hub_and_the_tiles_count_the_same_rows():
    from web_dashboard.api import dashboard, demo
    db = SessionLocal()
    try:
        _seed_cells(db)
        p = _presenter()
        hub = demo.summary(db=db, current_user=p)
        tiles = dashboard._db_tiles(db, p)
        ot = next(c for c in hub["cells"] if c["kind"] == "ot")
        assert ot["total"] == tiles["ot_cells"]["value"] == 2
        assert tiles["ot_cells"]["href"] == "/demo#cells", (
            "a hub reader's tile should open the hub, which lists every cloud")
    finally:
        db.close()


def test_a_tile_keeps_its_own_link_for_someone_without_the_hub():
    from web_dashboard.api import dashboard
    db = SessionLocal()
    try:
        _seed_cells(db)
        u = _user({"gcp": ["read"]})
        assert dashboard._db_tiles(db, u)["ot_cells"]["href"] == "/gcp#ot"
    finally:
        db.close()


# ── the profile ──────────────────────────────────────────────────────────────

def test_the_hub_is_the_estate_profiles():
    assert feature_flags._PROFILE_PAGES.get("demo_pages") == "demo"
    with _Profile("pov"):
        assert not feature_flags.profile_page_allowed("demo_pages")
    with _Profile("demo"):
        assert feature_flags.profile_page_allowed("demo_pages")


def test_the_page_and_the_api_are_both_profile_gated():
    main = _read("web_dashboard", "main.py")
    page = main.split('@app.get("/demo"', 1)[1].split("async def", 1)[0]
    assert '_profile_page_gate("demo_pages")' in page, "/demo renders on a POV instance"
    mount = main.split("app.include_router(demo_api.router", 1)[1].split(")", 2)
    assert '_profile_page_gate("demo_pages"' in "".join(mount[:2]), (
        "/api/demo serves on a POV instance")


def test_the_nav_link_is_profile_and_scope_gated():
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    block = nav.split("{% if demo_pages %}", 1)[1].split("{% endif %}", 1)[0]
    assert 'data-nav="demo"' in block
    assert "$store.auth.can('demo', 'read')" in block


# ── the launchpad ────────────────────────────────────────────────────────────

def _html_routes():
    main = _read("web_dashboard", "main.py")
    return set(re.findall(r'@app\.get\("(/[^"{]*)"', main))


def test_every_launchpad_target_is_a_real_page():
    routes = _html_routes()
    for label, links in demo_launchpad.LAUNCHPAD:
        for link in links:
            path = link.target.split("#", 1)[0]
            assert path in routes, f"{label}/{link.id} links to {path}, which is no page"


def test_launchpad_ids_are_unique():
    ids = [link.id for _, links in demo_launchpad.LAUNCHPAD for link in links]
    assert len(ids) == len(set(ids)), ids


def test_a_masked_entry_carries_no_href():
    with _Profile("pov"):
        groups = demo_launchpad.describe()
    clouds = next(g for g in groups if g["label"] == "Clouds")
    for link in clouds["links"]:
        assert link["state"] == "masked" and link["target"] == "", link


def test_the_launchpad_never_drops_an_entry():
    """A hub surfaces; it never subtracts. Masked or not, every entry is listed."""
    total = sum(len(links) for _, links in demo_launchpad.LAUNCHPAD)
    with _Profile("pov"):
        assert sum(len(g["links"]) for g in demo_launchpad.describe()) == total


# ── the role ─────────────────────────────────────────────────────────────────

def test_the_presenter_role_is_seeded():
    spec = next(r for r in role_service._BUILTIN_ROLES if r["slug"] == "demo-presenter")
    assert spec["permissions"]["demo"] == ["read", "write", "delete"]
    role_service.validate_role_permissions(spec["permissions"], slug=spec["slug"])


# ── Phase 3: the pickers and the destroy, cell-scoped ─────────────────────────

def test_the_picker_routes_take_cloud_read_or_demo_write():
    dep = demo_gates.require_cell_options("gcp")
    assert _passes(dep, _user({"gcp": ["read"]})), "a GCP reader lost the cell pickers"
    assert _passes(dep, _presenter())
    assert not _passes(dep, _user({"demo": ["read"]})), (
        "demo:read lists the account's images -- only a builder needs them")
    assert not _passes(dep, _user({"aws": ["read"]})), "an AWS reader got GCP's pickers"


def test_every_picker_route_delegates_to_the_cloud_handler():
    """Same response shape as the cloud page reads, which is what lets one form serve both."""
    routes = _route_gates("demo.py")
    for path, call in (("/cells/gcp/custom-images", "gcp.list_custom_images("),
                       ("/cells/gcp/network-options", "gcp.network_options("),
                       ("/cells/aws/amis", "aws.list_amis("),
                       ("/cells/aws/network-options", "aws.network_options("),
                       ("/cells/azure/images", "azure.list_images("),
                       ("/cells/azure/network-options", "azure.network_options(")):
        body = routes[("get", path)]
        assert call in body, f"{path} does not delegate to {call}"
        assert f'require_cell_options("{path.split("/")[2]}")' in body, path
    # bust is a Query(False) default on two handlers: left unpassed it is truthy.
    assert "bust=False" in routes[("get", "/cells/gcp/network-options")]
    assert "bust=False" in routes[("get", "/cells/azure/network-options")]


class _Destroys:
    """Stub the three clouds' destroy handlers and record what was asked of them."""

    def __enter__(self):
        from web_dashboard.api import aws, azure, gcp
        self.calls, self._saved = [], (gcp.destroy_instance, aws.destroy_instance,
                                       azure.destroy_vm)
        gcp.destroy_instance = lambda **kw: self.calls.append(("gcp", kw["instance_name"])) or {}
        aws.destroy_instance = lambda **kw: self.calls.append(("aws", kw["instance_id"])) or {}

        async def _az(**kw):
            self.calls.append(("azure", kw["vm_name"]))
            return {}
        azure.destroy_vm = _az
        return self

    def __exit__(self, *exc):
        from web_dashboard.api import aws, azure, gcp
        gcp.destroy_instance, aws.destroy_instance, azure.destroy_vm = self._saved


def _destroy(job_id, user, kind="ot"):
    from web_dashboard.api import demo
    db = SessionLocal()
    try:
        fn = demo.destroy_ot_cell if kind == "ot" else demo.destroy_net_cell
        return asyncio.run(fn(job_id, db=db, current_user=user))
    finally:
        db.close()


def _status(fn):
    try:
        fn()
        return 200
    except HTTPException as exc:
        return exc.status_code


def test_a_presenter_destroys_a_cell_and_its_broker_second():
    db = SessionLocal()
    db.query(Job).delete()
    broker = _job(db, "ec2_deploy", {"ot_broker": True, "instance_id": "i-broker"})
    cell = _job(db, "ec2_deploy", {"ot_cell": True, "instance_id": "i-cell",
                                   "ot_broker_job_id": broker.id})
    db.commit(); cell_id = cell.id; db.close()
    with _Destroys() as d:
        _destroy(cell_id, _presenter())
    assert d.calls == [("aws", "i-cell"), ("aws", "i-broker")], d.calls


def test_destroy_refuses_what_is_not_a_live_cell_in_your_workstreams():
    db = SessionLocal()
    db.query(Job).delete()
    ids = {
        "ordinary": _job(db, "gce_deploy", {"instance_name": "web-1"}).id,
        "failed": _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "f"},
                       status="failed").id,
        "gone": _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "g",
                                        "destroyed": True}).id,
        "elsewhere": _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "e"},
                          workgroup="ops").id,
        "net": _job(db, "gce_deploy", {"netcell": True, "instance_name": "vyos"}).id,
    }
    db.commit(); db.close()
    p = _presenter()
    with _Destroys() as d:
        for name in ("ordinary", "failed", "gone", "elsewhere"):
            assert _status(lambda: _destroy(ids[name], p)) == 404, name
        assert _status(lambda: _destroy(ids["net"], p)) == 404, "an OT route destroyed a net cell"
        assert _status(lambda: _destroy(ids["ordinary"], p, "net")) == 404, (
            "the net-cell route destroyed an ordinary VM")
        assert d.calls == [], d.calls


def test_destroy_needs_cloud_delete_or_demo_delete():
    db = SessionLocal()
    db.query(Job).delete()
    cid = _job(db, "gce_deploy", {"ot_cell": True, "instance_name": "c", "zone": "z"}).id
    db.commit(); db.close()
    with _Destroys() as d:
        assert _status(lambda: _destroy(cid, _user({"demo": ["read", "write"]}))) == 403
        assert _status(lambda: _destroy(cid, _user({"gcp": ["read", "write"]}))) == 403
        assert _status(lambda: _destroy(cid, _user({"gcp": ["delete"]}))) == 200
        assert d.calls == [("gcp", "c")]


# ── Phase 3: one form, two pages ─────────────────────────────────────────────

def test_the_cloud_pages_and_the_hub_render_the_same_partials():
    for cloud in ("gcp", "aws", "azure"):
        page = _read("web_dashboard", "templates", cloud, "index.html")
        assert f'demo/_ot_cell_{cloud}.html' in page, f"/{cloud} no longer uses the partial"
        assert "Deploy a cell" not in page, f"/{cloud} carries its own copy of the form again"
        assert f"otCellMixin('{cloud}')" in page
        for dup in ("async submitOTCell(", "async destroyOTCell(", "otImages() {"):
            assert dup not in page, f"/{cloud} defines {dup} again -- it lives in demo_cells.js"
    hub = _read("web_dashboard", "templates", "demo", "index.html")
    for partial in ("_ot_cell_gcp", "_ot_cell_aws", "_ot_cell_azure", "_net_cell"):
        assert f"demo/{partial}.html" in hub, partial


def test_the_hub_has_one_ot_panel_with_a_cloud_toggle():
    hub = _read("web_dashboard", "templates", "demo", "index.html")
    assert "'key': 'ot'," in hub
    for per_cloud in ("ot-gcp", "ot-aws", "ot-azure"):
        assert per_cloud not in hub, f"{per_cloud} panel is back -- use otCloud"
    assert "otCloud = '{{ cloud }}'" in hub
    main = _read("web_dashboard", "main.py")
    assert '"ot": bool(ot_clouds)' in main and '"ot_clouds": ot_clouds' in main


def test_cell_destroy_goes_through_the_cell_route_on_every_page():
    js = _read("web_dashboard", "static", "js", "demo_cells.js")
    assert "/api/demo/cells/ot/" in js and "/api/demo/cells/net/" in js
    for general in ("/api/gcp/instances", "/api/aws/instances", "/api/azure/vms"):
        assert general not in js, f"a cell destroy calls the general {general} route"


def test_the_gcp_cell_sends_its_broker():
    """The GCP page's copy never sent the broker fields its form collected."""
    js = _read("web_dashboard", "static", "js", "demo_cells.js")
    gcp = js.split("gcp: (h, f) => ['/api/ot/cell', {", 1)[1].split("}],", 1)[0]
    for field in ("broker_image_self_link", "broker_image_name", "broker_machine_type"):
        assert field in gcp, f"the GCP cell payload drops {field}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
