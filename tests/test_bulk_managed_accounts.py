"""Unit tests: the per-target managed-account WIRE CONTRACT for a bulk run.

A bulk Config-Management run used to apply ONE Password Safe managed account to every
selected object. Two things made that happen, and both are pinned here:

  * There was nowhere to express a different account per target, so a fleet whose
    hosts carry differently-named accounts failed N-1 jobs.
  * ``BulkRunRequest.managed_account`` accepts a PINNED ref (``system_id`` +
    ``account_id``), and the fan-out copied it to every target verbatim.
    ``resolve_managed_ref`` short-circuits on a pinned ref, so every job checked out
    ONE machine's credential and connected everywhere with it — for any caller that
    was not the browser tab which knew to strip the ids.

The fix is a per-target map keyed by inventory id, plus a refusal for keys that name
anything outside the resolved selection. These assertions cover the model shape, the
selection rule (``managed_accounts.pick_ref``) and back-compatibility with a payload
that sends only the old single field.

The end-to-end fan-out itself needs a DB and a live Password Safe, so what is pinned
here is the contract the fan-out is written against, plus a source-level check that it
actually calls ``pick_ref`` rather than re-copying one ref.

Runs under pytest, or standalone:  python tests/test_bulk_managed_accounts.py
"""
import ast
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MA_PATH = os.path.join(_ROOT, "web_dashboard", "services", "managed_accounts.py")
_spec = importlib.util.spec_from_file_location("managed_accounts", _MA_PATH)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)

# Imported UNGUARDED: pydantic and fastapi are present in this environment, and an
# ImportError here is a real regression rather than an optional dependency. A bare
# `except Exception: skip` is what turns a file into a permanent green no-op.
from web_dashboard.api.config_mgmt import (  # noqa: E402
    BulkManagedAccountsRequest, BulkRunRequest, ManagedAccountRef, RunRequest)

_CFG_PATH = os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py")


# ── ManagedAccountRef: both forms, and the third case the map introduces ────────

def test_a_pinned_ref_validates():
    ref = ManagedAccountRef(system_id=4, account_id=9, account_name="root")
    assert (ref.system_id, ref.account_id) == (4, 9)


def test_a_name_only_ref_validates():
    assert ManagedAccountRef(account_name="svc-ansible").account_id is None


def test_a_ref_carrying_both_ids_and_a_name_validates():
    """What the per-target picker sends: the ids drive the checkout directly, and the
    name travels alongside so a SCHEDULED batch can drop the ids and re-resolve."""
    ref = ManagedAccountRef(system_id=1, account_id=2, account_name="svc", uses_ssh_key=True)
    assert ref.account_name == "svc" and ref.uses_ssh_key is True


def test_an_empty_ref_is_refused():
    """Neither pinned nor named is unresolvable — catch it at the edge, not at
    dispatch time when a job already exists."""
    try:
        ManagedAccountRef()
    except Exception as e:                                   # noqa: BLE001
        assert "account_name" in str(e) or "system_id" in str(e)
    else:
        raise AssertionError("an empty ManagedAccountRef should not validate")


def test_a_half_pinned_ref_is_refused():
    """One id alone cannot identify an account — the pair is what names it."""
    for kwargs in ({"system_id": 1}, {"account_id": 2}):
        try:
            ManagedAccountRef(**kwargs)
        except Exception:                                    # noqa: BLE001
            pass
        else:
            raise AssertionError(f"{kwargs} should not validate")


# ── BulkRunRequest: the per-target map ──────────────────────────────────────────

def _bulk(**kw):
    return BulkRunRequest(asset="site.yml", inventory_ids=["job:1", "job:2"], **kw)


def test_the_map_accepts_a_pinned_ref_per_target():
    """Pinned ids are legitimate HERE — the key names the target whose own live list
    they came from, and the server checks the key against its resolved plan."""
    req = _bulk(managed_accounts={
        "job:1": {"system_id": 1, "account_id": 10, "account_name": "svc-web01"},
        "job:2": {"system_id": 2, "account_id": 20, "account_name": "svc-db02"},
    })
    assert req.managed_accounts["job:1"].system_id == 1
    assert req.managed_accounts["job:2"].account_name == "svc-db02"


def test_the_map_accepts_an_explicit_null_for_one_target():
    req = _bulk(managed_accounts={"job:1": None})
    assert "job:1" in req.managed_accounts and req.managed_accounts["job:1"] is None


def test_both_maps_exist_and_default_empty():
    req = _bulk()
    assert req.managed_accounts == {} and req.managed_becomes == {}


