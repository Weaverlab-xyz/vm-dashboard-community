"""The access-role abstraction, and the one hazard that runs through all of it.

``api/auth.has_permission`` treats an EMPTY effective permission map as **unrestricted** --
backward compatibility for the pre-OIDC users who never had one. Every failure mode of a
role feature therefore points the same way: a role that resolves to nothing does not deny,
it grants everything. So these tests are mostly about the states in which a role's map is
*absent* rather than the states in which it is set.

The design decision this file exists to pin is that a user's role reaches
``effective_permissions_dict`` through a **materialised column** and never through a
SQLAlchemy relationship. ``api/mcp_server._validate_pat`` loads a User, closes its session,
and keeps the instance in a ContextVar for the whole MCP session -- so the instance is
DETACHED while still authorising tool calls. A loaded column reads fine there; an unloaded
relationship raises ``DetachedInstanceError``, and the natural handler around that returns
``{}``, which is a total authorization bypass.
``test_a_detached_user_still_resolves_and_still_denies`` is the test that would catch
anybody reintroducing one.

Runs under pytest, or standalone:
    python tests/test_rbac_roles.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="rbac-roles-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-rbac-roles")

from sqlalchemy import inspect as sa_inspect  # noqa: E402

from web_dashboard.api.auth import (PERMISSION_LEVELS,  # noqa: E402
                                    PERMISSION_SCOPE_LEVELS, PERMISSION_SCOPES,
                                    has_explicit_permission, has_permission)
from web_dashboard.database import (AccessRole, Base,  # noqa: E402
                                    OAuthGroupMapping, SessionLocal, User,
                                    _ROLE_UNRESOLVED, engine)
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _session():
    """A clean slate, ROLES INCLUDED.

    The roles table has to be cleared too: these tests share one sqlite file, and a seed-count
    assertion run after another test had already seeded would be measuring that test's rows.
    Users and mappings go first so nothing references a role as it is removed.
    """
    db = SessionLocal()
    db.query(User).delete()
    db.query(OAuthGroupMapping).delete()
    db.query(AccessRole).delete()
    db.commit()
    return db


def _seeded(db):
    role_service.seed_builtins(db)
    return {r.slug: r for r in role_service.list_all(db)}


def _user(db, name=None, **kw):
    u = User(id=str(uuid.uuid4()), username=name or ("u-" + uuid.uuid4().hex[:8]),
             is_active=True, **kw)
    db.add(u)
    return u


# ── the empty-map hazard, in every combination ────────────────────────────────

def test_the_full_truth_table_of_empty_maps():
    """The one combination that may still read as unrestricted is "no role at all".

    Enumerated rather than spot-checked, because the interesting cases are the empty ones
    and there is more than one way to be empty.
    """
    db = _session()
    roles = _seeded(db)

    # 1. No role, nothing set -> unrestricted. LEGACY BEHAVIOUR, must not change.
    legacy = _user(db)
    assert legacy.effective_permissions_dict == {}
    assert has_permission(legacy, "aws", "write") is True, (
        "a pre-roles user with no permissions lost their backward-compatible full access")

    # 2. A role that grants something -> exactly that.
    ro = _user(db)
    role_service.apply_role_to_user(db, ro, roles["read-only"])
    assert has_permission(ro, "aws", "read") is True
    assert has_permission(ro, "aws", "write") is False

    # 3. role_id set, materialised copy MISSING -> deny, not unrestricted.
    orphan = _user(db)
    orphan.role_id = "no-such-role"
    assert orphan.effective_permissions_dict == _ROLE_UNRESOLVED
    assert has_permission(orphan, "aws", "read") is False

    # 4. Administrator role -> admin, via the is_admin term.
    adm = _user(db)
    role_service.apply_role_to_user(db, adm, roles["administrator"])
    assert adm.is_effective_admin is True
    assert has_permission(adm, "aws", "delete") is True

    # 5. A role plus a per-user grid -> the union, neither side lost.
    both = _user(db)
    role_service.apply_role_to_user(db, both, roles["read-only"])
    both.permissions_dict = {"aws": ["write"]}
    assert both.effective_permissions_dict["aws"] == ["read", "write"]
    db.commit()


def test_a_role_bearing_user_is_never_unrestricted():
    """No built-in, and no empty custom role, may leave its holder able to do everything."""
    db = _session()
    roles = _seeded(db)
    for slug, role in roles.items():
        u = _user(db)
        role_service.apply_role_to_user(db, u, role)
        if slug == role_service.ADMIN_ROLE_SLUG:
            assert u.is_effective_admin, "the administrator role must confer admin"
            continue
        perms = u.effective_permissions_dict
        assert perms, f"{slug} produced an EMPTY effective map, which means unrestricted"
        denied = [(s, l) for s in PERMISSION_SCOPES for l in PERMISSION_SCOPE_LEVELS[s]
                  if not has_permission(u, s, l)]
        assert denied, f"{slug} denies nothing at all — it is unrestricted in practice"


def test_a_detached_user_still_resolves_and_still_denies():
    """THE test that pins the materialised-copy design.

    `api/mcp_server._validate_pat` closes its session and keeps the User in a ContextVar, so
    permissions are evaluated on a detached instance for the life of an MCP session. A
    relationship read here would raise, and returning `{}` from a handler around it would
    make that principal UNRESTRICTED rather than refused.
    """
    db = _session()
    roles = _seeded(db)
    u = _user(db, "detached-probe")
    role_service.apply_role_to_user(db, u, roles["read-only"])
    db.commit()
    uid = u.id
    db.close()

    db2 = SessionLocal()
    loaded = db2.query(User).filter(User.id == uid).first()
    db2.close()

    assert sa_inspect(loaded).detached, "the fixture failed to produce a detached instance"
    # Must not raise:
    perms = loaded.effective_permissions_dict
    assert perms, "a detached role-bearing user resolved to an empty (unrestricted) map"
    assert has_permission(loaded, "aws", "read") is True
    assert has_permission(loaded, "aws", "write") is False


def test_the_deny_sentinel_is_not_a_real_scope():
    """So it can never grant, even if some route were gated on that string."""
    for key in _ROLE_UNRESOLVED:
        assert key not in PERMISSION_SCOPE_LEVELS, (
            f"{key!r} is in the permission catalog, so the deny sentinel is grantable")
        assert key not in PERMISSION_SCOPES


def test_the_sentinel_denies_the_explicit_form_too():
    """`require_explicit_permission` has no unrestricted clause, so it should already
    refuse -- asserted because the sentinel must not accidentally SATISFY a scope."""
    db = _session()
    u = _user(db)
    u.role_id = "gone"
    for scope in ("audit", "gateways", "storage"):
        assert has_explicit_permission(u, scope, "read") is False


# ── the built-in definitions ──────────────────────────────────────────────────

def test_every_builtin_grants_only_levels_its_scope_offers():
    """A level a scope does not offer is a grant nothing can enforce, and the API would
    422 the same map if an admin tried to save it by hand."""
    bad = []
    for spec in role_service._BUILTIN_ROLES:
        for scope, levels in spec["permissions"].items():
            if scope == "is_admin":
                continue
            offered = PERMISSION_SCOPE_LEVELS.get(scope)
            if offered is None:
                bad.append(f"{spec['slug']}: unknown scope {scope!r}")
                continue
            for level in levels:
                assert level in PERMISSION_LEVELS, f"{spec['slug']}: bogus level {level!r}"
                if level not in offered:
                    bad.append(f"{spec['slug']}: {scope}:{level} is not offered")
    assert not bad, "; ".join(bad)


def test_every_builtin_map_passes_the_role_validator():
    for spec in role_service._BUILTIN_ROLES:
        role_service.validate_role_permissions(spec["permissions"], slug=spec["slug"])


def test_the_read_only_role_covers_the_whole_catalog():
    """Every scope offers `read`, so "see everything, change nothing" is exhaustive or it
    is a silent gap -- and a gap in a built-in is held by everyone assigned it."""
    ro = next(s for s in role_service._BUILTIN_ROLES if s["slug"] == "read-only")
    missing = sorted(set(PERMISSION_SCOPES) - set(ro["permissions"]))
    assert not missing, f"read-only omits {missing}"
    for scope, levels in ro["permissions"].items():
        assert levels == ["read"], f"read-only grants {levels} on {scope}"


def test_the_builtins_do_not_derive_from_the_live_catalog():
    """Frozen literals on purpose. `seed_builtins` inserts ONCE, so a derived definition is
    frozen anyway -- at whatever the catalog held on the day each install first booted, which
    would leave two installs on one version holding two different `read-only` roles with
    nothing to point at."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "role_service.py"),
               encoding="utf-8").read()
    block = src.split("_BUILTIN_ROLES = (", 1)[1].split("\nBUILTIN_SLUGS", 1)[0]
    for banned in ("PERMISSION_SCOPES", "PERMISSION_SCOPE_LEVELS", "for scope in",
                   "levels_for_scope"):
        assert banned not in block, (
            f"the built-in definitions reference {banned} — they must be literals")


