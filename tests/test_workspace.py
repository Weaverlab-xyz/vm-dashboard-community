"""The persona Workspace (/workspace): one landing page per estate role.

What this file holds in place:

  * **Every persona has a workspace,** and every workspace names a persona.
  * **Every link is a real page,** declares the flag that page is gated on, and every
    docs path is a real file -- the same checks the persona cards and the Demo launchpad
    are held to, because a workspace step IS a card.
  * **Every job type a step ticks on is one the code creates,** so a renamed job type
    fails here instead of leaving a step that can never tick.
  * **A step ticks on the caller's own COMPLETED job and nothing else.**
  * **Resources are the inventory's own RBAC.** Another workgroup's VM is absent, and
    without `inventory:read` the list is withheld.
  * **Curation only.** The page and API are profile-gated (estate only), never persona-
    or permission-gated; a bogus `?persona=` is never reflected; a masked link has no href.
  * **One-way imports:** workspaces imports personas, never the reverse, and nothing from
    the api layer.

Runs under pytest, or standalone:
    python tests/test_workspace.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="workspace-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-workspace")

from web_dashboard.database import (Base, Job, JobSchedule, SessionLocal, User,  # noqa: E402
                                    engine)
from web_dashboard.services import feature_flags, personas, workspaces  # noqa: E402

Base.metadata.create_all(bind=engine)

_DOCS = os.path.join(_ROOT, "docs")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


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


def _all_links():
    """(workspace key, UseCase) for every step and toolkit link."""
    for ws in workspaces.all_workspaces():
        for s in ws.steps:
            yield ws.key, s.card
        for _, links in ws.toolkit:
            for link in links:
                yield ws.key, link


# ── coverage ─────────────────────────────────────────────────────────────────

def test_every_persona_has_a_workspace():
    assert [w.key for w in workspaces.all_workspaces()] == list(personas.VALID_PERSONAS)


def test_every_workspace_names_a_persona():
    for key in workspaces._WORKSPACES:
        assert key in personas.VALID_PERSONAS, f"workspace {key!r} has no persona"


def test_cloudops_and_devops_lead_with_a_full_checklist():
    """The two roles this page was built for get the deepest getting-started lists."""
    for key in ("cloudops", "devops"):
        ws = workspaces._WORKSPACES[key]
        assert sum(1 for s in ws.steps if s.checkable) >= 5, key


def test_every_workspace_has_steps_and_a_toolkit():
    for ws in workspaces.all_workspaces():
        assert len(ws.steps) >= 3, ws.key
        assert ws.toolkit, ws.key
        assert ws.headline, ws.key


def test_ids_are_unique_within_a_workspace():
    for ws in workspaces.all_workspaces():
        ids = [s.card.id for s in ws.steps] + [link.id for _, ls in ws.toolkit for link in ls]
        assert len(ids) == len(set(ids)), (ws.key, ids)


# ── every link is real ───────────────────────────────────────────────────────

def _html_page_routes():
    """{path: decorator head} for every @app.get(..., HTMLResponse) route in main.py."""
    src = _read("web_dashboard", "main.py")
    out = {}
    for chunk in src.split("@app.get(")[1:]:
        path = chunk.split('"')[1]
        head = chunk.split("async def", 1)[0]
        if "HTMLResponse" in head:
            out[path] = head
    return out


def test_every_target_is_a_real_page():
    routes = _html_page_routes()
    for key, link in _all_links():
        if not link.target:
            continue
        path = link.target.split("#", 1)[0]
        assert path in routes, f"{key}/{link.id} links to {path}, which is no page"
    for path in workspaces.CLOUD_HOME.values():
        assert path.split("#", 1)[0] in routes, path


def test_every_fragment_is_a_real_tab():
    """`/aws#instances` only works if the page has an `instances` tab."""
    for key, link in _all_links():
        if "#" not in link.target:
            continue
        path, frag = link.target.split("#", 1)
        page = {
            "/aws": ("aws", "index.html"), "/azure": ("azure", "index.html"),
            "/gcp": ("gcp", "index.html"), "/containers": ("containers", "index.html"),
            "/workload-lab": ("workload_lab", "index.html"),
        }.get(path)
        assert page, f"{key}/{link.id}: no template mapped for {path}"
        src = _read("web_dashboard", "templates", *page)
        assert re.search(rf"""['"]{re.escape(frag)}['"]""", src), (
            f"{key}/{link.id} links to {link.target}, but {path} has no '{frag}' tab")


