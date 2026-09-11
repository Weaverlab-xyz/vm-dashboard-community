"""Pre-flight refusals shared by every path that enqueues a credentialed Ansible run.

The Config-Management endpoint grew these checks; the SPIRE lab needs the same ones, and
most of them are **messages** — an operator-facing sentence naming the Settings item to
change. A second copy of such a sentence is how two pages end up telling one operator two
different things about one checkbox.

**Returns a refusal rather than raising ``HTTPException``.** Only three modules under
``services/`` raise HTTP, and a plain value is what lets each caller choose its own failure
shape: the API layers raise, and it keeps the four sentences unit-testable without FastAPI
or a database.

**Pure and stdlib-only, with no sibling imports**, which is what makes this testable by file
path the way ``managed_accounts`` / ``cloud_ansible_secrets`` / ``ansible_run_meta`` are.
Every fact it needs is therefore passed in:

  * ``needs_ephemeral_store`` is ``managed_accounts.requires_ephemeral_store``'s answer,
    computed by the caller. This module may not import it and stay pure — and the call has
    to remain visible in ``api/config_mgmt`` anyway, because
    ``tests/test_database_registration`` pins its position relative to the k8s/database
    dispatch (a gate that preceded that dispatch would 400 every cloud database run).
  * config is read through an injected ``cfg`` callable, the shape ``cloud_ansible_secrets``
    already uses. Pass the SAME resolver the run path uses
    (``ansible_local_service._cfg``): a gate reading different config from the dispatcher
    can green-light a run that is then routed somewhere the credential cannot go.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Refusal:
    """A refusal an API layer raises verbatim.

    ``detail`` is authored here and operator-facing, so returning it is safe — no third
    party's exception text is ever interpolated into it (CodeQL ``py/stack-trace-exposure``
    is why that distinction matters elsewhere in this codebase).
    """
    status: int
    detail: str


def effective_runner(cloud: str, *, cfg) -> str:
    """The Ansible runner backend that will actually handle a run for this target cloud —
    the per-cloud override (``ansible_runner_<cloud>``) falling back to the global
    ``ansible_runner``, defaulting to local.

    ``cloud`` outside the three known clouds ignores the override key entirely, so a
    ``target_kind`` that is not a cloud VM cannot pick up a cloud runner by accident.
    """
    runner = cfg("ansible_runner") or "local"
    if cloud in ("aws", "azure", "gcp"):
        runner = cfg(f"ansible_runner_{cloud}") or runner
    return runner


def check_permission(*, wants_secret: bool, can_use_secrets: bool,
                     has_managed: bool, password_safe_enabled: bool) -> Refusal | None:
    """May this caller use a credential in a run at all, and is the feature it needs on?

    Split from :func:`check_runner_capability` rather than bundled, because
    ``api/config_mgmt`` has a THIRD check between the two — ``_validate_cloud_secret_stores``
    for store residency — and the order it reports refusals in is observable: ``/run-bulk``
    surfaces "the first target's error", so bundling these would change which reason a
    batch reports. Callers with no store-residency step (the SPIRE lab, which sends no
    named-var or become secret) want :func:`check_credentials` instead.
    """
    if wants_secret and not can_use_secrets:
        return Refusal(
            403,
            "Using a Secrets-Management secret in a run requires the 'secrets:use' permission.")
    if has_managed and not password_safe_enabled:
        return Refusal(
            400,
            "Managed-account checkout requires BeyondTrust Password Safe to be enabled in Settings.")
    return None


def check_runner_capability(*, needs_ephemeral_store: bool = False,
                            ephemeral_enabled: bool = False,
                            runner: str = "",
                            gcp_runner_service_account: str = "") -> Refusal | None:
    """Can the runner this run will dispatch to actually carry the chosen credential?

    Managed-account checkout works on the local and ACI runners (both inject the
    credential inline). ECS / Cloud Run reference a store secret, so a just-in-time
    credential needs an ephemeral, RBAC-locked store copy — an explicit opt-in, because it
    briefly copies a PAM-vaulted credential into the cloud store for the run.
    """
    if needs_ephemeral_store:
        if not ephemeral_enabled:
            return Refusal(
                400,
                ("Managed-account checkout on the ECS / Cloud Run runners requires "
                 "'Ephemeral cloud secrets' to be enabled in Settings (it briefly copies "
                 "the credential into the cloud store, RBAC-locked). Otherwise use the "
                 "local or Azure (ACI) runner."))
        if runner == "gcp" and not gcp_runner_service_account:
            return Refusal(
                400,
                ("GCP ephemeral secrets require 'gcp_ansible_runner_service_account' to be "
                 "set — the Cloud Run job runs as that SA and read access to the ephemeral "
                 "secret is locked to it."))
    return None


def check_credentials(*, wants_secret: bool, can_use_secrets: bool,
                      has_managed: bool, password_safe_enabled: bool,
                      needs_ephemeral_store: bool = False,
                      ephemeral_enabled: bool = False,
                      runner: str = "",
                      gcp_runner_service_account: str = "") -> Refusal | None:
    """The first refusal that applies, or ``None`` when the run may proceed.

    The order is the Config-Management endpoint's own and is load-bearing: permission,
    then feature enablement, then runner capability. A caller who has no permission AND no
    Password Safe gets the 403 — telling them to enable a feature they may not use would
    send them to a Settings page that cannot help.
    """
    return check_permission(
        wants_secret=wants_secret, can_use_secrets=can_use_secrets,
        has_managed=has_managed, password_safe_enabled=password_safe_enabled,
    ) or check_runner_capability(
        needs_ephemeral_store=needs_ephemeral_store,
        ephemeral_enabled=ephemeral_enabled, runner=runner,
        gcp_runner_service_account=gcp_runner_service_account)
