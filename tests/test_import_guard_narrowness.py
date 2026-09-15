"""Gate: a test file may not swallow a BROKEN import as if it were a MISSING one.

Most files in this suite guard their module-level imports so they can run in a bare
interpreter — the app deps (fastapi, boto3, the cloud SDKs) are genuinely optional here,
and skipping without them is correct. The trap is the width of the guard, and Python
draws the line for us exactly where we need it:

    an absent PACKAGE      ->  ModuleNotFoundError   (environmental; skipping is right)
    a missing SYMBOL       ->  plain ImportError      (the module under test is BROKEN)

A stub whose shape has gone stale raises the second kind. ``cannot import name
'notify_policy' from 'web_dashboard.services'`` and ``cannot import name 'CertLab' from
'web_dashboard.database'`` are both plain ImportError, NOT ModuleNotFoundError. So
``except Exception`` — and ``except ImportError``, which is broader than it looks — cannot
tell "this machine lacks boto3" from "this test no longer works", and treats both as a
skip. CI runs each file standalone (.github/workflows/tests.yml), so the file prints one
``SKIP:`` line and exits **0**: indistinguishable from a pass, forever.

That is not hypothetical. FIVE files were silent no-ops when this gate was written —
test_setup_feature_roundtrip.py (30 Settings-panel parity assertions, dormant since
e6ebc347) and test_expiry_api.py / test_expiry_reaper.py (50 assertions covering the
auto-delete sweep, including the at-most-once and per-pass-cap properties that bound how
much a runaway reaper can destroy), and test_inventory_bulk_run.py /
test_inventory_service.py. The last of those is the sharpest lesson available: its stub
list carried a comment asking the next person to keep it in sync, and was then missed
twice anyway. A convention a human must remember is not a guard.

THE RULE: if a module-level ``try`` imports a first-party module (``web_dashboard*`` /
``runners*``) and its handler skips or exits, that handler must catch
``ModuleNotFoundError`` and nothing wider.

The migration is mechanical — probe the optional third-party deps by name, then import
the first-party module unguarded:

    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError as exc:
        ...skip...
    from web_dashboard.api.setup import _read_feature  # noqa: E402

A file that stubs every dependency itself needs no guard at all; see
tests/test_expiry_reaper.py.

Note this rule makes the OTHER half of the bug class self-reporting. Sixteen files stub a
package with ``__path__ = []``, which looks like a package but resolves no submodule —
harmless until the module under test imports a sibling. That is not a defect on its own,
so it is deliberately not gated; once the import above it is unguarded, a stale
``__path__`` fails loudly by itself, which is all we wanted.

Pure stdlib ast, in the style of tests/test_open_encoding.py and
tests/test_no_redefined_names.py. Runs under pytest, or standalone:
    python tests/test_import_guard_narrowness.py
"""
import ast
import glob
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FIRST_PARTY = ("web_dashboard", "runners")

# Only ModuleNotFoundError is narrow enough (see the docstring). ImportError is listed as
# too-broad ON PURPOSE: it is the superclass that a stale stub actually raises, so it is
# the most tempting wrong answer here.
_TOO_BROAD = {"Exception", "BaseException", "ImportError", "OSError",
              "AttributeError", "TypeError", "ValueError", "<bare>"}


def _rel(path):
    """Repo-relative, forward-slashed. Windows returns backslashes from relpath, so a
    comparison against the slash-keyed allowlist below would never match locally and
    always match in CI — see tests/test_path_separator_portability.py."""
    return os.path.relpath(path, _ROOT).replace(os.sep, "/")


