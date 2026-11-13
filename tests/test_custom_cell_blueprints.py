"""Custom demo cell blueprints: what an item may say, who may author one, who sees it.

A blueprint is authored once by a Cell Builder and launched later by other people, so the
tests are mostly about the things a recipe must NOT be able to carry (a credential
reference, a name, a workgroup, its own schedule) and about the author/presenter split.

Runs standalone:
    python tests/test_custom_cell_blueprints.py
"""
import asyncio
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="custom-cell-bp-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "custom-cell-bp-test")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import custom_cell as api  # noqa: E402
from web_dashboard.api.demo_gates import require_blueprints  # noqa: E402
from web_dashboard.database import (Base, CellBlueprint, SessionLocal,  # noqa: E402
                                    User, Workgroup, engine)
from web_dashboard.services import custom_cell_service as svc  # noqa: E402
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)

GKE = {"kind": "k8s", "cloud": "gcp", "region": "us-central1", "label": "gke",
       "spec": {"node_count": 2}}
MYSQL = {"kind": "database", "cloud": "gcp", "region": "us-central1", "label": "db",
         "spec": {"engine": "mysql", "tier": "db-f1-micro"}}
UBUNTU = {"kind": "vm", "cloud": "gcp", "label": "web", "count": 2,
          "spec": {"image_self_link": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                   "zone": "us-central1-a", "machine_type": "e2-small"}}


def _db():
    db = SessionLocal()
    role_service.seed_builtins(db)
    for name in ("demo", "other"):
        if not db.query(Workgroup).filter(Workgroup.name == name).first():
            db.add(Workgroup(id=str(uuid.uuid4()), name=name, display_name=name))
    db.commit()
    return db


def _user(db, *roles, workgroups=("demo",), **cols):
    u = User(id=str(uuid.uuid4()), username=f"u-{uuid.uuid4().hex[:8]}",
             hashed_password="x", is_active=True, **cols)
    u.workgroups_list = list(workgroups)
    db.add(u)
    if roles:
        role_service.apply_roles_to_user(db, u, [role_service.get_by_slug(db, r) for r in roles])
    db.commit()
    return u


def _passes(dep, user) -> bool:
    try:
        asyncio.run(dep(current_user=user))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        return False


def _refused(fn, needle=""):
    try:
        fn()
    except svc.CellError as exc:
        assert needle.lower() in str(exc).lower(), str(exc)
        return True
    return False


# ── item validation ──────────────────────────────────────────────────────────

def test_the_example_cell_validates():
    """The cell the feature was asked for: GKE, MySQL and two Ubuntu VMs."""
    out = svc.validate_items([GKE, MYSQL, UBUNTU])
    assert [i["kind"] for i in out] == ["k8s", "database", "vm"]
    assert out[2]["count"] == 2
    assert out[1]["spec"]["engine"] == "mysql"


def test_items_on_different_clouds_validate_together():
    aws_vm = {"kind": "vm", "cloud": "aws", "region": "us-east-2", "label": "jump",
              "spec": {"ami_id": "ami-123", "subnet_id": "subnet-1",
                       "security_group_ids": ["sg-1"]}}
    assert len(svc.validate_items([GKE, aws_vm])) == 2


def test_a_spec_cannot_carry_a_credential_or_target_reference():
    for key in ("ssh_key_secret_override", "pra_credential_ref", "jump_group",
                "jumpoint_name", "vault_account_group_id", "ad_directory_id",
                "docker_deploy_key_ref"):
        item = dict(UBUNTU, spec=dict(UBUNTU["spec"], **{key: "x"}))
        assert _refused(lambda: svc.validate_item(item), key), key


def test_a_spec_cannot_name_the_resource_set_its_workgroup_or_schedule_itself():
    for key in ("instance_name", "workgroup", "run_at", "ttl_hours", "count"):
        item = dict(UBUNTU, spec=dict(UBUNTU["spec"], **{key: "x"}))
        assert _refused(lambda: svc.validate_item(item), key), key
    assert _refused(lambda: svc.validate_item(dict(GKE, spec={"name": "x"})), "name")


def test_the_clouds_own_model_refuses_what_its_form_would():
    no_image = dict(UBUNTU, spec={"zone": "us-central1-a"})
    assert _refused(lambda: svc.validate_item(no_image), "image_self_link")


def test_an_unknown_field_is_refused_rather_than_silently_dropped():
    item = dict(GKE, spec={"node_count": 2, "nodes": 3})
    assert _refused(lambda: svc.validate_item(item), "nodes")


def test_only_vms_take_a_count_and_it_is_bounded():
    assert _refused(lambda: svc.validate_item(dict(GKE, count=2)), "count")
    assert _refused(lambda: svc.validate_item(dict(UBUNTU, count=0)), "count")
    assert _refused(lambda: svc.validate_item(dict(UBUNTU, count=svc.MAX_VM_COUNT + 1)),
                    "count")


def test_k8s_and_database_items_need_a_region_and_a_real_engine():
    assert _refused(lambda: svc.validate_item(dict(GKE, region="")), "region")
    bad = dict(MYSQL, spec={"engine": "cassandra"})
    assert _refused(lambda: svc.validate_item(bad), "engine")


def test_labels_are_name_safe_and_unique():
    assert _refused(lambda: svc.validate_item(dict(GKE, label="My Cluster")), "label")
    assert _refused(lambda: svc.validate_items([GKE, dict(MYSQL, label="gke")]), "unique")


def test_an_empty_blueprint_is_refused():
    assert _refused(lambda: svc.validate_items([]), "at least one")


