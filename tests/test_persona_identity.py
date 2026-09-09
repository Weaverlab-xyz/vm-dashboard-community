"""A persona can be assigned to a USER or to their OIDC GROUP. Still curation only.

The persona layer resolved instance-wide until now. Binding it to an identity introduces
three failure modes, none of which announces itself:

  * **The login clobber.** ``_complete_oauth_login`` rewrites the group-derived column on
    every login -- that overwrite is load-bearing, because it is how losing an Entra group
    actually takes the focus away. An admin's deliberate per-user assignment written to the
    same column would be silently wiped at that user's next login. That is exactly why
    ``jit_permissions`` is separate from ``session_permissions``, and this layer copies the
    split. Nothing in this repo covered that interaction before this file.
  * **A non-deterministic group answer.** A user matched to several mappings must get the
    same focus every time, regardless of the order the database returned the rows in.
  * **Silent escalation.** The whole layer is safe only while a persona cannot grant or
    deny anything. An identity-bound persona sits next to real authorization columns now,
    so "it only reorders" has to be asserted rather than assumed.

Source-shape assertions parse files and import nothing. The behavioural ones import
personas, which needs only config_service and settings.

Runs under pytest, or standalone:
    python tests/test_persona_identity.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-persona-identity")

_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_AUTH = os.path.join(_ROOT, "web_dashboard", "api", "auth.py")
_USER_MODEL = os.path.join(_ROOT, "web_dashboard", "models", "user.py")
_PERSONAS = os.path.join(_ROOT, "web_dashboard", "services", "personas.py")
_APP_JS = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")
_LOGIN = os.path.join(_ROOT, "web_dashboard", "templates", "login.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _U:
    """Duck-typed user. personas.resolve_for_user must not need the real model."""

    def __init__(self, persona=None, session_persona=None):
        self.persona = persona
        self.session_persona = session_persona


class _M:
    """Duck-typed OIDC group mapping."""

    def __init__(self, persona=None, persona_priority=None, display_name=""):
        self.persona = persona
        self.persona_priority = persona_priority
        self.display_name = display_name


# ── the columns exist, and are separate on purpose ───────────────────────────

def test_both_user_columns_exist_and_are_migrated():
    src = _read(_DB)
    for col in ("persona", "session_persona"):
        assert re.search(r"^    %s = Column\(String\(32\)" % col, src, re.M), \
            f"users.{col} is not a String(32) column"
        assert f'"ALTER TABLE users ADD COLUMN {col} VARCHAR(32)"' in src, \
            f"users.{col} has no migration entry — existing databases never get it"


def test_the_group_mapping_carries_a_persona_and_a_priority():
    src = _read(_DB)
    block = src.split("class OAuthGroupMapping(Base)", 1)[1].split("\nclass ", 1)[0]
    assert "persona = Column(String(32)" in block
    assert "persona_priority = Column(Integer" in block
    assert '"ALTER TABLE oauth_group_mappings ADD COLUMN persona VARCHAR(32)"' in src
    assert '"ALTER TABLE oauth_group_mappings ADD COLUMN persona_priority INTEGER"' in src


def test_the_two_user_columns_are_documented_as_separate_writers():
    """The comment is the only thing that stops the next author collapsing them into one
    column, which would reintroduce the clobber."""
    src = _read(_DB)
    block = src.split("    persona = Column(String(32), nullable=True)", 1)[0][-1600:]
    assert "session_persona" in block and "login" in block.lower(), \
        "the persona columns carry no note about the login overwrite"


# ── the login clobber ────────────────────────────────────────────────────────

def test_the_login_path_never_writes_the_admin_set_column():
    """The failure this file exists for. `user.persona` is an admin's decision; the login
    path may only ever touch `user.session_persona`."""
    src = _read(_AUTH)
    body = src.split("def _complete_oauth_login", 1)[1].split("\nasync def ", 1)[0]
    offenders = [ln.strip() for ln in body.split("\n")
                 if re.search(r"\buser\.persona\s*=", ln)]
    assert not offenders, (
        "the login path assigns user.persona, which an admin set deliberately — it would "
        f"be wiped on every login: {offenders}")
    assert re.search(r"user\.session_persona\s*=", body), \
        "the login path never writes the group-derived column, so a group grants nothing"


def test_the_login_path_writes_the_group_column_unconditionally():
    """It must CLEAR as well as set. If the write were conditional on a persona having
    been matched, removing someone from a persona-granting group would leave the old focus
    in place forever — the same shape as the session_permissions overwrite it sits beside.
    """
    src = _read(_AUTH)
    body = src.split("def _complete_oauth_login", 1)[1].split("\nasync def ", 1)[0]
    writes = [ln for ln in body.split("\n") if re.search(r"user\.session_persona\s*=", ln)]
    assert len(writes) == 2, (
        f"expected the existing-user and auto-provision branches to both write it, "
        f"found {len(writes)}")
    for ln in writes:
        assert "matched_persona or None" in ln, (
            f"the write is not a plain overwrite, so a removed group would not clear it: "
            f"{ln.strip()}")


def test_the_group_persona_is_computed_where_the_group_ids_still_exist():
    """/api/auth/me has only the user row — it cannot re-derive which groups matched,
    because the login path maps ids to workgroups and then discards the ids."""
    src = _read(_AUTH)
    body = src.split("def _complete_oauth_login", 1)[1].split("\nasync def ", 1)[0]
    assert "personas.persona_for_groups(" in body
    i_compute = body.index("persona_for_groups(")
    i_write = body.index("user.session_persona")
    assert i_compute < i_write, "the persona is written before it is computed"


def test_the_env_fallback_path_confers_no_persona():
    """The .env group map is `{group_id: workgroup}` — it has no row to hang a persona on,
    so it must yield none rather than guessing."""
    src = _read(_AUTH)
    body = src.split("def _complete_oauth_login", 1)[1].split("\nasync def ", 1)[0]
    legacy = body.split("no mappings configured", 1)[1].split("\n\n", 1)[0]
    assert 'matched_persona = ""' in legacy, \
        "the legacy .env path leaves matched_persona undefined or set to a real persona"


# ── per-user resolution ──────────────────────────────────────────────────────

def test_the_user_assignment_outranks_the_group_one():
    from web_dashboard.services import personas as P
    assert P.resolve_for_user(_U(persona="ot", session_persona="dba")) == ("ot", "user")
    assert P.resolve_for_user(_U(session_persona="dba")) == ("dba", "group")


def test_an_unset_assignment_falls_through_to_the_instance_default():
    from web_dashboard.services import personas as P
    real = P.default_persona
    try:
        P.default_persona = lambda: "sre"
        assert P.resolve_for_user(_U()) == ("sre", "default")
        P.default_persona = lambda: P.NEUTRAL
        assert P.resolve_for_user(_U()) == (P.NEUTRAL, "none")
    finally:
        P.default_persona = real


def test_a_retired_persona_key_reads_as_unset_rather_than_as_itself():
    """A persona removed from the registry leaves rows behind. They must not resolve to a
    focus that no longer exists — and must not shadow the tier below them either."""
    from web_dashboard.services import personas as P
    real = P.default_persona
    try:
        P.default_persona = lambda: P.NEUTRAL
        assert P.resolve_for_user(_U(persona="was-a-persona")) == (P.NEUTRAL, "none")
        # And it falls THROUGH to the group column rather than blocking it.
        assert P.resolve_for_user(_U(persona="gone", session_persona="ot")) == ("ot", "group")
    finally:
        P.default_persona = real


def test_resolve_for_user_needs_no_database_model():
    """Duck-typed on purpose: personas.py is imported by jobs_worker, and the reason it
    lives in services/ is that it costs a process serving no requests nothing."""
    src = _read(_PERSONAS)
    assert "from ..database" not in src and "import database" not in src, \
        "personas.py imports the database layer; the worker pays for that import"


# ── group tie-break ──────────────────────────────────────────────────────────

def test_the_lowest_priority_number_wins():
    from web_dashboard.services import personas as P
    ms = [_M("ot", 20, "SE-OT-Team"), _M("cloudops", 10, "SE-CloudOps"), _M(None, 5, "SE-All")]
    assert P.persona_for_groups(ms) == "cloudops"


def test_a_mapping_with_no_persona_expresses_no_opinion():
    """What lets a broad catch-all group grant a workgroup without dictating a focus."""
    from web_dashboard.services import personas as P
    assert P.persona_for_groups([_M(None, 1, "SE-All")]) == P.NEUTRAL
    assert P.persona_for_groups([_M(None, 1, "SE-All"), _M("ot", 90, "SE-OT")]) == "ot"


def test_an_unprioritised_mapping_loses_to_a_prioritised_one():
    from web_dashboard.services import personas as P
    assert P.persona_for_groups([_M("ot", None, "A-OT"), _M("dba", 50, "Z-DBA")]) == "dba"


def test_the_answer_does_not_depend_on_row_order():
    """Databases do not promise a stable order for an unordered query, so the same set of
    memberships must produce the same focus however the rows arrive."""
    from web_dashboard.services import personas as P
    import itertools
    ms = [_M("ot", 20, "SE-OT"), _M("cloudops", 10, "SE-Cloud"), _M("dba", 20, "SE-DBA")]
    answers = {P.persona_for_groups(list(p)) for p in itertools.permutations(ms)}
    assert answers == {"cloudops"}, f"order-dependent result: {answers}"


def test_ties_break_on_display_name_deterministically():
    from web_dashboard.services import personas as P
    ms = [_M("ot", 10, "Zeta"), _M("dba", 10, "Alpha")]
    assert P.persona_for_groups(ms) == "dba"
    assert P.persona_for_groups(list(reversed(ms))) == "dba"


def test_an_unknown_persona_on_a_mapping_is_ignored():
    from web_dashboard.services import personas as P
    assert P.persona_for_groups([_M("nope", 1, "X")]) == P.NEUTRAL
    assert P.persona_for_groups([_M("nope", 1, "X"), _M("ot", 9, "Y")]) == "ot"


def test_no_mappings_is_neutral_not_an_error():
    from web_dashboard.services import personas as P
    assert P.persona_for_groups([]) == P.NEUTRAL
    assert P.persona_for_groups(None) == P.NEUTRAL


# ── the transport into the HTML render ───────────────────────────────────────

def test_an_explicit_pick_still_outranks_an_assignment():
    """An assignment is a default, not a lock. An SE presenting to a different role must
    be able to switch without asking an admin, and nothing is protected by pinning it."""
    from web_dashboard.services import personas as P

    class _R:
        def __init__(self, q=None, c=None):
            self.query_params = q or {}
            self.cookies = c or {}

    real = P.default_persona
    try:
        P.default_persona = lambda: P.NEUTRAL
        both = _R(c={"persona": "dba", "persona_assigned": "user:ot"})
        assert P.resolve(both) == ("dba", "cookie")
        # Clearing the explicit pick returns them to the assignment.
        assert P.resolve(_R(c={"persona_assigned": "user:ot"})) == ("ot", "user")
        assert P.resolve(_R(c={"persona_assigned": "group:sre"})) == ("sre", "group")
        # And ?persona= still beats everything, for one render.
        assert P.resolve(_R(q={"persona": "itops"},
                            c={"persona_assigned": "user:ot"})) == ("itops", "url")
    finally:
        P.default_persona = real


def test_a_malformed_assigned_cookie_is_ignored_rather_than_trusted():
    """Attacker-editable input on the request path."""
    from web_dashboard.services import personas as P

    class _R:
        def __init__(self, c):
            self.query_params = {}
            self.cookies = c

    real = P.default_persona
    try:
        P.default_persona = lambda: P.NEUTRAL
        for bad in ("ot", "user:", ":ot", "user:nope", "admin:ot", "", "user:ot:extra",
                    "<script>:ot", "USER:OT"):
            key, src = P.resolve(_R({"persona_assigned": bad}))
            assert key in P.VALID_PERSONAS or key == P.NEUTRAL, f"{bad!r} -> {key!r}"
            if bad == "USER:OT":
                # lowercased before parsing, so this one legitimately resolves
                assert (key, src) == ("ot", "user")
            elif bad != "user:ot:extra":
                assert key == P.NEUTRAL, f"{bad!r} was trusted -> {key!r}"
    finally:
        P.default_persona = real


def test_the_me_endpoint_returns_the_resolved_focus():
    """/api/auth/me is the hop both login paths already make; adding this costs no round
    trip and is the only place the server can answer 'whose nav is this?'."""
    src = _read(_AUTH)
    body = src.split('@router.get("/me"', 1)[1].split("\n@router.", 1)[0]
    assert "personas.resolve_for_user(current_user)" in body
    assert "persona=" in body and "persona_source=" in body
    model = _read(_USER_MODEL)
    block = model.split("class UserResponse", 1)[1].split("\nclass ", 1)[0]
    assert "persona: str" in block and "persona_source: str" in block


def test_the_client_writes_and_clears_the_assigned_cookie():
    src = _read(_APP_JS)
    body = src.split("function setAssignedPersona", 1)[1].split("\n}", 1)[0]
    assert "persona_assigned=" in body
    assert "max-age=0" in body, (
        "setAssignedPersona never clears the cookie — an admin removing an assignment "
        "would never take effect, because the stale value would outlive it")
    assert "samesite=lax" in body.lower()


def test_both_login_paths_seed_the_cookie():
    src = _read(_LOGIN)
    assert src.count("setAssignedPersona") == 2, (
        "the SSO fragment path and the password path must both seed it, or SSO users get "
        "no assigned focus")


def test_logout_clears_both_persona_cookies():
    """Per-browser state. The next person to log in on a shared machine must not inherit
    this user's focus — harmless, but it makes an assignment look broken."""
    src = _read(_APP_JS)
    body = src.split("logout() {", 1)[1].split("\n        },", 1)[0]
    assert "clearPersonaCookies()" in body
    clearer = src.split("function clearPersonaCookies", 1)[1].split("\n}", 1)[0]
    assert "persona_assigned=" in clearer and "max-age=0" in clearer
    assert re.search(r"'persona=;|\"persona=;", clearer), \
        "the explicit-pick cookie is not cleared, so it would outlive the session"


