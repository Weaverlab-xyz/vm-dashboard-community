"""Pushing a spend budget into the cloud: what it will and will not touch.

This is the first thing in the tree that WRITES account-level configuration into somebody
else's cloud account, so most of what is pinned here is refusal rather than function.

Three guards carry the whole posture, and each has a test that fails if it is removed:

  * **A budget this dashboard did not name is never written to.** AWS budgets carry no
    tags, so a `vm-dashboard-` name prefix is doing a tag's job — a weaker guarantee, and
    the reason `assert_writable` raises rather than returning a bool somebody forgets to
    check.
  * **Nothing deletes a budget.** A cleared limit means the dashboard stops managing the
    number, not that a budget somebody relies on is torn out of their billing account.
    Asserted against the source, the way the unattributed cost list's read-only guard is.
  * **A budget with no subscriber is refused.** One that alerts nobody is the exact
    failure this feature exists to fix, so creating one is worse than doing nothing.

Run: python tests/test_provider_budget.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-provider-budget")

try:
    from web_dashboard.services import provider_budget as pb
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_PB_SRC = open(os.path.join(_ROOT, "web_dashboard/services/provider_budget.py"),
               encoding="utf-8").read()
_API_SRC = open(os.path.join(_ROOT, "web_dashboard/api/budgets.py"),
                encoding="utf-8").read()
_AWS_SRC = open(os.path.join(_ROOT, "web_dashboard/services/aws_service.py"),
                encoding="utf-8").read()

EMAILS = "ops@example.com, finance@example.com"


# ── The three guards ──────────────────────────────────────────────────────────

def test_a_budget_this_dashboard_did_not_name_is_never_written():
    """The ownership guard, and the weakest link by design — there is no tag to check."""
    for foreign in ("monthly-spend", "finance-fy26", "", "vmdashboard-monthly"):
        try:
            pb.assert_writable(foreign)
        except pb.BudgetError as exc:
            assert "not created by this dashboard" in str(exc)
        else:
            raise AssertionError(f"{foreign!r} was accepted as writable")
    # And the one it does own.
    pb.assert_writable(pb.budget_name("aws"))


def test_nothing_deletes_a_budget():
    """A cleared limit stops the dashboard managing the number. It does not remove a
    budget from somebody's billing account — the reapers' guard, one layer out."""
    for label, src in (("provider_budget", _PB_SRC), ("api/budgets", _API_SRC)):
        assert "delete_budget" not in src, f"{label} can delete a budget"
    # aws_service has exactly one delete in this feature, and it is of a NOTIFICATION on a
    # budget the dashboard owns — needed because there is no "set subscribers" call, so an
    # address removed in Settings would otherwise keep receiving alerts forever.
    assert "delete_budget" not in _AWS_SRC, "aws_service can delete a budget"
    assert _AWS_SRC.count("delete_notification") == 1


def test_a_budget_nobody_is_told_about_is_refused():
    try:
        pb.desired("aws", 500, emails="")
    except pb.BudgetError as exc:
        assert "notification email" in str(exc)
        assert "not running" in str(exc), "say WHY an outside address is the point"
    else:
        raise AssertionError("a budget with no subscriber was accepted")


# ── Refusals that are not failures ────────────────────────────────────────────

def test_no_configured_limit_is_refused_without_suggesting_a_deletion():
    """Zero means "not managed here", and the message must not imply the opposite."""
    for empty in (0, 0.0, None, "", "not-a-number"):
        try:
            pb.desired("aws", empty, emails=EMAILS)
        except pb.BudgetError as exc:
            assert "does not remove a budget already in the cloud" in str(exc)
        else:
            raise AssertionError(f"limit {empty!r} produced a budget")


# ── The document ──────────────────────────────────────────────────────────────

def test_the_name_is_deterministic_so_a_second_push_updates_the_first():
    assert pb.budget_name("aws") == pb.budget_name("aws")
    assert pb.budget_name("aws").startswith(pb.NAME_PREFIX)
    assert pb.budget_name("aws") != pb.budget_name("gcp")


def test_the_desired_document_carries_what_the_provider_needs():
    want = pb.desired("aws", 500.256, currency="usd", emails=EMAILS, threshold=90)
    assert want["limit"] == 500.26
    assert want["currency"] == "USD"
    assert want["time_unit"] == "MONTHLY"
    assert want["threshold_percent"] == 90
    assert want["emails"] == ["ops@example.com", "finance@example.com"]


def test_a_nonsensical_threshold_is_clamped_rather_than_fatal():
    """A bad percentage should not stop a budget existing — the budget is the point."""
    assert pb.alert_percent(0) == pb.MIN_ALERT_PERCENT
    assert pb.alert_percent(9999) == pb.MAX_ALERT_PERCENT
    assert pb.alert_percent("eighty") == pb.DEFAULT_ALERT_PERCENT
    assert pb.alert_percent(None) == pb.DEFAULT_ALERT_PERCENT


def test_addresses_are_split_on_whatever_separator_someone_typed():
    for raw in ("a@x.com,b@x.com", "a@x.com b@x.com", "a@x.com; b@x.com",
                " a@x.com , b@x.com "):
        assert pb.parse_emails(raw) == ["a@x.com", "b@x.com"], raw
    # Junk between real addresses is dropped rather than sent to the provider.
    assert pb.parse_emails("a@x.com, , nonsense") == ["a@x.com"]


# ── The diff, which is what an operator reads before pressing the button ──────

def test_an_absent_budget_reads_as_a_create():
    want = pb.desired("aws", 500, emails=EMAILS)
    assert pb.diff(None, want)["action"] == "create"


def test_an_identical_budget_reads_as_unchanged():
    want = pb.desired("aws", 500, emails=EMAILS)
    existing = dict(want)
    assert pb.diff(existing, want)["action"] == "unchanged"
    assert pb.diff(existing, want)["changes"] == {}


def test_the_diff_names_which_value_moved():
    """Field by field rather than equality: somebody about to edit their billing account
    deserves to see WHAT changes, not just that something does."""
    want = pb.desired("aws", 750, emails=EMAILS, threshold=90)
    existing = dict(want, limit=500.0, threshold_percent=80)
    d = pb.diff(existing, want)
    assert d["action"] == "update"
    assert d["changes"]["limit"] == {"from": 500.0, "to": 750.0}
    assert d["changes"]["threshold_percent"] == {"from": 80, "to": 90}
    assert "emails" not in d["changes"]


def test_a_removed_address_shows_up_as_a_change():
    """Otherwise an address deleted in Settings keeps receiving cloud alerts, and the
    page would say nothing needed doing."""
    want = pb.desired("aws", 500, emails="ops@example.com")
    existing = dict(want, emails=["ops@example.com", "gone@example.com"])
    d = pb.diff(existing, want)
    assert d["action"] == "update"
    assert d["changes"]["emails"]["to"] == ["ops@example.com"]


# ── The endpoint ──────────────────────────────────────────────────────────────

def test_the_push_is_an_action_not_a_side_effect_of_saving_settings():
    """Creating billing configuration because somebody typed into a form is the surprise
    this design avoids. The number lives in Settings; the push lives behind POST."""
    tree = ast.parse(_API_SRC)
    verbs = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                verbs[node.name] = dec.func.attr
    assert verbs.get("read_aws_budget") == "get"
    assert verbs.get("push_aws_budget") == "post"
    # No setup/config handler may reach the push.
    setup = open(os.path.join(_ROOT, "web_dashboard/api/setup.py"), encoding="utf-8").read()
    assert "put_budget" not in setup and "budgets_api" not in setup


def test_the_endpoints_are_admin_only():
    assert _API_SRC.count("Depends(require_admin)") == 2, (
        "both the read and the push must require admin — this reads and writes billing "
        "configuration")


def test_the_read_reports_instead_of_failing_when_nothing_is_configured():
    """"You have not set this up" is the answer to the question, not a fault — otherwise
    the page cannot say WHICH of the two settings is missing."""
    assert "configured\": False" in _API_SRC or '"configured": False' in _API_SRC
    body = _API_SRC[_API_SRC.index("async def read_aws_budget"):
                    _API_SRC.index("async def push_aws_budget")]
    assert "HTTPException" not in body, (
        "the read should answer 200 with a reason rather than raising")


def test_the_budgets_client_is_global_not_regional():
    """AWS Budgets lives at us-east-1 and is account-scoped. Passing the configured
    region works by accident on a us-east-1 deployment and fails everywhere else."""
    assert '_BUDGETS_REGION = "us-east-1"' in _AWS_SRC
    assert "_aws_kwargs(_BUDGETS_REGION)" in _AWS_SRC


def _run_tests():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_tests())