# ── the backlog ──────────────────────────────────────────────────────────────
#
# 105 files already use the broad form. They are NOT known to be broken — this is a
# shape, not a failure — but each one is capable of going silently dead the way the
# five above did. Listed individually rather than by prefix or glob so a NEW file
# cannot hide among them.
#
# This list may only shrink. test_every_legacy_entry_is_still_real below fails on a
# stale entry, so migrating a file forces deleting its line here and the backlog can't
# rot into a permanent exemption.
_LEGACY = {
    "tests/test_admission_service.py",
    "tests/test_agent_ansible_bundle.py",
    "tests/test_agent_ansible_remote_fetch.py",
    "tests/test_agent_api.py",
    "tests/test_agent_audience.py",
    "tests/test_agent_guard.py",
    "tests/test_agent_ps_credential.py",
    "tests/test_agent_secret_api.py",
    "tests/test_ansible_cloud_run_service.py",
    "tests/test_ansible_deploy_key_match.py",
    "tests/test_audit_readable.py",
    "tests/test_aws_cache_scope.py",
    "tests/test_aws_region.py",
    "tests/test_azure_bulk_projection.py",
    "tests/test_azure_cache_scope.py",
    "tests/test_azure_destroy_region.py",
    "tests/test_azure_jumpoint_modes.py",
    "tests/test_azure_region.py",
    "tests/test_bulk_power_fanout.py",
    "tests/test_cache_warmer_parity.py",
    "tests/test_cert_lab_functional_account.py",
    "tests/test_cloud_db_tf_vars.py",
    "tests/test_cloud_function_region_picker.py",
    "tests/test_cloud_power.py",
    "tests/test_cloud_run_job_listing.py",
    "tests/test_cloud_run_job_reaper.py",
    "tests/test_clouddb_ansible_conn_vars.py",
    "tests/test_clouddb_azure_jump_region.py",
    "tests/test_clouddb_capacity_error.py",
    "tests/test_clouddb_db_name_concepts.py",
    "tests/test_clouddb_gcp_grant.py",
    "tests/test_clouddb_gcp_iam_db_user.py",
    "tests/test_clouddb_gcp_sqlserver_fa_login.py",
    "tests/test_clouddb_progress_and_reclaim.py",
    "tests/test_clouddb_provision_job_meta.py",
    "tests/test_clouddb_ps_functional_account.py",
    "tests/test_clouddb_ps_registration.py",
    "tests/test_clouddb_region_picker.py",
    "tests/test_clouddb_row_db_name.py",
    "tests/test_compute_region_config.py",
    "tests/test_config_mgmt_agent_resolution.py",
    "tests/test_config_mgmt_routes.py",
    "tests/test_cost_cache.py",
    "tests/test_cost_service.py",
    "tests/test_dashboard_collect.py",
    "tests/test_dashboard_stat_cache.py",
    "tests/test_dashboard_stats_api.py",
    "tests/test_database_registration.py",
    "tests/test_destroy_guardrails.py",
    "tests/test_entitle_agent_token_lifecycle.py",
    "tests/test_entitle_agent_token_recovery.py",
    "tests/test_entitle_db_proxy_placement.py",
    "tests/test_entitle_rancher.py",
    "tests/test_entitle_request_access_link.py",
    "tests/test_fido2_state_store.py",
    "tests/test_gateway_reconcile.py",
    "tests/test_gateway_region_picker.py",
    "tests/test_gcp_cache_scope.py",
    "tests/test_gcp_jumpoint_modes.py",
    "tests/test_gcp_region.py",
    "tests/test_gcp_subnetwork_qualify.py",
    "tests/test_gcp_vm_nat.py",
    "tests/test_hypervisor_connections.py",
    "tests/test_hypervisor_sync.py",
    "tests/test_inventory_hypervisor.py",
    "tests/test_inventory_route_queries.py",
    "tests/test_job_retry.py",
    "tests/test_k8s_capacity_message.py",
    "tests/test_k8s_provision_job_meta.py",
    "tests/test_k8s_provision_rollback.py",
    "tests/test_k8s_tf_vars.py",
    "tests/test_login_guard.py",
    "tests/test_mcp_rbac.py",
    "tests/test_node_aws.py",
    "tests/test_node_azure.py",
    "tests/test_node_cloud_picker.py",
    "tests/test_oci_cache_scope.py",
    "tests/test_oci_deploy_placement.py",
    "tests/test_oci_gateway.py",
    "tests/test_oke_shape_options.py",
    "tests/test_oke_version_options.py",
    "tests/test_pov_accessor.py",
    "tests/test_pov_summary.py",
    "tests/test_power_resync.py",
    "tests/test_pra_web_jump.py",
    "tests/test_preflight.py",
    "tests/test_provider_budget.py",
    "tests/test_proxy_trust.py",
    "tests/test_rancher_api_runner.py",
    "tests/test_rancher_firewall_merge.py",
    "tests/test_rancher_service.py",
    "tests/test_runner_kubeconfig.py",
    "tests/test_spire_lab_service.py",
    "tests/test_sweeper_loop_parity.py",
    "tests/test_terraform_cancel_lock_release.py",
    "tests/test_terraform_destroy_refresh_wedge.py",
    "tests/test_terraform_operator_force_unlock.py",
    "tests/test_unmanaged_vms.py",
    "tests/test_vdesktop_seats.py",
    "tests/test_vm_spend_cap.py",
    "tests/test_vm_suspend_schedule.py",
    "tests/test_vms_agent_merge.py",
    "tests/test_vms_dashboard_stats.py",
    "tests/test_workgroup_delete_guard.py",
    "tests/test_workload_k8s_service.py",
}