# ── the admin surfaces ───────────────────────────────────────────────────────

_USERS_API = os.path.join(_ROOT, "web_dashboard", "api", "users.py")
_GROUPS_API = os.path.join(_ROOT, "web_dashboard", "api", "groups.py")
_USERS_TPL = os.path.join(_ROOT, "web_dashboard", "templates", "users", "list.html")
_GROUPS_TPL = os.path.join(_ROOT, "web_dashboard", "templates", "groups", "index.html")
_DASHBOARD = os.path.join(_ROOT, "web_dashboard", "templates", "dashboard.html")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")


def test_both_apis_validate_the_persona_against_the_registry():
    """A free-text column here stores a focus that resolves to nothing and then reads as
    "unset" with no way to tell it from a group that never had one."""
    for path in (_USERS_API, _GROUPS_API):
        src = _read(path)
        assert "VALID_PERSONAS" in src, \
            f"{os.path.basename(path)} stores a persona without checking the registry"
        assert "422" in src


def test_clearing_the_user_assignment_is_expressible():
    """"" must clear and None must mean no change -- the same contract every other field
    on UserUpdateRequest has. Without the distinction an admin can assign but never
    un-assign, and the only way back would be editing the database."""
    src = _read(_USERS_API)
    body = src.split("if body.persona is not None:", 1)[1].split("\n    if ", 1)[0]
    assert "user.persona = want or None" in body, \
        "an empty persona does not clear the column, so an assignment cannot be removed"