def test_every_docs_path_exists_on_disk():
    for ws in workspaces.all_workspaces():
        for s in ws.steps:
            if s.card.docs:
                assert os.path.exists(os.path.join(_DOCS, s.card.docs + ".md")), (
                    f"{ws.key}/{s.card.id} names docs '{s.card.docs}', which is no file")


def test_a_link_declares_the_flag_its_target_page_is_gated_on():
    gate_of = {}
    for path, head in _html_page_routes().items():
        m = re.search(r'_feature_gate\(\s*"(\w+)"\s*\)', head)
        if m:
            gate_of[path] = m.group(1)
    assert gate_of
    derived = feature_flags._DERIVED
    for key, link in _all_links():
        path = link.target.split("#")[0]
        flag = gate_of.get(path)
        if not flag:
            continue
        acceptable = set(derived.get(flag, (flag,)))
        declared = set(link.requires_flags) | set(link.requires_any_flag)
        assert acceptable & declared, (
            f"{key}/{link.id} targets {path} (gated on '{flag}') but requires none of "
            f"{sorted(acceptable)}")


def test_a_link_to_a_cloud_page_declares_the_cloud():
    gated = {"/aws", "/azure", "/gcp", "/oci", "/images"}
    for key, link in _all_links():
        if link.target.split("#")[0] in gated:
            assert link.requires_clouds, f"{key}/{link.id} targets a cloud page"
    for ws in workspaces.all_workspaces():
        for s in ws.steps:
            if s.cloud_home:
                assert s.card.requires_clouds, f"{ws.key}/{s.card.id} is cloud_home"


def test_a_masked_link_carries_no_href():
    with _Profile("pov"):
        for ws in workspaces.all_workspaces():
            out = workspaces.describe(ws.key)
            # The page and API are what the profile gates; describe() is not, so this
            # walks every link with the cloud consoles masked.
            for s in out.get("steps", []):
                if s["state"] == "masked":
                    assert s["target"] == "", s
            for g in out.get("toolkit", []):
                for link in g["links"]:
                    if link["state"] == "masked":
                        assert link["target"] == "", link


def test_the_toolkit_never_drops_a_link():
    """A workspace surfaces; masked or not, every entry is listed."""
    with _Profile("pov"):
        for ws in workspaces.all_workspaces():
            out = workspaces.describe(ws.key)
            assert (sum(len(g["links"]) for g in out["toolkit"])
                    == sum(len(ls) for _, ls in ws.toolkit))
            assert len(out["steps"]) == len(ws.steps)


# ── job types ────────────────────────────────────────────────────────────────

def _job_types_in_code():
    found = set()
    pat = re.compile(r"""job_type\s*(?:=|==)\s*["']([a-z0-9_]+)["']|"job_type":\s*"([a-z0-9_]+)\"""")
    base = os.path.join(_ROOT, "web_dashboard")
    for dirpath, _, files in os.walk(base):
        for name in files:
            if not name.endswith(".py") or name == "workspaces.py":
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                for m in pat.finditer(fh.read()):
                    found.add(m.group(1) or m.group(2))
    return found


def test_every_tick_job_type_is_one_the_code_creates():
    known = _job_types_in_code()
    assert "ec2_deploy" in known and "ansible_local" in known, "parser found nothing"
    for ws in workspaces.all_workspaces():
        for s in ws.steps:
            for t in s.done_job_types:
                assert t in known, f"{ws.key}/{s.card.id} ticks on unknown job type {t!r}"
        for t in ws.job_types:
            assert t in known, f"{ws.key} lists unknown job type {t!r}"