_MIGRATION_HINT = (
    "\n\nTo fix: probe the optional third-party deps by name under "
    "`except ModuleNotFoundError`, then import the first-party module UNGUARDED so a "
    "broken stub raises instead of skipping. See tests/test_setup_feature_roundtrip.py "
    "for the guarded form and tests/test_expiry_reaper.py for the no-guard form."
)


# ── ast helpers ──────────────────────────────────────────────────────────────

def _module_level_trys(tree):
    """Every ``try`` reachable without entering a def/class.

    A try INSIDE a function is ordinary control flow and not this bug — the failure mode
    here is specifically a collection-time guard that decides whether the file runs at
    all. Recurses through module-level if/for/while/with and into nested trys, because a
    guard is sometimes nested one deep inside a platform check."""
    found = []

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Try):
                found.append(node)
                walk(node.body)
                walk(node.orelse)
                walk(node.finalbody)
                for handler in node.handlers:
                    walk(handler.body)
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With)):
                walk(node.body)
                walk(getattr(node, "orelse", []) or [])

    walk(tree.body)
    return found


def _first_party_imports(node):
    """First-party module names imported anywhere inside ``node``."""
    mods = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and sub.module:
            mods.append(sub.module)
        elif isinstance(sub, ast.Import):
            mods += [alias.name for alias in sub.names]
    return [m for m in mods
            if any(m == p or m.startswith(p + ".") for p in _FIRST_PARTY)]


def _caught_names(handler):
    """The exception names a handler catches; ``<bare>`` for a bare except."""
    node = handler.type
    if node is None:
        return ["<bare>"]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple):
        return [e.id if isinstance(e, ast.Name) else ast.unparse(e) for e in node.elts]
    return [ast.unparse(node)]


def _skips_or_exits(handler):
    """Does this handler abandon the file rather than fail it?"""
    dumped = ast.dump(ast.Module(body=handler.body, type_ignores=[]))
    return "skip" in dumped or "sys.exit" in dumped or "'exit'" in dumped


def _violations():
    """{rel_path: sorted too-broad exception names} for the whole tests/ tree."""
    found = {}
    scanned = 0
    for path in sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py"))):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        scanned += 1
        for try_node in _module_level_trys(tree):
            if not _first_party_imports(try_node):
                continue
            for handler in try_node.handlers:
                if not _skips_or_exits(handler):
                    continue
                broad = [n for n in _caught_names(handler) if n in _TOO_BROAD]
                if broad:
                    found.setdefault(_rel(path), set()).update(broad)
    return {k: sorted(v) for k, v in found.items()}, scanned