def test_the_users_api_never_writes_the_group_column():
    """`session_persona` belongs to the login path. An admin writing it would have their
    change wiped at the user's next login, which looks like the save silently failing."""
    src = _read(_USERS_API)
    # WRITES, not mentions. Reading it is not only allowed but necessary: the users list
    # reports whether a focus came from the group, and an admin who cannot see that has no
    # way to understand why someone's nav differs. The first draft of this test forbade the
    # string outright and failed on exactly that read.
    writes = [ln.strip() for ln in src.split("\n")
              if re.search(r"\.session_persona\s*=(?!=)", ln)]
    assert not writes, (
        "api/users.py assigns session_persona, which the login path overwrites on every "
        f"login — the admin's change would silently vanish: {writes}")


def test_the_priority_is_not_stored_without_a_persona():
    """A priority on a mapping that expresses no focus is a value nothing ever reads."""
    src = _read(_GROUPS_TPL)
    body = src.split("async addMapping()", 1)[1].split("\n    },", 1)[0]
    assert "this.form.persona ?" in body, \
        "the group form sends a priority even when no focus is set"


def test_both_admin_pages_get_their_options_from_the_registry():
    """Injected, not hard-coded: the dropdown must not be able to offer a focus the
    resolver does not know, and no persona key may be spelled in a template."""
    main = _read(_MAIN)
    assert main.count('"persona_options"') == 2, \
        "persona_options is not injected into both the /users and /groups routes"
    assert "personas.all_personas()" in main
    for path in (_USERS_TPL, _GROUPS_TPL):
        assert "{{ persona_options | tojson }}" in _read(path), \
            f"{os.path.basename(path)} does not read the injected options"