def test_step_done_rules():
    step = workspaces._WORKSPACES["cloudops"].steps[0]
    assert not workspaces.step_done(step, set(), set())
    assert workspaces.step_done(step, {"gce_deploy"}, set())
    assert not workspaces.step_done(step, {"ansible_local"}, set())
    sched = next(s for s in workspaces._WORKSPACES["devops"].steps if s.done_schedule)
    assert workspaces.step_done(sched, set(), {"ansible_local"})
    assert not workspaces.step_done(sched, set(), {"gce_deploy"})
    # The one-off run is the step before; it must not tick "make it recurring".
    assert not workspaces.step_done(sched, {"ansible_local"}, set())
    look = next(s for s in workspaces._WORKSPACES["security"].steps if not s.checkable)
    assert not workspaces.step_done(look, {"anything"}, {"anything"})


def test_progress_counts_only_checkable_steps():
    out = workspaces.describe("security")
    assert out["progress"]["total"] == sum(
        1 for s in workspaces._WORKSPACES["security"].steps if s.checkable)


# ── the API ──────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


def _user(perms=None, *, admin=False, workgroups=("ws-a",), persona=None):
    u = User(id=str(uuid.uuid4()), username="u-" + uuid.uuid4().hex[:8], is_active=True,
             is_admin=admin)
    u.permissions = json.dumps(perms) if perms else None
    u.workgroups = json.dumps(list(workgroups))
    u.persona = persona
    return u


def _job(db, *, user, job_type, status="completed", workgroup="ws-a", name=None):
    j = Job(id=str(uuid.uuid4()), job_type=job_type, status=status, workgroup=workgroup,
            created_by=user)
    j.extra_data = json.dumps({"instance_name": name or ("vm-" + uuid.uuid4().hex[:6]),
                               "region": "us-east-1"})
    db.add(j)
    db.commit()
    return j


def _summary(user, persona="", cookies=None):
    from web_dashboard.api import workspace as api
    from web_dashboard.services import cache_service

    async def run():
        await cache_service.invalidate(cache_service.key_global("deployment_inventory"))
        db = SessionLocal()
        try:
            return await api.summary(request=_Req(cookies), persona=persona, db=db,
                                     current_user=user)
        finally:
            db.close()
    with _Profile("demo"):
        return asyncio.run(run())


def test_no_persona_answers_the_chooser():
    out = _summary(_user({"inventory": ["read"]}))
    assert out["persona"] == ""
    assert [c["key"] for c in out["chooser"]] == list(personas.VALID_PERSONAS)


def test_the_assigned_persona_is_used():
    out = _summary(_user({"inventory": ["read"]}, persona="devops"))
    assert out["persona"] == "devops" and out["source"] == "user"


def test_url_beats_cookie_beats_assignment():
    u = _user({"inventory": ["read"]}, persona="devops")
    assert _summary(u, cookies={"persona": "dba"})["persona"] == "dba"
    assert _summary(u, persona="sre", cookies={"persona": "dba"})["persona"] == "sre"


def test_a_bogus_persona_is_never_reflected():
    u = _user({"inventory": ["read"]}, persona="cloudops")
    out = _summary(u, persona="<script>x</script>", cookies={"persona": "nope"})
    assert out["persona"] == "cloudops"
    assert "<script>" not in json.dumps(out)


def test_a_step_ticks_on_the_callers_own_completed_job_only():
    u = _user({"inventory": ["read"]}, persona="cloudops")
    other = "someone-" + uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        _job(db, user=other, job_type="ec2_deploy")                       # not theirs
        _job(db, user=u.username, job_type="ec2_deploy", status="failed")  # not completed
        out = _summary(u)
        first = next(s for s in out["steps"] if s["id"] == "cloudops-first-vm")
        assert not first["done"]
        _job(db, user=u.username, job_type="ec2_deploy")
        out = _summary(u)
        first = next(s for s in out["steps"] if s["id"] == "cloudops-first-vm")
        assert first["done"]
        assert out["progress"]["done"] == 1
        assert any(j["job_type"] == "ec2_deploy" for j in out["recent_jobs"])
    finally:
        db.close()


