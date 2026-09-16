"""End-to-end: the /api/databases and /api/k8s routes honour a workgroup.

tests/test_workgroup_resource_tagging.py pins the predicate and the column. This file
drives the actual HTTP routes, because the two halves failed independently in the past:
the rule can be correct while a route forgets to call it, and
tests/test_k8s_db_action_ownership.py can only see that the guard is *mentioned*, not
that it *works*.

What is asserted here:

- A tagged row is LISTED for a workgroup member who did not create it. That is the
  capability being added; without it the change is a no-op.
- An untagged row is listed only for its creator. Every row predating the column is
  untagged, so this is the property that made the change safe to deploy.
- A by-id action on a row the caller cannot see answers **404**, not 403 — matching
  api/spire_lab and api/auth.require_pov_env_access. The list endpoint hides the row, so
  403 would confirm the existence of what RBAC just denied.
- The retag endpoint is admin-only, stores the canonical name, and accepts "" to clear.

Hermetic TestClient over a real throwaway SQLite, following the pattern in
tests/test_aws_cache_scope.py: a bare app carrying only the two routers, with
``get_current_user`` overridden by a switchable holder.

Runs under pytest, or standalone:  python tests/test_workgroup_resource_routes.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMP = tempfile.mkdtemp()
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(_TMP, "wgroutes.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the optional third-party deps by NAME; import first-party unguarded so a missing
# symbol fails loudly instead of printing SKIP and exiting 0.
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:  # pragma: no cover - environmental
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.api import cloud_databases as dbapi  # noqa: E402
from web_dashboard.api import k8s as k8sapi  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.database import (Base, CloudDatabase, K8sCluster,  # noqa: E402
                                    SessionLocal, Workgroup, engine, get_db)

Base.metadata.create_all(bind=engine)


class _User:
    is_admin = False
    is_effective_admin = False
    effective_permissions_dict: dict = {}      # empty == unrestricted (legacy compat)
    # require_admin refuses a POV accessor outright before it even looks at the admin
    # flag, so the stub has to carry this attribute or every admin route 500s on an
    # AttributeError that looks nothing like an authorization failure.
    accessor_env_id = None
    pov_env_ids_list: list = []

    def __init__(self, username, workgroups=(), admin=False):
        self.username = username
        self.workgroups_list = list(workgroups)
        self.is_admin = self.is_effective_admin = admin


ALICE = _User("alice", ["team-a"])          # creator of everything below
BOB = _User("bob", ["team-b"])              # outsider
CAROL = _User("carol", ["team-a"])          # in team-a, created nothing
ADMIN = _User("root", admin=True)

_WHO = {"user": ALICE}


def _current():
    return _WHO["user"]


def _session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_app = FastAPI()
_app.include_router(k8sapi.router)
_app.include_router(dbapi.router)
_app.dependency_overrides[get_current_user] = _current
_app.dependency_overrides[get_db] = _session
_client = TestClient(_app, raise_server_exceptions=False)

# The Databases router gates every route on the cloud_database feature flag, which reads
# config. Neutralised here: this file is about ownership, not about the flag, which
# tests/test_feature_gated_routers.py already covers.
dbapi._require_enabled = lambda: None


def _seed():
    db = SessionLocal()
    try:
        for model in (CloudDatabase, K8sCluster, Workgroup):
            db.query(model).delete()
        for name in ("team-a", "team-b"):
            db.add(Workgroup(id=f"wg-{name}", name=name, display_name=name.title()))
        # One tagged and one untagged of each, both created by alice.
        db.add(CloudDatabase(id="db-tagged", engine="postgres", cloud="aws",
                             status="available", created_by="alice",
                             workgroup="team-a"))
        db.add(CloudDatabase(id="db-plain", engine="mysql", cloud="gcp",
                             status="available", created_by="alice"))
        db.add(K8sCluster(id="k8s-tagged", cloud="gcp", name="tagged",
                          status="registered", source="registered",
                          created_by="alice", workgroup="team-a"))
        db.add(K8sCluster(id="k8s-plain", cloud="aws", name="plain",
                          status="registered", source="registered",
                          created_by="alice"))
        db.commit()
    finally:
        db.close()


def _as(user):
    _WHO["user"] = user


def _db_ids():
    r = _client.get("/api/databases")
    assert r.status_code == 200, r.text
    return sorted(d["id"] for d in r.json()["databases"])


def _cluster_ids():
    r = _client.get("/api/k8s/clusters")
    assert r.status_code == 200, r.text
    return sorted(c["id"] for c in r.json()["clusters"])


# ── Listing ───────────────────────────────────────────────────────────────────

def test_a_workgroup_member_is_listed_the_tagged_row_they_did_not_create():
    """THE capability. carol created nothing and is in team-a."""
    _seed()
    _as(CAROL)
    assert _db_ids() == ["db-tagged"], "carol must see the team-a database"
    assert _cluster_ids() == ["k8s-tagged"], "carol must see the team-a cluster"


def test_an_outsider_is_listed_nothing():
    _seed()
    _as(BOB)
    assert _db_ids() == []
    assert _cluster_ids() == []


def test_the_creator_still_sees_their_untagged_rows():
    """The upgrade-safety property, over HTTP. alice sees both: the team-a row by
    workgroup, the untagged one by creation."""
    _seed()
    _as(ALICE)
    assert _db_ids() == ["db-plain", "db-tagged"]
    assert _cluster_ids() == ["k8s-plain", "k8s-tagged"]


def test_an_admin_sees_everything():
    _seed()
    _as(ADMIN)
    assert _db_ids() == ["db-plain", "db-tagged"]
    assert _cluster_ids() == ["k8s-plain", "k8s-tagged"]


# ── By-id actions ─────────────────────────────────────────────────────────────

def test_a_by_id_read_on_an_invisible_row_is_404():
    """404 and not 403: the list endpoint hides this row, so 403 here would confirm it
    exists and make the id worth guessing."""
    _seed()
    _as(BOB)
    r = _client.get("/api/k8s/clusters/k8s-tagged")
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
    assert "403" not in str(r.status_code)


def test_a_workgroup_member_may_read_the_tagged_row_by_id():
    _seed()
    _as(CAROL)
    r = _client.get("/api/k8s/clusters/k8s-tagged")
    assert r.status_code == 200, r.text
    assert r.json()["workgroup"] == "team-a"


def test_a_member_of_the_wrong_workgroup_cannot_read_the_untagged_row_by_id():
    """carol is in team-a but did NOT create k8s-plain, which carries no workgroup."""
    _seed()
    _as(CAROL)
    assert _client.get("/api/k8s/clusters/k8s-plain").status_code == 404


def test_the_kubeconfig_route_refuses_before_it_builds_anything():
    """The sharpest case: this route returns a CLUSTER-ADMIN credential. An outsider must
    be refused, and refused as a 404."""
    _seed()
    _as(BOB)
    for path in ("/api/k8s/clusters/k8s-tagged/api-tunnel-kubeconfig",
                 "/api/k8s/clusters/k8s-tagged/entra-kubeconfig",
                 "/api/k8s/clusters/k8s-tagged/console"):
        r = _client.get(path)
        assert r.status_code == 404, f"{path}: expected 404, got {r.status_code}"


def test_a_destructive_by_id_action_on_an_invisible_row_is_404():
    """Before the guard, a non-admin holding the delete scope could decommission any
    cluster or database by id, including ones the list endpoint refused to show."""
    _seed()
    _as(BOB)
    assert _client.delete("/api/k8s/clusters/k8s-tagged").status_code == 404
    assert _client.delete("/api/databases/db-tagged").status_code == 404
    # ...and the rows are still there.
    _as(ADMIN)
    assert _cluster_ids() == ["k8s-plain", "k8s-tagged"]
    assert _db_ids() == ["db-plain", "db-tagged"]


def test_the_database_connection_route_is_guarded():
    _seed()
    _as(BOB)
    assert _client.get("/api/databases/db-tagged/connection").status_code == 404


# ── Retag ─────────────────────────────────────────────────────────────────────

def test_retag_is_admin_only():
    _seed()
    for user in (ALICE, CAROL, BOB):
        _as(user)
        r = _client.patch("/api/k8s/clusters/k8s-plain/workgroup",
                          json={"workgroup": "team-a"})
        assert r.status_code == 403, (
            f"{user.username} is not an admin: expected 403, got {r.status_code}")


def test_retag_stores_the_canonical_name_and_widens_the_list():
    """The whole point of the endpoint: an existing untagged row becomes shareable."""
    _seed()
    _as(ADMIN)
    r = _client.patch("/api/k8s/clusters/k8s-plain/workgroup",
                      json={"workgroup": "TEAM-A"})
    assert r.status_code == 200, r.text
    assert r.json()["workgroup"] == "team-a", (
        "must store the canonical lowercase name — a stored 'TEAM-A' would be invisible "
        "to every member of team-a")
    _as(CAROL)
    assert _cluster_ids() == ["k8s-plain", "k8s-tagged"], (
        "carol should now see the retagged cluster too")


def test_retag_accepts_blank_to_clear():
    """The only way back from a mis-tag, and something the AWS retag endpoint cannot
    express."""
    _seed()
    _as(ADMIN)
    r = _client.patch("/api/databases/db-tagged/workgroup", json={"workgroup": ""})
    assert r.status_code == 200, r.text
    assert r.json()["workgroup"] == ""
    _as(CAROL)
    assert _db_ids() == [], "cleared back to creator-scoped, so carol sees nothing"
    _as(ALICE)
    assert _db_ids() == ["db-plain", "db-tagged"], "its creator still sees it"


def test_retag_refuses_an_unknown_workgroup():
    _seed()
    _as(ADMIN)
    r = _client.patch("/api/databases/db-plain/workgroup", json={"workgroup": "nope"})
    assert r.status_code == 400, r.text


def test_retag_of_a_missing_row_is_404():
    _seed()
    _as(ADMIN)
    assert _client.patch("/api/k8s/clusters/nope/workgroup",
                         json={"workgroup": "team-a"}).status_code == 404


# ── Tagging on create ─────────────────────────────────────────────────────────

def test_registering_into_a_workgroup_you_are_not_in_is_refused():
    """403, distinguishable from the 400 an unknown name gets. And it matters: the
    workgroup branch outranks the creator branch, so this would hide the new cluster from
    the person creating it."""
    _seed()
    _as(ALICE)
    r = _client.post("/api/k8s/clusters", json={
        "name": "alices-cluster", "cloud": "local",
        "kubeconfig": "apiVersion: v1\nclusters:\n- cluster:\n    server: https://h:6443\n",
        "workgroup": "team-b",
    })
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_registering_with_an_unknown_workgroup_is_a_400():
    _seed()
    _as(ALICE)
    r = _client.post("/api/k8s/clusters", json={
        "name": "alices-cluster", "cloud": "local",
        "kubeconfig": "apiVersion: v1\nclusters:\n- cluster:\n    server: https://h:6443\n",
        "workgroup": "nope",
    })
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"


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