def test_the_injected_name_cannot_be_overwritten_by_the_context_processor():
    """Starlette applies context processors AFTER the route's own context, so a route
    passing a name the processor also returns has it silently discarded. This is the trap
    tests/test_install_profile pins for flag names."""
    main = _read(_MAIN)
    body = main.split("def _profile_context(", 1)[1].split("\ntemplates = ", 1)[0]
    returned = set(re.findall(r'"([a-z_]+)":', body))
    assert "persona_options" not in returned, (
        "_profile_context returns 'persona_options', so the admin routes' injection would "
        "be silently overwritten")


def test_no_admin_template_hard_codes_a_persona_key():
    from web_dashboard.services import personas as P
    for path in (_USERS_TPL, _GROUPS_TPL, _DASHBOARD):
        src = _read(path)
        for key in P.VALID_PERSONAS:
            k = re.escape(key)
            near = (r"""persona[^\n]{0,80}['"]%s['"]""" % k,
                    r"""['"]%s['"][^\n]{0,80}persona""" % k)
            assert not any(re.search(pat, src, re.I) for pat in near), \
                f"{os.path.basename(path)} branches on the persona key {key!r}"


def test_the_lens_can_get_back_to_the_assignment():
    """Without this the lens is a one-way door: one pick permanently shadows an
    assignment, and re-picking the same key is NOT equivalent -- it stays an override, so
    a later reassignment would never reach that user."""
    src = _read(_DASHBOARD)
    assert "clearPersona()" in src
    body = src.split("clearPersona() {", 1)[1].split("\n      },", 1)[0]
    assert "max-age=0" in body, "clearPersona does not actually expire the cookie"
    assert "searchParams.delete('persona')" in body, \
        "clearPersona leaves ?persona= in the URL, which would immediately win again"
    # And it is only offered when there is something to clear.
    assert "persona.source === 'cookie'" in src


