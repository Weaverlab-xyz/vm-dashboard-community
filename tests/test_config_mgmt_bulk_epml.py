"""Regression: a bulk Config-Management run must carry every field it shares with a
single run — ``epml_token_var`` in particular.

Two silent breaks put this here, and either one alone is enough to lose the field:

1. ``BulkRunRequest`` did not DECLARE ``epml_token_var``. Neither model sets
   ``model_config``, so pydantic's default ``extra="ignore"`` dropped the key the
   Config-Management page posts (templates/config-mgmt/index.html) at validation —
   no error, no warning, the attribute simply absent.
2. The per-target ``RunRequest`` in ``run_playbook_bulk`` is a deliberate
   field-by-field copy, so a field that IS declared still goes nowhere unless it is
   named there too. The comment above ``run_at`` in that call records the first time
   this happened; ``epml_token_var`` was the second.

The operator-visible symptom is the worst kind: every job in the batch runs GREEN with
the EPM-L token variable never defined, so activation no-ops or the play dies on an
undefined var — once per target, across a whole fleet, while the same setting works
perfectly on a single run.

``test_every_shared_field_survives_the_fan_out`` is the general guard, so the third
field does not have to be found in production the way the first two were.

Skips ONLY if the app deps (fastapi/pydantic/sqlalchemy) aren't installed — any other
import failure is a real regression and must fail loudly, because a drift check that
can vanish quietly is worth nothing. Runs under pytest, or standalone:
    python tests/test_config_mgmt_bulk_epml.py
"""
import ast
import asyncio
import inspect
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "bulkepml.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# The ONLY legitimate reason to skip is a bare interpreter with no app deps, so probe
# for those by name and let every other ImportError propagate as a failure.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.api import config_mgmt as cm  # noqa: E402
from web_dashboard.services import inventory_service as inv  # noqa: E402

TOKEN_VAR = "epml_installation_token"


# ── driving the route without a database ─────────────────────────────────────

def _targets(*names):
    """A plan_bulk_run result. ``spec`` is what inventory_service resolves per row and
    is splatted into the per-target RunRequest, so it stays minimal and VM-shaped."""
    return [{"id": f"job:{i}", "name": n,
             "spec": {"target": f"10.0.0.{i}", "cloud": "aws"}}
            for i, n in enumerate(names, start=1)]


def _drive(payload, targets=None):
    """Run ``run_playbook_bulk`` and return the per-target RunRequests it built.

    Everything the route reaches for is replaced on the live module: it does its
    ``from ..services import inventory_service`` INSIDE the function body, and looks
    ``run_playbook`` up as a module global, so both resolve at call time. Nothing is
    torn down — CI runs each test file in its own process."""
    inv.accessible_workgroups = lambda user: set()
    inv.collect = lambda db: []
    inv.visible_to = lambda item, accessible, username: True
    inv.plan_bulk_run = lambda items, ids: {
        "kind": "vm", "targets": targets if targets is not None else _targets("web-01")}
    # The trailing ansible_bulk_run audit is the one thing that would touch a real db.
    cm.job_service.log_audit = lambda *a, **k: None

    captured = []

    async def _capture(req, db, current_user):
        captured.append(req)
        return {"job_id": f"j{len(captured)}"}

    cm.run_playbook = _capture
    result = asyncio.run(cm.run_playbook_bulk(
        payload, None, types.SimpleNamespace(username="operator")))
    assert result["count"] == len(captured), "the route reported jobs it did not dispatch"
    return captured


