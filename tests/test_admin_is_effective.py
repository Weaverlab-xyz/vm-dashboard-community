"""One admin rule: ``is_effective_admin``, everywhere a request decides "is this an admin?".

``User.is_admin`` is the checkbox on the Users tab. ``User.is_effective_admin`` is what
makes someone an administrator: that checkbox, OR a session-permissions row, OR a live
Entitle JIT grant, OR the built-in Administrator role. ``api/auth.require_admin`` and
``has_permission`` have always used the second.

Thirty-odd sites used the first. Among them: every cloud console's workgroup scope, every
hypervisor page's visibility, job cancel and reschedule, the Secrets page, the POV expiry
route, and the MCP cloud tools. So someone given the Administrator role, or granted admin
by Entitle, passed ``require_admin`` on one route and was scoped to their own workgroups on
the next, and could not cancel a stuck job. ``/api/auth/me`` already returns the effective
value, so the UI showed them admin menus the server then refused.

Two tests hold the line:

  * **Source scan.** No ``.is_admin`` read, and no ``getattr(x, "is_admin", ...)``, anywhere
    under ``web_dashboard/`` outside ``_ALLOWED`` below. Both spellings, because the audit
    that found the first 23 missed the eight ``getattr`` ones.
  * **Behaviour.** A user who holds admin only through the role is an administrator to a
    cloud console's scope, to job cancel, and to the MCP tools.

Run: python tests/test_admin_is_effective.py   (or under pytest)
"""
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-admin-effective")

_WEB = os.path.join(_ROOT, "web_dashboard")

# Where reading the raw column is RIGHT, each with its reason. Matched as (relpath, line
# substring) so a new read on another line of the same file still fails.
_ALLOWED = {
    # The column itself, and the property that ORs it with the other three sources.
    ("web_dashboard/database.py", ""),
    # The Users tab displays and edits the checkbox; `body.is_admin` is the request payload.
    ("web_dashboard/api/users.py", "is_admin=u.is_admin"),
    ("web_dashboard/api/users.py", "is_admin=user.is_admin"),
    ("web_dashboard/api/users.py", "body.is_admin"),
}

# `.is_admin` not followed by `_` (is_admin_groups) and not an assignment; and the getattr
# spelling with either quote.
_READ = re.compile(r"""\.is_admin\b(?!\s*=[^=])|getattr\(\s*\w+\s*,\s*["']is_admin["']""")


def _python_files():
    for dp, dirs, files in os.walk(_WEB):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                yield os.path.relpath(p, _ROOT).replace("\\", "/"), p


def _allowed(rel, line):
    return any(rel == path and (not frag or frag in line) for path, frag in _ALLOWED)


def test_no_code_reads_the_raw_admin_column():
    bad = []
    for rel, path in _python_files():
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                code = line.split("#", 1)[0]
                # Docstrings and comments name the column when explaining the rule; a code
                # read is what matters, and none of those lines is one.
                if "``" in code or code.strip().startswith(('"', "'")):
                    continue
                if _READ.search(code) and not _allowed(rel, line):
                    bad.append(f"  {rel}:{n}: {line.strip()}")
    assert not bad, ("these read the raw is_admin column, which ignores role and Entitle "
                     "admin grants -- use is_effective_admin:\n" + "\n".join(bad))


def test_the_scan_would_catch_both_spellings():
    """Pin the pattern, so a loosened regex cannot make the scan above vacuous."""
    assert _READ.search("if not user.is_admin and x:")
    assert _READ.search('bool(getattr(current_user, "is_admin", False))')
    assert not _READ.search("user.is_effective_admin")
    assert not _READ.search("user.is_admin = True")
    assert not _READ.search("is_admin_groups(groups)")


# ── behaviour ─────────────────────────────────────────────────────────────────

def _role_admin():
    """A user who is an administrator ONLY through the Administrator role's map."""
    from web_dashboard.database import User
    u = User(id="u-role-admin", username="role-admin", is_active=True, is_admin=False)
    u.workgroups_list = ["hydra"]
    u.role_id = "role-administrator"
    u.role_permissions_dict = {"is_admin": True}
    assert u.is_effective_admin and not u.is_admin
    return u


def _imports():
    try:
        from web_dashboard.api import aws, jobs, mcp_server  # noqa: F401
        return aws, jobs, mcp_server
    except Exception as exc:  # app deps absent outside CI
        print(f"  (skipped: {exc})")
        return None


def test_a_role_admin_is_unscoped_on_a_cloud_console():
    mods = _imports()
    if not mods:
        return
    aws, _, mcp_server = mods
    u = _role_admin()
    assert aws._accessible_workgroups(u) is None, "cloud console scoped a role admin"
    assert mcp_server._cloud_workgroups(u) is None, "MCP cloud tools scoped a role admin"


def test_a_role_admin_may_cancel_someone_elses_job():
    mods = _imports()
    if not mods:
        return
    _, jobs, _ = mods
    job = types.SimpleNamespace(id="j1", created_by="somebody-else", status="running")
    orig = (jobs.job_service.get_job, jobs.job_service.set_cancelled)
    jobs.job_service.get_job = lambda db, jid: job
    jobs.job_service.set_cancelled = lambda db, jid: types.SimpleNamespace(status="cancelled")
    try:
        out = jobs.cancel_job("j1", db=None, current_user=_role_admin())
        assert out["status"] == "cancelled"
    finally:
        jobs.job_service.get_job, jobs.job_service.set_cancelled = orig


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
