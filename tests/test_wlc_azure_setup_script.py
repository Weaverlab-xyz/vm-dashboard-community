"""Structural guards for scripts/wlc/setup-azure-apps.sh.

The script talks to a real Entra tenant, so nothing here executes it. What it pins is the
handful of decisions that are wrong-but-plausible — each one produces a setup that looks
finished and then fails somewhere else:

  * granting the target app nothing, because the grants do not arrive on their own
  * handing Workload Credentials a client ID where it wants an Object ID
  * accumulating client secrets on an app registration that caps them
  * printing a client secret to a terminal

Runs under pytest, or standalone:
    python tests/test_wlc_azure_setup_script.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "wlc", "setup-azure-apps.sh")


def _src():
    with open(_SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def _code():
    """The script with comment lines removed, so prose cannot satisfy an assertion."""
    return "\n".join(l for l in _src().splitlines()
                     if not l.lstrip().startswith("#"))


# ── Shape ────────────────────────────────────────────────────────────────────

def test_the_script_exists_and_is_bash():
    assert os.path.isfile(_SCRIPT)
    assert _src().startswith("#!/usr/bin/env bash")


def test_it_fails_fast_and_on_unset_variables():
    # A half-run of this leaves an app registration with no grants, which is the state
    # that looks finished and is not.
    assert "set -Eeuo pipefail" in _code()


def test_it_reuses_the_repos_own_helpers():
    # retry() in particular: Entra replication means a just-created SP 404s for a few
    # seconds, and every lookup here has to tolerate that.
    code = _code()
    assert "lib/common.sh" in code
    assert "retry " in code


# ── The grants ───────────────────────────────────────────────────────────────

def test_the_rbac_is_copied_from_a_reference_principal():
    """The reason the script is worth having.

    setup-azure.sh grants its OWN service principal, so a separate target app starts
    with nothing. Enumerating the grants here instead would be a second definition of
    that set and would drift from it.
    """
    code = _code()
    assert "--reference-sp" in code
    assert "az role assignment list" in code
    assert "az role assignment create" in code


def test_the_copy_keys_on_role_definition_id_not_name():
    # This deployment has at least one custom role ("Dashboard Image Promoter" for an
    # external image gallery), and a custom role's name is not guaranteed to resolve.
    code = _code()
    assert "roleDefinitionId" in code


def test_skipping_the_grants_warns_loudly():
    # --no-rbac leaves the target app unable to do anything. Silence there would produce
    # a 403 on every call with no clue why.
    src = _src()
    # Anchored on the branch itself: "COPY_RBAC ))" also appears in the preflight guard
    # near the top, and splitting on that lands the window in the wrong place.
    block = src.split("(( ! COPY_RBAC ))", 1)[1][:600]
    assert "warn" in block
    assert "403" in block


def test_a_failed_assignment_explains_the_privilege_it_needs():
    # Creating an assignment needs Microsoft.Authorization/roleAssignments/write, which
    # Contributor does NOT include — so this failure is expected for many operators and
    # the message has to say what to ask for.
    src = _src()
    assert "roleAssignments/write" in src
    assert "User Access Administrator" in src


# ── The Object ID / client ID distinction ────────────────────────────────────

def test_the_target_is_identified_by_object_id():
    """The most common mistake in Azure setup, per the provider's own docs.

    Workload Credentials wants the app's Object ID; passing the Application (client) ID
    fails without naming the field.
    """
    code = _code()
    # The terraform variable that consumes it is named for the object id, and the script
    # must feed it the value it queried with `--query id`, not `--query appId`.
    assert "azure_target_application_object_id='$TARGET_OBJECT_ID'" in code
    assert "--query id -o tsv" in code


def test_both_ids_are_resolved_separately():
    # `.appId` and `.id` are different values on the same object; conflating them is the
    # failure above.
    code = _code()
    assert "--query appId -o tsv" in code


# ── The client secret ────────────────────────────────────────────────────────

def test_the_secret_is_written_to_a_restricted_file_not_stdout():
    code = _code()
    assert "chmod 600" in code
    # The value itself must never be echoed; only the path to it.
    assert not re.search(r'echo .*\.password', code)


def test_the_secret_is_reset_rather_than_appended():
    # `credential add` on a re-run accumulates secrets, and an app registration caps how
    # many it holds — eventually you cannot mint at all.
    code = _code()
    assert "az ad app credential reset" in code
    assert "az ad app credential add" not in code


def test_it_is_idempotent_about_the_secret():
    # A re-run must not invalidate a secret the dashboard is already using.
    code = _code()
    assert "Reusing the integration client secret" in _src()
    assert 'jq -e ' in code


# ── Ownership ────────────────────────────────────────────────────────────────

def test_ownership_is_preferred_over_a_graph_application_role():
    """Ownership needs no tenant-wide permission; Application.ReadWrite.All would grant
    it over every app in the directory."""
    src = _src()
    assert "az ad app owner add" in _code()
    assert "Application.ReadWrite.All" in src  # named in the rationale, not requested
    assert "appRoleAssignments" not in _code()


def test_ownership_is_checked_before_being_added():
    code = _code()
    assert "az ad app owner list" in code


# ── The Key Vault mode distinction ───────────────────────────────────────────

def test_key_vault_handling_distinguishes_rbac_from_access_policy():
    """Contributor grants neither, and the two vault modes need different calls.

    An RBAC vault's permissions came with the role copy; a policy vault needs
    set-policy. Getting this backwards produces a vault the target cannot read.
    """
    code = _code()
    assert "enableRbacAuthorization" in code
    assert "az keyvault set-policy" in code


# ── The handoff ──────────────────────────────────────────────────────────────

def test_it_prints_every_value_the_module_requires():
    src = _src()
    for var in ("azure_tenant_id", "azure_integration_client_id",
                "azure_integration_sp_object_id", "azure_target_application_object_id"):
        assert var in src, f"the script never surfaces {var}"


def test_the_printed_variables_are_real_module_variables():
    # A name that does not exist in the module sends the operator in circles.
    with open(os.path.join(_ROOT, "terraform", "workload_credentials", "variables.tf"),
              encoding="utf-8") as fh:
        variables = fh.read()
    src = _src()
    for var in ("azure_tenant_id", "azure_integration_client_id",
                "azure_integration_sp_object_id", "azure_target_application_object_id",
                "azure_integration_client_secret"):
        assert f'variable "{var}"' in variables, f"{var} is not a module variable"
        assert var in src


def test_it_reminds_the_operator_about_the_subscription():
    # The lease carries no subscription, so a minted credential with none configured is
    # refused. Easy to miss because nothing else about Azure setup mentions it.
    assert "azure_subscription_id" in _src()


def test_nothing_claims_the_grants_arrive_on_their_own():
    """A correction, pinned so it cannot regress.

    azure.tf used to say pointing setup-azure.sh at the target app brought the grants
    with it. It does not — that script grants its own principal. The claim was written
    in TWO places and an earlier version of this test only checked one of them, so it
    sweeps the whole module directory now.
    """
    module = os.path.join(_ROOT, "terraform", "workload_credentials")
    checked = 0
    for name in sorted(os.listdir(module)):
        if not name.endswith((".tf", ".md")):
            continue
        with open(os.path.join(module, name), encoding="utf-8") as fh:
            text = fh.read()
        assert "Point that setup at the target app and the grants" not in text, (
            f"{name} still says the grants arrive on their own")
        checked += 1
    assert checked >= 5, f"only swept {checked} files — did the module move?"

    with open(os.path.join(module, "azure.tf"), encoding="utf-8") as fh:
        assert "setup-azure-apps.sh" in fh.read()


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
