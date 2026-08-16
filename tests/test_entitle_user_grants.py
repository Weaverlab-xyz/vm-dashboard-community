"""Permission grant/revoke rules for the dashboard's own Entitle adapter.

Pure logic, tested with nothing installed — deliberately, because this is the code
that decides who gets administrator, and gating that on a web framework being
present would mean it is only ever exercised in CI.

The rule everything here defends: **Entitle may only ever touch what Entitle
granted.** A dashboard user's permissions come from three independent sources, and
this module writes exactly one of them.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import entitle_user_grants as grants

LEVELS = ["read", "write", "delete", "use"]


class _User:
    """A stand-in with the three permission sources the real model exposes."""

    def __init__(self, username="alice", email="alice@example.com"):
        self.username = username
        self.email = email
        self.is_admin = False
        self._baseline = {}
        self._session = {}
        self._jit = {}

    # jit_permissions is the only one this module may write.
    @property
    def jit_permissions_dict(self):
        return dict(self._jit)

    @jit_permissions_dict.setter
    def jit_permissions_dict(self, value):
        self._jit = dict(value or {})

    @property
    def effective_permissions_dict(self):
        """Mirrors User.effective_permissions_dict: union of all three."""
        out = {}
        for src in (self._baseline, self._session, self._jit):
            for key, val in src.items():
                if key == "is_admin":
                    out[key] = out.get(key, False) or bool(val)
                elif isinstance(val, list):
                    out[key] = sorted(set(out.get(key, [])) | set(val))
                else:
                    out[key] = val
        return out

    @property
    def is_effective_admin(self):
        return (bool(self.is_admin) or bool(self._session.get("is_admin"))
                or bool(self._jit.get("is_admin")))


# ── Grant / revoke ───────────────────────────────────────────────────────────

def test_a_grant_lands_and_reports_the_change():
    user = _User()
    assert grants.grant(user, "aws", "write") is True
    assert user.jit_permissions_dict == {"aws": ["write"]}
    assert "write" in user.effective_permissions_dict["aws"]


def test_granting_twice_is_idempotent():
    user = _User()
    grants.grant(user, "aws", "write")
    assert grants.grant(user, "aws", "write") is False
    assert user.jit_permissions_dict == {"aws": ["write"]}


def test_levels_accumulate_and_revoke_individually():
    user = _User()
    for level in ("read", "write"):
        grants.grant(user, "aws", level)
    assert user.jit_permissions_dict == {"aws": ["read", "write"]}
    assert grants.revoke(user, "aws", "write") is True
    assert user.jit_permissions_dict == {"aws": ["read"]}


def test_revoking_the_last_level_drops_the_scope():
    user = _User()
    grants.grant(user, "aws", "write")
    grants.revoke(user, "aws", "write")
    assert user.jit_permissions_dict == {}


def test_revoking_what_is_not_held_is_a_no_op_not_an_error():
    """Entitle retries, and a revoke that errors leaves standing access behind."""
    user = _User()
    assert grants.revoke(user, "aws", "write") is False
    grants.grant(user, "aws", "read")
    assert grants.revoke(user, "aws", "write") is False
    assert user.jit_permissions_dict == {"aws": ["read"]}


def test_scopes_are_independent():
    user = _User()
    grants.grant(user, "aws", "write")
    grants.grant(user, "gcp", "write")
    grants.revoke(user, "aws", "write")
    assert user.jit_permissions_dict == {"gcp": ["write"]}


# ── The isolation rule ───────────────────────────────────────────────────────

def test_entitle_cannot_revoke_the_admin_set_baseline():
    """An operator's own grant must survive an Entitle revoke."""
    user = _User()
    user._baseline = {"aws": ["write"]}
    assert grants.revoke(user, "aws", "write") is False
    assert user._baseline == {"aws": ["write"]}
    assert "write" in user.effective_permissions_dict["aws"]


def test_entitle_cannot_revoke_a_group_derived_permission():
    """That is the group's to remove — and session_permissions is rewritten at every
    login anyway, so touching it here would be both wrong and futile."""
    user = _User()
    user._session = {"aws": ["delete"]}
    assert grants.revoke(user, "aws", "delete") is False
    assert user._session == {"aws": ["delete"]}