def test_the_suspend_schedule_and_ttl_use_the_shared_validators():
    assert _refused(lambda: svc.validate_schedule(
        {"suspend_at_local": "19:00", "resume_at_local": "19:00"}), "same")
    assert _refused(lambda: svc.validate_ttl(0), "ttl")
    assert svc.validate_ttl(8) == 8.0


# ── the role and the gates ───────────────────────────────────────────────────

def test_the_cell_builder_is_a_builtin_holding_everything_the_presenter_does():
    db = _db()
    builder = role_service.get_by_slug(db, "cell-builder")
    presenter = role_service.get_by_slug(db, "demo-presenter")
    assert builder is not None and builder.is_builtin
    for scope, levels in presenter.permissions_dict.items():
        assert set(levels) <= set(builder.permissions_dict.get(scope, [])), scope
    assert builder.permissions_dict["demo_blueprints"] == ["read", "write", "delete"]


def test_only_an_author_may_write_and_a_presenter_may_still_read():
    db = _db()
    builder = _user(db, "cell-builder")
    presenter = _user(db, "demo-presenter")
    both = _user(db, "demo-presenter", "cell-builder")
    viewer = _user(db, "auditor")
    for level in ("write", "delete"):
        assert _passes(require_blueprints(level), builder)
        assert _passes(require_blueprints(level), both)
        assert not _passes(require_blueprints(level), presenter), level
        assert not _passes(require_blueprints(level), viewer), level
    assert _passes(require_blueprints("read"), presenter), "a presenter must see what to launch"
    assert not _passes(require_blueprints("read"), viewer)


def test_full_access_does_not_invent_blueprint_authoring():
    from web_dashboard.api.auth import EXPLICIT_ONLY_LEVELS
    assert EXPLICIT_ONLY_LEVELS["demo_blueprints"] == ("read", "write", "delete")


# ── CRUD through the routes ──────────────────────────────────────────────────

def _body(**kw):
    base = dict(name=f"bp-{uuid.uuid4().hex[:6]}", items=[GKE, MYSQL, UBUNTU])
    base.update(kw)
    return api.BlueprintRequest(**base)


def test_create_read_update_delete():
    db = _db()
    me = _user(db, "cell-builder")
    out = api.create_blueprint(_body(workgroup="demo", ttl_hours=6,
                                     suspend_at_local="19:00", resume_at_local="07:00",
                                     schedule_timezone="America/Chicago"),
                               db=db, current_user=me)
    bid = out["id"]
    assert out["workgroup"] == "demo" and out["item_count"] == 3 and out["ttl_hours"] == 6

    got = api.get_blueprint(bid, db=db, current_user=me)
    assert [i["label"] for i in got["items"]] == ["gke", "db", "web"]

    upd = api.update_blueprint(bid, {"items": [GKE], "description": "just k8s"},
                               db=db, current_user=me)
    assert upd["item_count"] == 1 and upd["description"] == "just k8s"
    assert upd["ttl_hours"] == 6, "a partial update changed a field it was not sent"

    api.delete_blueprint(bid, db=db, current_user=me)
    assert db.query(CellBlueprint).filter(CellBlueprint.id == bid).first() is None


def test_a_bad_item_is_a_400_and_stores_nothing():
    db = _db()
    me = _user(db, "cell-builder")
    before = db.query(CellBlueprint).count()
    try:
        api.create_blueprint(_body(items=[dict(GKE, spec={"pra_credential_ref": "x"})]),
                             db=db, current_user=me)
        raise AssertionError("accepted a credential reference")
    except HTTPException as exc:
        assert exc.status_code == 400
    assert db.query(CellBlueprint).count() == before


def test_a_bad_update_leaves_the_blueprint_as_it_was():
    db = _db()
    me = _user(db, "cell-builder")
    bid = api.create_blueprint(_body(), db=db, current_user=me)["id"]
    try:
        api.update_blueprint(bid, {"name": "renamed", "items": []}, db=db, current_user=me)
        raise AssertionError("accepted an empty item list")
    except HTTPException as exc:
        assert exc.status_code == 400
    got = api.get_blueprint(bid, db=db, current_user=me)
    assert got["item_count"] == 3 and got["name"] != "renamed"


def test_another_workgroups_blueprint_is_a_404_not_a_403():
    db = _db()
    author = _user(db, "cell-builder", workgroups=("other",))
    stranger = _user(db, "cell-builder", workgroups=("demo",))
    bid = api.create_blueprint(_body(workgroup="other"), db=db, current_user=author)["id"]
    try:
        api.get_blueprint(bid, db=db, current_user=stranger)
        raise AssertionError("saw another workgroup's blueprint")
    except HTTPException as exc:
        assert exc.status_code == 404
    listed = api.list_blueprints(db=db, current_user=stranger)["blueprints"]
    assert bid not in [b["id"] for b in listed]


def test_you_cannot_file_a_blueprint_under_a_workgroup_you_are_not_in():
    db = _db()
    me = _user(db, "cell-builder", workgroups=("demo",))
    try:
        api.create_blueprint(_body(workgroup="other"), db=db, current_user=me)
        raise AssertionError("filed under a foreign workgroup")
    except HTTPException as exc:
        assert exc.status_code in (400, 403)


def test_the_router_is_mounted_behind_the_demo_page_gate():
    src = open(os.path.join(_ROOT, "web_dashboard", "main.py"), encoding="utf-8").read()
    i = src.index("custom_cell_api.router")
    assert '_profile_page_gate("demo_pages")' in src[i:i + 200]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