def test_a_schedule_ticks_the_recurring_step():
    u = _user({"inventory": ["read"]}, persona="devops")
    db = SessionLocal()
    try:
        db.add(JobSchedule(name="nightly", job_type="ansible_local", created_by=u.username,
                           **_schedule_required()))
        db.commit()
    finally:
        db.close()
    out = _summary(u)
    step = next(s for s in out["steps"] if s["id"] == "devops-schedule-playbook")
    assert step["done"]


def _schedule_required():
    """Fill whatever NOT NULL columns JobSchedule has beyond the three the test sets."""
    out = {}
    for col in JobSchedule.__table__.columns:
        if col.name in ("id", "name", "job_type", "created_by"):
            continue
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        py = getattr(col.type, "python_type", str)
        out[col.name] = {str: "{}", int: 0, bool: False}.get(py, "{}")
    return out


def test_resources_follow_the_inventorys_workgroup_rule():
    u = _user({"inventory": ["read"]}, persona="cloudops", workgroups=("ws-a",))
    mine, theirs = "mine-" + uuid.uuid4().hex[:6], "theirs-" + uuid.uuid4().hex[:6]
    db = SessionLocal()
    try:
        _job(db, user="x", job_type="ec2_deploy", workgroup="ws-a", name=mine)
        _job(db, user="x", job_type="ec2_deploy", workgroup="ws-b", name=theirs)
    finally:
        db.close()
    out = _summary(u)
    names = {r["name"] for r in out["resources"]}
    assert mine in names and theirs not in names
    assert all(r["href"] for r in out["resources"])


def test_resources_need_inventory_read():
    u = _user({"jobs": ["read"]}, persona="cloudops")
    out = _summary(u)
    assert out["resources_state"] == "forbidden" and out["resources"] == []


# ── gates ────────────────────────────────────────────────────────────────────

def test_the_workspace_is_the_estate_profiles():
    assert feature_flags._PROFILE_PAGES.get("workspace_pages") == "demo"
    with _Profile("pov"):
        assert not feature_flags.profile_page_allowed("workspace_pages")
        assert workspaces.chooser() == []


def test_the_page_and_the_api_are_profile_gated_and_nothing_else():
    main = _read("web_dashboard", "main.py")
    page = main.split('@app.get("/workspace"', 1)[1].split("async def", 1)[0]
    assert '_profile_page_gate("workspace_pages")' in page
    assert "_feature_gate" not in page and "require_permission" not in page
    mount = main.split("app.include_router(workspace_api.router", 1)[1].split(")", 2)
    assert '_profile_page_gate("workspace_pages"' in "".join(mount[:2])
    api = _read("web_dashboard", "api", "workspace.py")
    assert "require_permission" not in api, "a workspace is curation; it takes no scope"


def test_the_nav_link_is_profile_gated_only():
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    block = nav.split("{% if workspace_pages %}", 1)[1].split("{% endif %}", 1)[0]
    assert 'data-nav="workspace"' in block
    assert "$store.auth" not in block


def test_every_persona_pins_the_workspace_second():
    for p in personas.all_personas():
        assert p.nav_pins[:2] == ("dashboard", "workspace"), p.key


def test_the_dashboard_band_is_profile_gated():
    src = _read("web_dashboard", "templates", "dashboard.html")
    assert '<template x-if="features.workspace_pages">' in src
    assert "/api/workspace/summary" in src
    assert 'href="/workspace"' in src


def test_imports_run_one_way():
    ws_src = _read("web_dashboard", "services", "workspaces.py")
    assert "from ..api" not in ws_src and "import api" not in ws_src
    p_src = _read("web_dashboard", "services", "personas.py")
    assert "workspaces" not in p_src


if __name__ == "__main__":
    import inspect
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL", name, exc)
    sys.exit(1 if failed else 0)