# ── the gate ─────────────────────────────────────────────────────────────────

def test_no_new_file_swallows_a_broken_first_party_import():
    """The gate proper: no file outside the backlog may catch wider than
    ModuleNotFoundError around a first-party import it then skips on."""
    found, scanned = _violations()
    assert scanned > 300, f"only scanned {scanned} test files — did the glob break?"
    new = {k: v for k, v in found.items() if k not in _LEGACY}
    assert not new, (
        "these files skip on a BROKEN first-party import, so they exit 0 having tested "
        "nothing: " + "; ".join(f"{k} (catches {'/'.join(v)})" for k, v in sorted(new.items()))
        + _MIGRATION_HINT)


def test_every_legacy_entry_is_still_real():
    """A stale exemption is how a backlog becomes permanent. Migrating a file — or
    deleting it — must force removing its line from _LEGACY, so the list can only shrink
    and always reflects real remaining work."""
    found, _ = _violations()
    stale = sorted(_LEGACY - set(found))
    assert not stale, (
        "_LEGACY lists files that no longer have a broad import guard (or no longer "
        f"exist). Delete these lines — the backlog is finished with them: {stale}")


def test_the_backlog_only_shrinks():
    """A ceiling, so the list can't be grown as a way of silencing the gate."""
    assert len(_LEGACY) <= 105, (
        f"_LEGACY has grown to {len(_LEGACY)}. It is a backlog, not an allowlist: a new "
        "file must use the narrow form instead of being added here." + _MIGRATION_HINT)


def test_the_reference_implementations_are_not_exempt():
    """The five files this gate was written for must stay migrated. They are what the
    failure message points people at, so a regression in them would teach the wrong
    pattern to everyone who trips this gate."""
    found, _ = _violations()
    for name in ("tests/test_setup_feature_roundtrip.py",
                 "tests/test_expiry_api.py",
                 "tests/test_expiry_reaper.py",
                 "tests/test_inventory_bulk_run.py",
                 "tests/test_inventory_service.py"):
        assert name not in _LEGACY, f"{name} is the reference form; it must not be exempt"
        assert name not in found, (
            f"{name} regressed to a broad import guard — it is cited in this gate's own "
            "failure message as the example to copy." + _MIGRATION_HINT)


def test_modulenotfounderror_is_the_only_narrow_catch():
    """Pins the language fact the whole rule rests on, so nobody 'simplifies' the rule to
    allow ImportError: a missing PACKAGE raises ModuleNotFoundError, but a missing SYMBOL
    (the stale-stub case) raises plain ImportError, which ModuleNotFoundError does not
    catch. Asserted at runtime rather than trusted."""
    import types
    pkg = types.ModuleType("_gate_probe_pkg")
    pkg.__path__ = []
    sys.modules["_gate_probe_pkg"] = pkg
    try:
        try:
            from _gate_probe_pkg import absent_submodule  # noqa: F401
            raise AssertionError("expected the stub import to fail")
        except ModuleNotFoundError:
            raise AssertionError(
                "a missing symbol raised ModuleNotFoundError — the rule's premise is "
                "wrong on this Python and the gate needs rethinking")
        except ImportError:
            pass  # correct: a broken stub is a plain ImportError
    finally:
        del sys.modules["_gate_probe_pkg"]

    try:
        import _gate_definitely_not_installed  # noqa: F401
        raise AssertionError("expected the absent package import to fail")
    except ModuleNotFoundError:
        pass  # correct: an absent package IS a ModuleNotFoundError

    assert issubclass(ModuleNotFoundError, ImportError), \
        "ModuleNotFoundError must stay a SUBCLASS of ImportError, or `except " \
        "ImportError` would not be the wider catch this gate rejects it for being"


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