def test_a_grant_survives_a_login_rewriting_the_group_permissions():
    """_complete_oauth_login OVERWRITES session_permissions on every login. This is
    the reason jit_permissions exists as a separate column."""
    user = _User()
    grants.grant(user, "aws", "write")
    user._session = {"gcp": ["read"]}          # what a login does
    assert "write" in user.effective_permissions_dict["aws"]


def test_the_module_never_writes_the_other_two_sources():
    """Belt and braces: exercise every operation and assert the other two dicts are
    byte-identical afterwards."""
    user = _User()
    user._baseline = {"aws": ["read"]}
    user._session = {"gcp": ["read"]}
    before = (json.dumps(user._baseline, sort_keys=True),
              json.dumps(user._session, sort_keys=True))
    for scope in ("aws", "gcp", "k8s", grants.ADMIN_ASSET):
        grants.grant(user, scope, "write")
        grants.revoke(user, scope, "write")
        grants.revoke(user, scope, "read")
    after = (json.dumps(user._baseline, sort_keys=True),
             json.dumps(user._session, sort_keys=True))
    assert before == after


# ── Administrator ────────────────────────────────────────────────────────────

def test_admin_round_trips_through_its_own_asset():
    user = _User()
    assert grants.grant(user, grants.ADMIN_ASSET, grants.ADMIN_ROLE) is True
    assert user.is_effective_admin is True
    assert grants.revoke(user, grants.ADMIN_ASSET, grants.ADMIN_ROLE) is True
    assert user.is_effective_admin is False


def test_an_operator_set_admin_flag_survives_an_entitle_revoke():
    user = _User()
    user.is_admin = True
    grants.revoke(user, grants.ADMIN_ASSET, grants.ADMIN_ROLE)
    assert user.is_effective_admin is True


def test_admin_is_never_granted_by_a_scope_named_admin():
    """A scope literally called 'admin' must not become the is_admin flag — only
    the dedicated asset identifier does that."""
    user = _User()
    grants.grant(user, "admin", "write")
    assert user.jit_permissions_dict == {"admin": ["write"]}
    assert user.is_effective_admin is False


# ── Asset shape and resolution ───────────────────────────────────────────────

def test_scope_resolution_from_an_asset_identifier():
    assert grants.scope_from_asset(
        {"asset": {"identifier": "dashboard:scope:aws"}}) == "aws"
    assert grants.scope_from_asset(
        {"asset": {"identifier": grants.ADMIN_ASSET}}) == grants.ADMIN_ASSET


def test_an_unrecognised_asset_resolves_to_nothing():
    for identifier in ("", "something:else", "dashboard:", "scope:aws", None):
        assert grants.scope_from_asset({"asset": {"identifier": identifier}}) == ""
    assert grants.scope_from_asset({}) == ""


def test_assets_offer_every_level_as_a_role():
    asset = grants.asset_for("aws", LEVELS)
    assert asset["identifier"] == "dashboard:scope:aws"
    assert [o["code"] for o in asset["role_options"]] == LEVELS
    assert all(o["available"] for o in asset["role_options"])


def test_actor_matching_is_case_insensitive_on_username_or_email():
    user = _User("Alice", "Alice@Example.com")
    for identifier in ("alice", "ALICE", "  Alice ", "alice@example.com",
                       "Alice@Example.COM"):
        assert grants.matches_actor(user, identifier), identifier
    for identifier in ("bob", "", None, "alice@other.com"):
        assert not grants.matches_actor(user, identifier), identifier


def test_held_by_reports_only_entitles_own_grants():
    """Reporting the baseline or group sets would invite Entitle to reconcile away
    access it never granted."""
    user = _User()
    user._baseline = {"aws": ["delete"]}
    user._session = {"gcp": ["read"]}
    grants.grant(user, "azure", "write")
    grants.grant(user, grants.ADMIN_ASSET, grants.ADMIN_ROLE)
    assert sorted(grants.held_by(user)) == ["admin", "write"]


def test_the_module_is_importable_with_nothing_installed():
    """It must not reach for FastAPI, SQLAlchemy or the database — that is what
    makes this logic testable outside CI."""
    import ast
    tree = ast.parse(open(grants.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported, f"the grant rules pulled in {imported}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