def test_the_lens_names_the_two_new_sources():
    src = _read(_DASHBOARD)
    body = src.split("personaSourceLabel() {", 1)[1].split("\n      },", 1)[0]
    for src_name in ("user", "group", "cookie", "url", "default"):
        assert f"{src_name}:" in body, f"the lens cannot explain the '{src_name}' source"


# ── still curation only ──────────────────────────────────────────────────────

def test_neither_column_can_grant_or_deny_anything():
    """The invariant the whole layer rests on, now that these columns sit beside real
    authorization ones. If a persona ever reaches the permission machinery, the editable
    cookie transport above becomes an escalation path."""
    for path in (_AUTH, os.path.join(_ROOT, "web_dashboard", "api", "users.py")):
        src = _read(path)
        for ln in src.split("\n"):
            if "persona" not in ln or ln.strip().startswith("#"):
                continue
            for token in ("is_admin", "permissions", "require_permission", "jit_"):
                assert token not in ln, (
                    f"{os.path.basename(path)}: a persona value shares a statement with "
                    f"{token!r}, which is authorization: {ln.strip()[:100]}")


def _method_body(src, name):
    """The body of one method, stopping at the next sibling or the end of the class.

    Bounded on the FIRST of several delimiters rather than just the next decorator: the
    method this is used on is the last property in its class, so an unbounded slice ran to
    the end of the file and reported `pov_use_case_progress.persona` as a hit. A guard that
    reads the wrong function is worse than no guard.
    """
    body = src.split(f"def {name}", 1)[1]
    ends = [body.index(d) for d in ("\n    @", "\n    def ", "\nclass ") if d in body]
    return body[:min(ends)] if ends else body


def test_the_persona_columns_are_not_in_effective_permissions():
    src = _read(_DB)
    for fn in ("effective_permissions_dict", "is_effective_admin"):
        body = _method_body(src, fn)
        assert "persona" not in body, \
            f"{fn} reads a persona column — curation must never reach authorization"
        assert "is_admin" in body, f"_method_body({fn!r}) sliced the wrong thing"


def test_feature_flags_still_knows_nothing_about_personas():
    """Unchanged from the original layer, re-asserted here because this slice adds two new
    callers and the import direction is what keeps the gate and the curation apart."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "feature_flags.py"))
    assert "persona" not in src.lower()


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