def test_only_the_administrator_builtin_may_grant_admin():
    """`has_permission` makes is_admin a total bypass, so an editable role carrying it turns
    the role editor into an admin-maker one hop removed."""
    carriers = [s["slug"] for s in role_service._BUILTIN_ROLES
                if s["permissions"].get("is_admin")]
    assert carriers == [role_service.ADMIN_ROLE_SLUG], (
        f"roles other than administrator grant the admin flag: {carriers}")

    for slug in ("operator", "read-only", "my-custom-role", None, ""):
        try:
            role_service.validate_role_permissions({"is_admin": True}, slug=slug)
        except role_service.RoleError:
            continue
        raise AssertionError(f"is_admin was accepted on role slug {slug!r}")
    # And allowed on the one that needs it.
    role_service.validate_role_permissions({"is_admin": True},
                                           slug=role_service.ADMIN_ROLE_SLUG)


# ── validation ────────────────────────────────────────────────────────────────

def test_a_role_with_no_map_is_refused_rather_than_defaulted():
    """On a role, empty means "grants nothing" -- the inverse of a user's column. A caller
    who omitted the field did not mean that, and a reader who later "fixes" empty to mean
    unrestricted would hand every assignee every scope."""
    for payload in (None, {}, "nope", 7, []):
        try:
            role_service.validate_role_permissions(payload)
        except role_service.RoleError:
            continue
        raise AssertionError(f"an empty/invalid role map was accepted: {payload!r}")


