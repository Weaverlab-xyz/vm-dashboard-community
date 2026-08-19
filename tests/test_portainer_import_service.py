"""Unit tests for the Portainer bundle import (``portainer_import`` job).

Exercises ``portainer_import_service.run_import`` against a stubbed
``portainer_service``, so the replay logic is tested without a Portainer or a DB.
The cases that matter are the REFUSALS — each one is something that would
otherwise look like a successful import:

  * environments are never created (they address a LAN host this node cannot reach)
  * an imported user is never an administrator, even if the bundle says so
  * stacks are skipped unless the operator names a live environment, because
    ``deploy_stack`` actually creates containers
  * source ids are translated, never reused — a source id addresses an unrelated
    object on the target
  * a name that already exists is MATCHED, so re-importing is a no-op

Runs under pytest or standalone:

    python tests/test_portainer_import_service.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Stub the app modules the service imports ─────────────────────────────────
_cfg_mod = types.ModuleType("web_dashboard.config")
_cfg_mod.settings = types.SimpleNamespace()
sys.modules["web_dashboard.config"] = _cfg_mod

_CONFIG = {"portainer_url": "https://node:9443"}
_cfgsvc = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc.get = lambda key, default=None: _CONFIG.get(key, "")
_cfgsvc.set = lambda key, val: _CONFIG.__setitem__(key, val)
_cfgsvc.get_bool = lambda key, default=False: bool(_CONFIG.get(key, default))
sys.modules["web_dashboard.services.config_service"] = _cfgsvc

#: Every job_service call the handler makes, in order.
_JOBS: dict = {}
_jobsvc = types.ModuleType("web_dashboard.services.job_service")
_jobsvc.set_running = lambda db, jid: _JOBS.setdefault("running", []).append(jid)
_jobsvc.update_progress = lambda db, jid, pct, msg: _JOBS.setdefault("progress", []).append((pct, msg))
_jobsvc.set_completed = lambda db, jid, result=None: _JOBS.__setitem__("completed", result)
_jobsvc.set_failed = lambda db, jid, error, result=None: _JOBS.__setitem__(
    "failed", {"error": error, "result": result})
sys.modules["web_dashboard.services.job_service"] = _jobsvc


class _StubPortainerError(Exception):
    pass


class FakePortainer:
    """A Portainer whose state is three lists. Mirrors the real client's contract:
    name matching is case-insensitive and ``add_team_member`` is idempotent."""

    USER_ROLE_ADMIN = 1
    USER_ROLE_STANDARD = 2
    TEAM_ROLE_LEADER = 1
    TEAM_ROLE_MEMBER = 2
    PortainerError = _StubPortainerError

    def __init__(self, users=None, teams=None, registries=None, fail=()):
        self.users = list(users or [])
        self.teams = list(teams or [])
        self.registries = list(registries or [])
        self.memberships = []
        self.stacks = []
        self.created_roles = {}
        self.fail = set(fail)
        self._next = 100

    def _id(self):
        self._next += 1
        return self._next

    async def find_user(self, username):
        t = username.strip().lower()
        return next((u for u in self.users
                     if str(u.get("Username", "")).strip().lower() == t), {})

    async def create_user(self, username, password, role=2):
        if "user" in self.fail:
            raise _StubPortainerError("user creation refused")
        rec = {"Id": self._id(), "Username": username, "Role": role}
        self.users.append(rec)
        self.created_roles[username] = role
        return rec

    async def find_team(self, name):
        t = name.strip().lower()
        return next((x for x in self.teams
                     if str(x.get("Name", "")).strip().lower() == t), {})

    async def create_team(self, name):
        if "team" in self.fail:
            raise _StubPortainerError("team creation refused")
        rec = {"Id": self._id(), "Name": name}
        self.teams.append(rec)
        return rec

    async def list_team_memberships(self):
        return list(self.memberships)

    async def add_team_member(self, user_id, team_id, role=2):
        for m in self.memberships:                      # idempotent, like the real one
            if m["UserID"] == user_id and m["TeamID"] == team_id:
                return m
        rec = {"Id": self._id(), "UserID": user_id, "TeamID": team_id, "Role": role}
        self.memberships.append(rec)
        return rec

    async def find_registry(self, name):
        t = name.strip().lower()
        return next((r for r in self.registries
                     if str(r.get("Name", "")).strip().lower() == t), {})

    async def create_registry(self, name, url, registry_type=3):
        rec = {"Id": self._id(), "Name": name, "URL": url, "Type": registry_type}
        self.registries.append(rec)
        return rec

    async def deploy_stack(self, endpoint_id, name, compose, env=None):
        if "stack" in self.fail:
            raise _StubPortainerError("stack deploy refused")
        self.stacks.append({"endpoint_id": endpoint_id, "name": name,
                            "compose": compose, "env": env})
        return {"Id": self._id(), "Name": name}


def _install(fake):
    sys.modules["web_dashboard.services.portainer_service"] = fake
    for mod in list(sys.modules):
        if mod.endswith("portainer_import_service"):
            del sys.modules[mod]
    import importlib
    return importlib.import_module("web_dashboard.services.portainer_import_service")


from web_dashboard.scripts.portainer_migrate import bundle as bundle_mod  # noqa: E402


def _bundle(**over):
    data = {"users": [], "teams": [], "team_memberships": [],
            "registries": [], "stacks": []}
    data.update({k: v for k, v in over.items() if k != "reference"})
    return bundle_mod.build(source_url="https://old:9443", source_version="2.21.0",
                            data=data, reference=over.get("reference") or {})


def _run(svc, doc, **meta):
    _JOBS.clear()
    meta.setdefault("bundle", doc)
    asyncio.run(svc.run_import(None, job_id="j1", meta=meta))
    return _JOBS


# ── the refusals ─────────────────────────────────────────────────────────────

def test_environments_in_the_bundle_are_never_created():
    """The single most important guarantee. An imported endpoint would address a
    Docker socket or LAN host this node has no route to, so it could only ever be a
    dead environment that LOOKS configured."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(teams=[{"Id": 1, "Name": "lab"}],
                  reference={"endpoints": [
                      {"Id": 1, "Name": "local", "URL": "unix:///var/run/docker.sock"},
                      {"Id": 2, "Name": "nas", "URL": "tcp://192.168.1.20:2375"}]})
    jobs = _run(svc, doc)
    assert "failed" not in jobs, jobs
    notes = " ".join(jobs["completed"]["notes"])
    assert "2 environment connection(s)" in notes, notes
    assert "Edge agent" in notes
    # And nothing endpoint-shaped was created anywhere.
    assert not hasattr(fake, "endpoints") or not getattr(fake, "endpoints", None)