def test_the_default_field_still_exists_alongside_the_map():
    req = _bulk(managed_account={"account_name": "svc-fleet"},
                managed_accounts={"job:1": {"system_id": 1, "account_id": 10,
                                            "account_name": "svc-web01"}})
    assert req.managed_account.account_name == "svc-fleet"
    assert req.managed_accounts["job:1"].account_id == 10


# ── back-compatibility ──────────────────────────────────────────────────────────

def test_a_payload_with_only_the_old_field_gives_every_target_the_same_ref():
    """The pre-existing caller. With both maps empty, pick_ref must return the batch
    default for EVERY target — byte-identical to the old behaviour."""
    req = _bulk(managed_account={"account_name": "svc-fleet"})
    for tid in ("job:1", "job:2"):
        assert ma.pick_ref(req.managed_accounts, tid, req.managed_account) \
            is req.managed_account


def test_a_payload_with_neither_gives_every_target_none():
    req = _bulk()
    for tid in ("job:1", "job:2"):
        assert ma.pick_ref(req.managed_accounts, tid, req.managed_account) is None


# ── the selection rule, against real model objects ──────────────────────────────

def test_pick_ref_over_a_parsed_payload():
    req = _bulk(
        managed_account={"account_name": "svc-fleet"},
        managed_accounts={
            "job:1": {"system_id": 1, "account_id": 10, "account_name": "svc-web01"},
            "job:2": None,
        })
    # Chosen for this target.
    assert ma.pick_ref(req.managed_accounts, "job:1", req.managed_account).account_name \
        == "svc-web01"
    # Explicitly NONE for this target — not the fleet default.
    assert ma.pick_ref(req.managed_accounts, "job:2", req.managed_account) is None
    # Not mentioned at all — the fleet default.
    assert ma.pick_ref(req.managed_accounts, "job:3", req.managed_account).account_name \
        == "svc-fleet"


def test_stray_keys_are_detectable_from_a_parsed_payload():
    req = _bulk(managed_accounts={"job:1": None, "job:99": None},
                managed_becomes={"job:zz": None})
    stray = ma.stray_ids(set(req.managed_accounts) | set(req.managed_becomes),
                         ["job:1", "job:2"])
    assert stray == ["job:99", "job:zz"]


# ── a per-target ref must survive into the RunRequest the fan-out builds ────────

def test_a_per_target_ref_is_a_valid_run_request_managed_account():
    """The fan-out hands each picked ref straight to RunRequest, so the two models
    have to accept the same object."""
    req = _bulk(managed_accounts={"job:1": {"system_id": 1, "account_id": 10,
                                            "account_name": "svc-web01"}})
    run = RunRequest(asset="site.yml", target="10.0.0.5",
                     managed_account=req.managed_accounts["job:1"])
    assert run.managed_account.account_id == 10


# ── the picker request ──────────────────────────────────────────────────────────

def test_bulk_managed_accounts_request_shape():
    r = BulkManagedAccountsRequest(inventory_ids=["job:1"],
                                   default_account_name="svc-fleet")
    assert r.inventory_ids == ["job:1"]
    assert r.default_account_name == "svc-fleet" and r.default_become_name == ""


def test_bulk_managed_accounts_request_defaults_are_empty():
    r = BulkManagedAccountsRequest()
    assert r.inventory_ids == [] and r.default_account_name == ""


# ── source-level: the fan-out must SELECT, not copy ─────────────────────────────

def _bulk_fn_source() -> str:
    with open(_CFG_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "run_playbook_bulk":
            with open(_CFG_PATH, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("run_playbook_bulk not found in api/config_mgmt.py")


def test_the_fan_out_picks_per_target_rather_than_reusing_one_ref():
    """The regression this whole feature is about. If the loop ever goes back to
    `managed_account=payload.managed_account`, every job in the batch checks out one
    machine's credential again — and nothing else in this file would notice, because
    the models would still validate."""
    src = _bulk_fn_source()
    assert "pick_ref" in src, "the fan-out no longer selects a per-target ref"
    assert "managed_account=payload.managed_account" not in src, \
        "the fan-out copies one ref to every target again"
    assert "managed_become=payload.managed_become" not in src


def test_the_fan_out_refuses_stray_per_target_keys():
    """A key naming a resource outside the run must 400, not be ignored — an ignored
    override silently puts that host back on the fleet account."""
    src = _bulk_fn_source()
    assert "stray_ids" in src and "400" in src


def test_the_connection_field_guard_sees_the_maps():
    """k8s/database batches must stay refused when ONLY the map is populated."""
    src = _bulk_fn_source()
    assert "payload.managed_account or payload.managed_accounts" in src
    assert "payload.managed_become or payload.managed_becomes" in src


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