def test_the_role_validator_delegates_to_the_catalog_validator():
    """Unknown scope, unknown level, level-not-offered and a non-list value must all be
    refused -- and by the same implementation the user and group paths use, not a copy."""
    for bad in ({"nosuchscope": ["read"]},
                {"aws": ["fly"]},
                {"inventory": ["delete"]},      # inventory offers read only
                {"secrets": "use"}):            # a string turns `in` into a substring test
        try:
            role_service.validate_role_permissions(bad)
        except Exception:
            continue
        raise AssertionError(f"the role validator accepted {bad!r}")

    src = open(os.path.join(_ROOT, "web_dashboard", "services", "role_service.py"),
               encoding="utf-8").read()
    assert "validate_permissions_payload" in src, (
        "role_service no longer delegates to the catalog validator, so the two can drift")


# ── seeding ───────────────────────────────────────────────────────────────────

def test_the_seed_is_idempotent_and_never_updates_an_existing_row():
    """Keyed on slug presence, not on the table being empty -- so a ninth built-in in a
    later release actually lands. And it must never UPDATE, which is what makes it safe
    without a schema marker: it cannot re-grant something an admin removed."""
    db = _session()
    first = role_service.seed_builtins(db)
    assert first == len(role_service._BUILTIN_ROLES)
    assert role_service.seed_builtins(db) == 0, "the seed is not idempotent"

    ro = role_service.get_by_slug(db, "read-only")
    ro.permissions_dict = {"aws": ["read"]}
    db.commit()
    role_service.seed_builtins(db)
    assert role_service.get_by_slug(db, "read-only").permissions_dict == {"aws": ["read"]}, (
        "the seed overwrote an existing row, so it could re-grant a removed permission")

    # A missing one is restored without touching the others.
    db.delete(role_service.get_by_slug(db, "dba"))
    db.commit()
    assert role_service.seed_builtins(db) == 1