def test_an_administrator_in_the_bundle_is_created_as_a_standard_user():
    """portainer_service.create_user ACCEPTS role 1, and a bundle is hand-editable
    input from another server — so the role must not come from the bundle."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 5, "Username": "root-ish", "Role": 1}])
    jobs = _run(svc, doc)
    assert fake.created_roles["root-ish"] == FakePortainer.USER_ROLE_STANDARD
    notes = " ".join(jobs["completed"]["notes"])
    assert "administrator in the source" in notes, notes


def test_the_nodes_own_admin_is_never_touched():
    """The dashboard holds this node's admin password; colliding with that account is
    how you lose access to the node."""
    fake = FakePortainer()
    svc = _install(fake)
    jobs = _run(svc, _bundle(users=[{"Id": 1, "Username": "admin", "Role": 1}]))
    assert fake.users == [], fake.users
    assert any("reserved" in s for s in jobs["completed"]["skipped"])


def test_stacks_are_skipped_without_a_target_environment():
    """deploy_stack CREATES containers — there is no store-without-running call — so
    with no environment there is nothing safe to do but skip and say why."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(stacks=[{"Id": 1, "Name": "web", "StackFileContent": "services: {}"}])
    jobs = _run(svc, doc)
    assert fake.stacks == []
    skipped = " ".join(jobs["completed"]["skipped"])
    assert "no target environment" in skipped, skipped
    assert "Edge agent" in skipped


