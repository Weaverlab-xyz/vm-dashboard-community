"""The agent cell's contract: what it issues, what it records, and what it must never store.

AST-parsed the way tests/test_ot_cell_meta.py parses api/ot.py, because these are
structural properties a well-meaning refactor breaks without any test noticing:

  * **The raw token never reaches the row.** It is returned once in the create response
    and nowhere else. A column holding a working PAT would make every database backup a
    credential store — the rule ``SpireLab`` already states for its PKCS#12.
  * **The expiry is always set.** ``PersonalAccessToken.expires_at`` is nullable; this
    cell must never exercise that. A non-expiring token for a non-human principal is what
    the demo argues against.
  * **Every guard runs before anything is created.** A refusal after the PAT row exists
    has already minted a credential nothing records.
  * **The host is re-derived, never trusted.** Two privileged playbooks against a host of
    the caller's choosing is what ``spire_lab_service.resolve_host`` exists to prevent.
  * **Revoke does not uninstall.** The worker keeps running and is refused on its next
    poll — that is the demo, and tearing it down in the same action would remove it.

Runs under pytest, or standalone:
    python tests/test_agentcell_api.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-api")

_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")


def _src(path=_API):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(src: str) -> str:
    """Source with docstrings and comments stripped.

    Every assertion below that looks for the ABSENCE of a name needs this. This module
    explains at length why revoking does NOT tear anything down, so a raw substring
    search finds the explanation rather than a call — which is exactly the trap
    tests/test_workload_lab_governance._code documents having been caught by twice.
    """
    src = re.sub(r'"""".*?""""', "", src, flags=re.S)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))


def _calls(name, tree=None):
    tree = tree or ast.parse(_src())
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == name)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


def _kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


# -- the credential is issued, not stored --------------------------------------

def test_the_row_never_stores_the_raw_token():
    """The single most important assertion here."""
    row = _calls("AgentCell")
    assert row, "api/agentcell.py no longer creates an AgentCell row"
    for call in row:
        for kw in call.keywords:
            assert kw.arg != "token", "the AgentCell row is being given the raw token"
            if kw.arg and "token" in kw.arg and kw.arg != "token_hash":
                # pat_id / pat_name / pat_expires_at are fine; a bare token field is not.
                assert kw.arg.startswith("pat_"), \
                    f"AgentCell is given {kw.arg!r}, which looks like it carries a token"


def test_the_model_has_no_column_that_could_hold_a_token():
    block = _src(_DB).split("class AgentCell(Base):", 1)[1].split("\nclass ", 1)[0]
    cols = set(re.findall(r"^\s{4}(\w+)\s*=\s*Column", block, re.M))
    for banned in ("token", "pat", "pat_token", "secret", "raw_token"):
        assert banned not in cols, \
            f"AgentCell has a {banned!r} column — the dashboard is not the vault"
    # and the things it SHOULD record
    for required in ("pat_id", "pat_name", "pat_user_id", "pat_expires_at", "spiffe_id"):
        assert required in cols, f"AgentCell no longer records {required!r}"


def test_the_raw_token_is_returned_exactly_once():
    resp = _calls("AgentCellCreateResponse")
    assert len(resp) == 1, "expected one create response"
    assert _kw(resp[0], "token") is not None, \
        "the create response no longer returns the token — the operator could never " \
        "install the worker"


def test_the_token_is_minted_with_an_expiry():
    pat = _calls("PersonalAccessToken")
    assert pat, "the cell no longer mints a PAT"
    node = _kw(pat[0], "expires_at")
    assert node is not None, (
        "the agent's PAT is created without expires_at, so it never expires — the "
        "arrangement this whole cell argues against")
    assert not (isinstance(node, ast.Constant) and node.value is None), \
        "expires_at is explicitly None"


def test_the_hashing_is_the_tokens_modules_own():
    """Two ways of minting the same kind of token is two things to keep in step."""
    src = _src()
    assert "from .tokens import" in src and "hash_pat" in src, \
        "the cell hashes its own token instead of reusing api/tokens' helpers"
    assert "sha256" not in src, "the cell reimplements the token hashing"


# -- ordering: nothing is created before the guards run ------------------------

def test_every_guard_runs_before_the_token_is_minted():
    src = _src()
    mint = src.index("PersonalAccessToken(")
    for guard in ("host_problem", "mcp_problem", "pat_expiry_problem",
                  "trust_domain_problem", "pat_user_problem"):
        assert guard in src, f"api/agentcell.py never calls {guard}()"
        assert src.index(guard) < mint, \
            f"{guard}() is checked after the PAT is minted — a refusal would leave a " \
            "credential nothing recorded"


def test_the_host_is_resolved_rather_than_trusted():
    src = _src()
    assert "resolve_host" in src, (
        "the cell no longer re-derives its host. A supplied address would mean two "
        "privileged playbooks against a host of the caller's choosing")


def test_the_admin_refusal_is_wired_to_the_real_user():
    """The guard is only as good as what is passed to it."""
    src = _src()
    assert "is_effective_admin" in src, \
        "pat_user_problem is called without consulting the user's actual admin status"


# -- revoke is not uninstall ---------------------------------------------------

def test_revoke_clears_the_token_and_does_not_tear_down():
    src = _src()
    body = _code(src.split("def revoke_agent(", 1)[1])
    assert "is_active" in body, "revoking does not deactivate the PAT"
    for teardown in ("resolve_host", "ansible", "destroy", "terminate"):
        assert teardown not in body, (
            f"revoke touches {teardown!r} — it should revoke and stop there; the worker "
            "keeps running and is refused on its next poll, which is the demo")


def test_revoke_reports_what_the_operator_should_watch():
    body = _src().split("def revoke_agent(", 1)[1]
    assert "journalctl" in body, \
        "the revoke response does not say where to watch the worker stop"


# -- /options, which exists to feed the Workload Lab's Agent tab ---------------

def _options_body() -> str:
    src = _src()
    assert "def build_options(" in src, "api/agentcell.py has no /options route"
    return src.split("def build_options(", 1)[1].split("\n@router.", 1)[0]


def test_the_options_route_never_lists_an_administrator():
    """The one thing this route could leak that nothing else the caller can reach does.

    ``/api/users`` is admin-only, so the candidate list is genuinely new disclosure. It
    is bounded two ways and both matter: only a caller who could obtain the same names
    by minting sees it at all, and administrators are omitted rather than shown-disabled
    — which would hand a non-admin a roster of exactly which accounts hold admin.
    """
    body = _code(_options_body())
    assert "is_effective_admin" in body, (
        "the options route no longer filters administrators out of the candidate user "
        "list — `pat_user_problem` refuses them anyway, so listing them discloses who "
        "holds admin and buys nothing")
    assert "is_active" in body, "the options route offers deactivated users"


def test_the_options_route_needs_the_permission_that_could_mint():
    body = _options_body().split("\n", 12)
    head = "\n".join(body)
    assert 'require_permission("config_mgmt", "write")' in head, (
        "the options route is not gated on the permission that can actually mint an "
        "agent, so the user list reaches callers who could not obtain it by minting")


def test_the_options_route_returns_no_credential():
    """Same rule as every other surface on this feature: names and ids, never a secret.

    Worth pinning here specifically because this route reaches into the OTHER Workload
    Lab tabs to build the link picker, and those rows sit next to real vault
    coordinates.
    """
    body = _code(_options_body())
    for banned in ("token_hash", "_generate_raw", "raw", "password", "secret_value"):
        assert banned not in body, (
            f"the options route mentions {banned!r} — it lists what an agent may be "
            f"made answerable for, and the credential is never part of that")


def test_the_options_route_marks_an_unrotated_token_unlinkable():
    """The refusal `link_agent` makes, surfaced in the picker instead of on submit.

    A Kubernetes token whose first rotation never completed holds the placeholder it was
    created with. Offering it as a choice promises the agent something it cannot be
    given, and the operator finds out from a 400 with a room watching.
    """
    body = _code(_options_body())
    assert "rotated" in body and "linkable" in body, (
        "the options route does not report whether a Kubernetes token has rotated, so "
        "the picker offers one that `link_agent` will refuse")


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