def test_the_seed_marks_its_rows_builtin():
    db = _session()
    roles = _seeded(db)
    for slug in role_service.BUILTIN_SLUGS:
        assert roles[slug].is_builtin, f"{slug} was not seeded as a built-in"


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_a_builtin_cannot_be_edited_or_deleted():
    db = _session()
    roles = _seeded(db)
    for slug in ("administrator", "read-only"):
        for call in (lambda r: role_service.update(db, r, name="Renamed"),
                     lambda r: role_service.update(db, r, description="x"),
                     lambda r: role_service.delete(db, r)):
            try:
                call(roles[slug])
            except role_service.RoleError:
                continue
            raise AssertionError(f"a built-in ({slug}) was mutated")


def test_cloning_the_administrator_role_is_refused():
    """A clone may not carry is_admin, so stripping it would leave an empty map -- and an
    empty role grants nothing. "Administrator copy" that confers no access is the most
    confusing possible outcome, so the refusal is the useful behaviour."""
    db = _session()
    roles = _seeded(db)
    try:
        role_service.clone(db, roles["administrator"], name="Admin copy")
    except role_service.RoleError:
        return
    raise AssertionError("the administrator role was cloned")


def test_a_clone_is_custom_and_carries_the_same_grants():
    db = _session()
    roles = _seeded(db)
    src = roles["dba"]
    copy = role_service.clone(db, src, name="DBA Plus")
    assert copy.is_builtin is False
    assert copy.permissions_dict == src.permissions_dict
    assert copy.slug != src.slug


def test_a_duplicate_role_name_is_refused():
    db = _session()
    _seeded(db)
    try:
        role_service.create(db, name="Read-Only", description=None,
                            permissions={"aws": ["read"]})
    except role_service.RoleError:
        return
    raise AssertionError("two roles were allowed to share a name")


def test_delete_is_refused_while_assigned_and_force_clears_both_columns():
    """The guard is in the service and not on the foreign key: the retrofit ALTER TABLE
    carries no REFERENCES clause, so an upgraded install has no constraint, and SQLite
    enforces none in this suite either way. A silent SET NULL would also leave
    role_permissions populated behind a NULL role_id -- drift nothing else would notice."""
    db = _session()
    _seeded(db)
    role = role_service.create(db, name="Temp Role", description=None,
                               permissions={"aws": ["read"]})
    u = _user(db)
    role_service.apply_role_to_user(db, u, role)
    mapping = OAuthGroupMapping(id=str(uuid.uuid4()), entra_group_id="gid-1",
                                display_name="G1", workgroup="default", role_id=role.id)
    db.add(mapping)
    db.commit()

    try:
        role_service.delete(db, role)
        raise AssertionError("a role in use was deleted without force")
    except role_service.RoleError as exc:
        assert "still assigned" in str(exc)

    cleared = role_service.delete(db, role, force=True)
    assert cleared == {"cleared_users": 1, "cleared_group_mappings": 1}
    db.refresh(u)
    db.refresh(mapping)
    assert u.role_id is None and u.role_permissions is None, (
        "force delete left a materialised copy behind a NULL role_id")
    assert mapping.role_id is None