def test_stacks_deploy_onto_a_named_environment():
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(stacks=[{"Id": 1, "Name": "web", "StackFileContent": "services: {}",
                           "Env": [{"name": "TAG", "value": "v1"}]}])
    _run(svc, doc, endpoint_id=4)
    assert len(fake.stacks) == 1
    assert fake.stacks[0]["endpoint_id"] == 4
    assert fake.stacks[0]["env"] == [{"key": "TAG", "value": "v1"}]


def test_a_stack_with_no_compose_text_is_skipped():
    """The exporter records an unreadable compose file as empty rather than dropping
    the stack, so the importer is where that has to be caught."""
    fake = FakePortainer()
    svc = _install(fake)
    jobs = _run(svc, _bundle(stacks=[{"Id": 1, "Name": "web", "StackFileContent": ""}]),
                endpoint_id=4)
    assert fake.stacks == []
    assert any("unreadable" in s for s in jobs["completed"]["skipped"])


# ── id translation ───────────────────────────────────────────────────────────

def test_membership_ids_are_translated_not_reused():
    """A source id addresses an unrelated object here. The mapping is the only safe
    way to join an imported user to an imported team."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 7, "Username": "alice", "Role": 2}],
                  teams=[{"Id": 3, "Name": "lab"}],
                  team_memberships=[{"Id": 1, "UserID": 7, "TeamID": 3, "Role": 2}])
    _run(svc, doc)
    alice = next(u for u in fake.users if u["Username"] == "alice")
    lab = next(t for t in fake.teams if t["Name"] == "lab")
    assert len(fake.memberships) == 1
    m = fake.memberships[0]
    assert (m["UserID"], m["TeamID"]) == (alice["Id"], lab["Id"])
    # The source ids must NOT have leaked through.
    assert (m["UserID"], m["TeamID"]) != (7, 3)


def test_a_membership_whose_user_did_not_import_is_skipped():
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(teams=[{"Id": 3, "Name": "lab"}],
                  team_memberships=[{"Id": 1, "UserID": 99, "TeamID": 3, "Role": 2}])
    jobs = _run(svc, doc)
    assert fake.memberships == []
    assert any("was not imported" in s for s in jobs["completed"]["skipped"])


def test_an_out_of_range_team_role_falls_back_to_member():
    """Portainer accepts any integer, and 1 is LEADER — silently granting leadership
    off a malformed bundle is the kind of thing nobody notices."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 7, "Username": "alice", "Role": 2}],
                  teams=[{"Id": 3, "Name": "lab"}],
                  team_memberships=[{"Id": 1, "UserID": 7, "TeamID": 3, "Role": 42}])
    _run(svc, doc)
    assert fake.memberships[0]["Role"] == FakePortainer.TEAM_ROLE_MEMBER


# ── merge semantics ──────────────────────────────────────────────────────────

def test_an_existing_membership_is_reported_as_matched_not_created():
    """add_team_member is idempotent and returns the EXISTING row rather than raising,
    so it gives the caller no way to tell "created" from "was already a member". Without
    a pre-read, a re-import reports memberships as created and the operator cannot see
    that nothing actually changed."""
    fake = FakePortainer(users=[{"Id": 11, "Username": "alice", "Role": 2}],
                         teams=[{"Id": 22, "Name": "lab"}])
    fake.memberships.append({"Id": 1, "UserID": 11, "TeamID": 22, "Role": 2})
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 7, "Username": "alice", "Role": 2}],
                  teams=[{"Id": 3, "Name": "lab"}],
                  team_memberships=[{"Id": 9, "UserID": 7, "TeamID": 3, "Role": 2}])
    jobs = _run(svc, doc)
    assert len(fake.memberships) == 1, fake.memberships
    assert jobs["completed"]["counts"]["created"] == 0, jobs["completed"]
    assert any("membership 11->22" in m for m in jobs["completed"]["matched"]),         jobs["completed"]["matched"]


