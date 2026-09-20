"""The cell inherits teardown, and must keep inheriting it.

A network cell is an ordinary ``gce_deploy`` row. That is the whole reason Destroy and
the auto-delete timer work on it without a line of cell-specific code: the VM, the Shell
Jump and the Password Safe registration are reaped by the paths that reap every other
VM. Nothing in this feature owns a teardown.

That property is cheap to lose. The moment someone adds a cell-specific wiring artifact
-- a firewall rule, a jump item, a tunnel -- it will be created by the cell and removed
by nobody, because ``gcp_vm_service._run_destroy`` has never heard of it. The result is
a leaked cloud resource whose only trace is a key on a destroyed job row.

So this file asserts the *absence* of an obligation rather than the presence of a
cleanup branch, which is the opposite of tests/test_ot_destroy_cleanup.py and is correct
for the opposite design. If it ever starts failing, the fix is not to delete it: it is
to add the matching removal branch to ``_run_destroy`` and rewrite this file into the
both-directions scan the OT cell needs.

Runs under pytest, or standalone:
    python tests/test_netcell_destroy_cleanup.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-netcell-destroy")

_API = os.path.join(_ROOT, "web_dashboard", "api", "netcell.py")
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "netcell_service.py")
_GCP_VM = os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# Metadata keys the cell legitimately writes that are pure description -- they name no
# cloud resource, so nothing has to remove them. Anything else beginning `netcell` would
# be a new artifact and a new obligation.
_DESCRIPTIVE_KEYS = {"netcell", "netcell_params"}


def _job_metadata_keys():
    """The literal keys of create_job(metadata={...}).

    AST-parsed rather than regexed over the file: a bare `"netcell[a-z_]*"` sweep also
    matches the audit-log action name, which is not a metadata key and names nothing.
    """
    import ast
    tree = ast.parse(_read(_API))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "create_job"):
            continue
        for kw in node.keywords:
            if kw.arg == "metadata" and isinstance(kw.value, ast.Dict):
                return {k.value for k in kw.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("api/netcell.py has no create_job(metadata={...}) call")


def test_the_cell_writes_no_metadata_key_that_names_a_resource():
    keys = {k for k in _job_metadata_keys() if k.startswith("netcell")}
    unexpected = keys - _DESCRIPTIVE_KEYS
    assert not unexpected, (
        f"api/netcell.py writes {sorted(unexpected)}. If any of those names a cloud "
        "resource, it is created by the cell and removed by nobody — gcp_vm_service."
        "_run_destroy has never heard of it. Add the removal branch there, then rewrite "
        "this test into the both-directions scan tests/test_ot_destroy_cleanup.py runs.")


def test_the_cell_provisions_no_jump_item_of_its_own():
    """The Shell Jump comes from the deploy path. A jump item created here would
    outlive the VM."""
    src = _read(_API) + _read(_SVC)
    for forbidden in ("provision_jump", "provision_api_tunnel", "terraform_pra_service",
                      "web_jump", "tunnel_jump"):
        assert forbidden not in src, (
            f"the cell touches {forbidden!r}. A jump item it provisions is one the "
            "ordinary destroy will not remove")


def test_the_cell_runs_no_terraform_of_its_own():
    src = _read(_API) + _read(_SVC)
    for forbidden in ("tf_state", "terraform_service", "run_terraform"):
        assert forbidden not in src, \
            f"the cell manages terraform state ({forbidden!r}) that nothing tears down"


def test_the_destroy_path_needs_no_netcell_branch():
    """Stated as an assertion so the claim is checked rather than believed. If this
    fails, the two files have grown a dependency and both this test and the destroy
    path need updating together."""
    assert "netcell" not in _read(_GCP_VM), (
        "gcp_vm_service now mentions netcell. That is fine — but it means the cell has "
        "teardown of its own, so this file's premise no longer holds and it must be "
        "rewritten as a both-directions scan")


def test_the_cell_row_is_an_ordinary_deploy_row():
    """The single fact everything above rests on."""
    src = _read(_API)
    assert 'job_type="gce_deploy"' in src, (
        "the cell is no longer an ordinary gce_deploy row, so it is no longer reaped by "
        "the paths that reap every other VM")


def test_the_service_says_teardown_is_inherited():
    """The reasoning has to survive in the file, not only in this test. Without it the
    next reader adds a wiring step and has no idea what it costs."""
    doc = _read(_SVC)
    assert "_run_destroy" in doc and "expiry reaper" in doc, \
        "netcell_service no longer explains that its teardown is inherited"


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