def test_editing_a_role_fans_out_to_its_users_in_one_call():
    db = _session()
    _seeded(db)
    role = role_service.create(db, name="Fan Role", description=None,
                               permissions={"aws": ["read"]})
    holders = [_user(db) for _ in range(3)]
    for u in holders:
        role_service.apply_role_to_user(db, u, role)
    db.commit()

    fanned = role_service.update(db, role, permissions={"aws": ["read", "write"]})
    assert fanned == 3, f"fan-out reached {fanned} users, expected 3"
    for u in holders:
        db.refresh(u)
        assert has_permission(u, "aws", "write") is True


def test_reconcile_repairs_drift_and_clears_a_dangling_role():
    db = _session()
    roles = _seeded(db)
    u = _user(db)
    role_service.apply_role_to_user(db, u, roles["auditor"])
    db.commit()

    u.role_permissions_dict = {"aws": ["delete"]}
    db.commit()
    assert role_service.reconcile(db) >= 1
    db.refresh(u)
    assert u.role_permissions_dict == roles["auditor"].permissions_dict

    u.role_id = "vanished"
    db.commit()
    role_service.reconcile(db)
    db.refresh(u)
    assert u.role_id is None and u.role_permissions is None, (
        "reconcile left a role_id pointing at nothing — an admin cannot act on that")


def test_apply_role_to_user_is_the_only_writer_of_the_role_columns():
    """An id without its copy is a user who can do nothing; a copy without an id is a grant
    nothing names. They must always be written together, so exactly one function writes
    them."""
    offenders = []
    for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, "web_dashboard")):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
            if rel.endswith("services/role_service.py"):
                continue
            if rel.endswith("web_dashboard/database.py"):
                # The model's own `role_permissions_dict` setter -- the column ACCESSOR that
                # role_service is written in terms of, not a second call site. Exempting the
                # definition is not a hole: the point of this sweep is that no OTHER module
                # sets one column without the other, and a setter only ever writes its own.
                continue
            src = open(path, encoding="utf-8").read()
            for line in src.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ".role_permissions_dict =" in stripped or ".role_permissions =" in stripped:
                    offenders.append(f"{rel}: {stripped[:80]}")
    assert not offenders, (
        "role_permissions is written outside role_service, so the two columns can "
        f"disagree: {offenders}")


# ── the merge rule ────────────────────────────────────────────────────────────

def test_the_merge_rule_has_exactly_one_definition():
    """Three call sites had their own copy of this before a role made it four.

    They agreed, which is the only reason it never bit -- and a permission-merge rule that
    exists four times is one that will eventually be four different rules, silently, in
    whichever direction the copy someone edited happens to point. The sweep looks for the
    SHAPE of a hand-rolled merge (an is_admin OR beside a set-union of levels) anywhere but
    the one definition.
    """
    offenders = []
    for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, "web_dashboard")):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), _ROOT).replace(os.sep, "/")
            src = open(os.path.join(dirpath, name), encoding="utf-8").read()
            if rel.endswith("web_dashboard/database.py"):
                # The one definition lives here. Assert it is still the only merge in the
                # file rather than exempting the whole module.
                assert src.count("def merge_permission_maps(") == 1, (
                    "merge_permission_maps is defined more than once")
                body = src.split("def merge_permission_maps(", 1)[1]
                body = body.split("\ndef ", 1)[0]
                rest = src.replace(body, "")
                if 'sorted(set(' in rest and 'get("is_admin"' in rest:
                    offenders.append(rel + ": a second merge beside the definition")
                continue
            if 'or bool(val)' in src and "sorted(set(" in src:
                offenders.append(rel)
    assert not offenders, (
        "a hand-rolled permission merge outside database.merge_permission_maps — call the "
        "helper instead, or the two rules drift with nothing to fail: %s" % offenders)