def _full_payload(**over):
    """A BulkRunRequest with a DISTINCT non-default value in every shared field.

    This matters: if a field is left at its default here, a copy that drops it still
    compares equal on the other side and the guard below passes by coincidence. That
    is exactly how a lazier version of this test would have missed epml_token_var."""
    ref = cm.ManagedAccountRef
    base = dict(
        inventory_ids=["job:1"],
        asset="epml-activate.yml",
        asset_backend="s3",
        extra_vars={"epml_site": "lab"},
        secret_vars={"api_key": "azure_kv://primary/api-key"},
        ansible_user="deploy",
        secret_become_source="azure_kv://primary/become",
        secret_ssh_key_source="azure_kv://primary/ssh",
        managed_account=ref(account_name="svc-connect"),
        managed_become=ref(account_name="svc-become"),
        run_at="2026-10-01T22:00",
        run_timezone="America/New_York",
        change_window_id="saturday-night",
        epml_token_var=TOKEN_VAR,
    )
    base.update(over)
    return cm.BulkRunRequest(**base)


# ── the field itself ─────────────────────────────────────────────────────────

def test_a_bulk_request_keeps_the_epml_token_var_it_was_sent():
    """Fails on ABSENCE, not on a wrong value — an undeclared field is discarded by
    pydantic before any of our code runs, so there is nothing to read back."""
    payload = cm.BulkRunRequest(inventory_ids=["job:1"], asset="epml-activate.yml",
                                epml_token_var=TOKEN_VAR)
    got = getattr(payload, "epml_token_var", "<not declared>")
    assert got == TOKEN_VAR, (
        f"BulkRunRequest dropped epml_token_var (got {got!r}) — the model must declare "
        f"it or pydantic's extra='ignore' discards what the page posts")


def test_the_epml_token_var_reaches_every_target_in_the_batch():
    """One target inheriting the setting is not enough: a half-activated fleet is
    harder to spot than one that plainly did nothing."""
    captured = _drive(_full_payload(inventory_ids=["job:1", "job:2", "job:3"]),
                      targets=_targets("web-01", "web-02", "db-01"))
    assert len(captured) == 3, f"expected 3 per-target runs, got {len(captured)}"
    for req in captured:
        assert req.epml_token_var == TOKEN_VAR, (
            f"target {req.target} was dispatched with epml_token_var="
            f"{req.epml_token_var!r} — the fan-out did not copy it")


# ── the general guard ────────────────────────────────────────────────────────

def test_every_shared_field_survives_the_fan_out():
    """The guard that makes a THIRD dropped field a test failure rather than a
    support ticket. Derived from the models, so a field added to both is covered the
    day it lands."""
    payload = _full_payload()
    captured = _drive(payload)[0]

    shared = set(cm.RunRequest.model_fields) & set(cm.BulkRunRequest.model_fields)
    # A derivation that silently finds nothing would pass forever; pin the floor.
    assert len(shared) >= 13, (
        f"only {len(shared)} shared fields found ({sorted(shared)}) — did one of the "
        f"models move or get renamed?")

    for field in sorted(shared):
        sent, arrived = getattr(payload, field), getattr(captured, field)
        assert arrived == sent, (
            f"{field} is declared on BOTH models but the field-by-field RunRequest "
            f"copy in run_playbook_bulk never carries it: sent {sent!r}, target got "
            f"{arrived!r}")


def test_the_fan_out_copy_is_still_field_by_field():
    """The guard above compares what the copy produced, so it only means something
    while the copy is still explicit. A ``**payload.model_dump()`` spread would make
    it pass unconditionally — and would quietly forward inventory_ids too."""
    tree = ast.parse(inspect.cleandoc(inspect.getsource(cm.run_playbook_bulk)))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "RunRequest"]
    assert len(calls) == 1, f"expected one RunRequest(...) construction, found {len(calls)}"
    starred = [ast.unparse(k.value) for k in calls[0].keywords if k.arg is None]
    # The resolved per-target spec is the one legitimate splat; anything else (a
    # payload.model_dump()) would carry fields without naming them.
    assert starred == ["target['spec']"], (
        f"run_playbook_bulk splats {starred} rather than only the resolved target "
        f"spec — test_every_shared_field_survives_the_fan_out no longer proves anything")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
