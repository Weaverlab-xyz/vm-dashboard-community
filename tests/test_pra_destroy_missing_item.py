"""Unit tests for PRA teardown when the appliance no longer has the item.

Regression guard for the wedge that failed clouddb_decommission forever:

    Error: Error deleting item with ID [135]
    Could not delete item, unexpected error: status: 404, body: {"message":"Not Found"}

The sra provider errors on a DELETE that 404s instead of treating the item as
already gone, so a jump item / Vault account removed out of band (PRA console, or
a half-finished earlier teardown) made every retry of `terraform destroy` fail on
the same missing item — the database row stayed `failed` and reported an orphaned
tunnel that did not exist. `_destroy_sync` now drops the items the error names out
of state (matched by the id state records) and destroys what genuinely remains,
while anything that is NOT an already-gone item still fails loudly.

Imports terraform_pra_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_pra_destroy_missing_item.py
"""
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import terraform_pra_service as pra  # noqa: E402

# Terraform's own diagnostic layout for the failure, both items, box characters and
# all — including the fact that the destroy of a provider-only config carries NO
# "with <address>" block, which is why the id is what gets matched.
DESTROY_404 = (
    "╷\n"
    "│ Error: Error deleting item with ID [135]\n"
    "│ \n"
    "│ Could not delete item, unexpected error: status: 404, "
    'body: {"message":"Not Found"}\n'
    "╵\n"
    "╷\n"
    "│ Error: Error deleting item with ID [150]\n"
    "│ \n"
    "│ Could not delete item, unexpected error: status: 404, "
    'body: {"message":"Not Found"}\n'
    "╵\n"
)

STATE = {
    "version": 4,
    "resources": [
        {"mode": "managed", "type": "sra_postgresql_tunnel_jump", "name": "db_tunnel",
         "instances": [{"attributes": {"id": "135", "name": "clouddb-abc123"}}]},
        {"mode": "managed", "type": "sra_vault_username_password_account",
         "name": "db_admin",
         "instances": [{"attributes": {"id": "150", "name": "clouddb-abc123-admin"}}]},
        # A data source is never a destroy target and must never be state-rm'd.
        {"mode": "data", "type": "sra_jump_group_list", "name": "jg",
         "instances": [{"attributes": {"id": "135"}}]},
    ],
}


def _proc(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["terraform"], returncode=rc,
                                       stdout=stdout, stderr=stderr)


class _FakeTF:
    """Stand-in for _run_tf: records calls and replays queued destroy results."""

    def __init__(self, destroy_results):
        self.destroy_results = list(destroy_results)
        self.calls = []
        self.state_rm = []

    def __call__(self, args, work_dir, timeout=120, extra_env=None, tenant=None):
        self.calls.append(list(args))
        if args[:2] == ["state", "rm"]:
            self.state_rm.append(args[2])
            return _proc(0, stdout=f"Removed {args[2]}")
        if args[0] == "destroy":
            return self.destroy_results.pop(0)
        return _proc(0)


def _run(destroy_results, state=STATE):
    """Drive _destroy_sync over a temp work dir holding ``state``."""
    fake = _FakeTF(destroy_results)
    orig = pra._run_tf
    pra._run_tf = fake
    try:
        with tempfile.TemporaryDirectory(prefix="pra_destroy_test_") as work_dir:
            if state is not None:
                Path(work_dir, "terraform.tfstate").write_text(json.dumps(state))
            error = None
            try:
                pra._destroy_sync(work_dir)
            except Exception as e:  # noqa: BLE001
                error = e
            return fake, error
    finally:
        pra._run_tf = orig


def test_already_deleted_ids_reads_both_items():
    assert pra._already_deleted_ids(DESTROY_404) == {"135", "150"}


def test_already_deleted_ids_ignores_a_real_failure():
    # A 403 on one item and a 404 on another: only the 404 is "already gone". Read
    # per diagnostic block, so the neighbouring 404 is not credited to the 403.
    mixed = (
        "│ Error: Error deleting item with ID [135]\n"
        '│ Could not delete item, unexpected error: status: 403, body: {"message":"Forbidden"}\n'
        "│ Error: Error deleting item with ID [150]\n"
        '│ Could not delete item, unexpected error: status: 404, body: {"message":"Not Found"}\n'
    )
    assert pra._already_deleted_ids(mixed) == {"150"}


def test_already_deleted_ids_empty_for_unrelated_errors():
    assert pra._already_deleted_ids("Error: Invalid provider configuration") == set()


def test_destroy_prunes_missing_items_and_succeeds():
    fake, error = _run([_proc(1, stderr=DESTROY_404), _proc(0, stdout="Destroy complete!")])
    assert error is None, f"already-gone items should not fail teardown: {error}"
    assert sorted(fake.state_rm) == [
        "sra_postgresql_tunnel_jump.db_tunnel",
        "sra_vault_username_password_account.db_admin",
    ], fake.state_rm
    # Retried after pruning, and both destroys skip the refresh the provider 404s on.
    assert [c for c in fake.calls if c[0] == "destroy"] == [
        ["destroy", "-auto-approve", "-refresh=false"],
        ["destroy", "-auto-approve", "-refresh=false"],
    ]


def test_real_destroy_failure_still_raises():
    boom = "Error: Error deleting item with ID [135]\nstatus: 403, body: forbidden"
    fake, error = _run([_proc(1, stderr=boom)])
    assert isinstance(error, pra.TerraformPRAError)
    assert "403" in str(error)
    assert fake.state_rm == []           # nothing pruned on a real failure
    assert len([c for c in fake.calls if c[0] == "destroy"]) == 1   # and no retry


def test_404_for_an_id_not_in_state_raises():
    # Nothing to prune means nothing was proven gone — surface it rather than
    # silently reporting a clean teardown.
    other = {"version": 4, "resources": [
        {"mode": "managed", "type": "sra_shell_jump", "name": "vm",
         "instances": [{"attributes": {"id": "999"}}]}]}
    fake, error = _run([_proc(1, stderr=DESTROY_404)], state=other)
    assert isinstance(error, pra.TerraformPRAError)
    assert fake.state_rm == []


def test_unreadable_state_raises_the_destroy_error():
    fake, error = _run([_proc(1, stderr=DESTROY_404)], state=None)
    assert isinstance(error, pra.TerraformPRAError)
    assert "404" in str(error)


def test_destroy_stops_after_bounded_pruning_rounds():
    # A destroy that keeps failing must surface, never spin.
    fake, error = _run([_proc(1, stderr=DESTROY_404)] * 5)
    assert isinstance(error, pra.TerraformPRAError)
    assert len([c for c in fake.calls if c[0] == "destroy"]) == 3


def test_teardown_entry_points_route_through_the_tolerant_destroy():
    # Every state-driven teardown in this module must use _destroy_sync; a raw
    # `destroy` call that raises on its return code is how the wedge came back.
    src = Path(_ROOT, "web_dashboard", "services", "terraform_pra_service.py").read_text(
        encoding="utf-8")
    for fn in ("_remove_sync", "_destroy_state_only_sync", "_remove_db_tunnel_sync"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "_destroy_sync(" in body, f"{fn} does not use the tolerant destroy"
        assert "terraform destroy failed" not in body, f"{fn} still raises on its own"


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