def test_the_merge_is_order_independent_for_levels():
    """`reconcile` detects drift by comparing maps for equality, so the union has to be
    sorted rather than merely correct as a set."""
    from web_dashboard.database import merge_permission_maps
    a = merge_permission_maps({"aws": ["write"]}, {"aws": ["read"]})
    b = merge_permission_maps({"aws": ["read"]}, {"aws": ["write"]})
    assert a == b == {"aws": ["read", "write"]}, (a, b)


def test_the_merge_ors_the_admin_flag_in_both_directions():
    from web_dashboard.database import merge_permission_maps
    assert merge_permission_maps({"is_admin": True}, {"is_admin": False})["is_admin"] is True
    assert merge_permission_maps({"is_admin": False}, {"is_admin": True})["is_admin"] is True


# ── the group side ────────────────────────────────────────────────────────────

def test_a_group_mapping_carries_a_role_but_no_materialised_copy():
    """The column a group role feeds -- users.session_permissions -- is rebuilt from
    scratch on every login, so there is nothing to keep in step and no reconcile pass. A
    second copy would be a second writer for one fact."""
    cols = {c.name for c in OAuthGroupMapping.__table__.columns}
    assert "role_id" in cols
    assert "role_permissions" not in cols, (
        "a group mapping grew a materialised permission copy; the login path rebuilds "
        "session_permissions every time, so that copy has no reader and will drift")


def test_the_login_path_unions_a_mapped_groups_role():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "auth.py"),
               encoding="utf-8").read()
    body = src.split("def _complete_oauth_login(", 1)[1].split("\n\n\n", 1)[0]
    assert "role_id" in body, (
        "_complete_oauth_login ignores a mapping's role, so assigning one on the Groups "
        "tab would grant nothing")
    assert "AccessRole" in body


def test_the_role_editor_never_writes_the_login_owned_column():
    """`session_permissions` is overwritten wholesale on every login, so a grant written
    there by an admin vanishes at the user's next sign-in -- intermittently, with nothing to
    point at. That is why jit_permissions exists as its own column."""
    for rel in ("services/role_service.py", "api/roles.py"):
        src = open(os.path.join(_ROOT, "web_dashboard", *rel.split("/")),
                   encoding="utf-8").read()
        assert "session_permissions" not in src, (
            f"{rel} touches session_permissions, which the login path rewrites")


# ── the other axis ────────────────────────────────────────────────────────────

def test_roles_and_personas_never_learn_about_each_other():
    """A persona is curation and may NEVER gate -- it can arrive from an editable cookie. A
    role does nothing but gate. Merging them would make that cookie load-bearing."""
    personas_src = open(os.path.join(_ROOT, "web_dashboard", "services", "personas.py"),
                        encoding="utf-8").read()
    assert "role_service" not in personas_src, "personas imported role_service"
    assert "AccessRole" not in personas_src

    role_src = open(os.path.join(_ROOT, "web_dashboard", "services", "role_service.py"),
                    encoding="utf-8").read()
    for banned in ("personas", "persona"):
        assert f"import {banned}" not in role_src, f"role_service imports {banned}"
    assert ".persona" not in role_src, "role_service touches a persona column"


def test_no_roles_scope_was_added_to_the_catalog():
    """Role administration stays on the admin flag. A grantable `roles` scope would be a
    silent revocation for every explicitly-permissioned user (needing its own frozen
    backfill list and schema marker, to grant nothing, since the routes are new) AND an
    escalation primitive, since "edit the role N people hold" is one hop from "assign
    Administrator"."""
    for name in ("roles", "role", "rbac", "access_roles"):
        assert name not in PERMISSION_SCOPE_LEVELS, (
            f"a {name!r} permission scope was added — see api/roles.py for why it must not be")

    src = open(os.path.join(_ROOT, "web_dashboard", "api", "roles.py"),
               encoding="utf-8").read()
    assert "require_admin" in src
    for banned in ("require_permission(", "require_explicit_permission("):
        assert banned not in src, f"api/roles.py uses {banned}, not require_admin"


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