def test_existing_names_are_matched_so_a_reimport_is_a_no_op():
    fake = FakePortainer(users=[{"Id": 1, "Username": "Alice", "Role": 2}],
                         teams=[{"Id": 2, "Name": "LAB"}],
                         registries=[{"Id": 3, "Name": "harbor"}])
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 7, "Username": "alice", "Role": 2}],
                  teams=[{"Id": 3, "Name": "lab"}],
                  registries=[{"Id": 4, "Name": "Harbor", "URL": "harbor.lan"}])
    jobs = _run(svc, doc)
    # Case-insensitive match, nothing duplicated.
    assert len(fake.users) == 1 and len(fake.teams) == 1 and len(fake.registries) == 1
    matched = jobs["completed"]["matched"]
    assert len(matched) == 3, matched
    assert jobs["completed"]["counts"]["created"] == 0


def test_generated_passwords_are_reported_and_unique():
    """Portainer never shows these again, so the job result is the only record."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 1, "Username": "a", "Role": 2},
                         {"Id": 2, "Username": "b", "Role": 2}])
    jobs = _run(svc, doc)
    pw = jobs["completed"]["generated_passwords"]
    assert set(pw) == {"a", "b"}
    assert pw["a"] != pw["b"]
    assert all(len(v) >= 12 for v in pw.values()), pw


def test_an_authenticated_registry_says_its_credential_did_not_travel():
    """A registry that looks configured but cannot pull is worse than an absent one."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(registries=[{"Id": 1, "Name": "harbor", "URL": "harbor.lan",
                               "Authentication": True, "Type": 3}])
    jobs = _run(svc, doc)
    notes = " ".join(jobs["completed"]["notes"])
    assert "re-enter" in notes and "harbor" in notes, notes


# ── failure handling ─────────────────────────────────────────────────────────

def test_a_partial_import_fails_the_job_but_keeps_what_worked():
    """Objects are independent, so partial is the normal shape of failure. The job
    page renders error_message only, which is why the failures go in the message."""
    fake = FakePortainer(fail={"user"})
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 1, "Username": "alice", "Role": 2}],
                  teams=[{"Id": 2, "Name": "lab"}])
    jobs = _run(svc, doc)
    assert "failed" in jobs, jobs
    assert "user alice" in jobs["failed"]["error"]
    # The team that DID land is still recorded.
    assert "team lab" in jobs["failed"]["result"]["created"]
    assert jobs["failed"]["result"]["counts"]["failed"] == 1


def test_an_unconfigured_portainer_fails_before_touching_anything():
    fake = FakePortainer()
    svc = _install(fake)
    _CONFIG["portainer_url"] = ""
    try:
        jobs = _run(svc, _bundle(teams=[{"Id": 1, "Name": "lab"}]))
    finally:
        _CONFIG["portainer_url"] = "https://node:9443"
    assert "No Portainer server is configured" in jobs["failed"]["error"]
    assert fake.teams == []


def test_an_invalid_bundle_is_rejected_before_any_write():
    """Re-validated in the job even though the route already did: a job may have been
    queued by an older build, and a half-applied import is worse than a rejected one."""
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(teams=[{"Id": 1, "Name": "lab"}])
    doc["schema"] = 99
    jobs = _run(svc, doc)
    assert "cannot be imported" in jobs["failed"]["error"]
    assert fake.teams == []


def test_a_missing_bundle_fails_cleanly():
    fake = FakePortainer()
    svc = _install(fake)
    _JOBS.clear()
    asyncio.run(svc.run_import(None, job_id="j1", meta={}))
    assert "no bundle" in _JOBS["failed"]["error"]


def test_summarize_is_pure_and_counts_only_importable_sections():
    fake = FakePortainer()
    svc = _install(fake)
    doc = _bundle(users=[{"Id": 1, "Username": "a"}],
                  reference={"endpoints": [{"Id": 1}, {"Id": 2}]})
    out = svc.summarize(doc)
    assert out["total"] == 1
    assert out["environments_seen"] == 2
    assert out["counts"]["users"] == 1


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"PASS {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
