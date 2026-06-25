"""
Shared "register a freshly-built VM in BeyondTrust Password Safe" hook for the
AWS / Azure / GCP deploy paths — the Password Safe counterpart to entitle_vm_hook.

Each cloud's deploy background task, after provisioning the VM (and its Entitle
registration), calls :func:`register` to optionally onboard the host as a Password
Safe **managed system** with its baked-in ``adminuser`` account (SSH-key managed),
and :func:`deregister` on teardown. Both are **non-fatal** — failures are recorded
on the job ``result`` dict but never fail the deploy/destroy. Gating on the per-build
opt-in + the global capability flag is the caller's job (carried on job metadata).

Management method is config-driven: the operator configures a functional account per
cloud (``passwordsafe_vm_functional_account_{aws,azure,gcp}``). Its platform decides
whether management is agent-plugin- or Resource-Broker-based; the dashboard just
resolves the account's id + platform_id and onboards against it.
"""
import logging

logger = logging.getLogger(__name__)


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, "") or ""


def registration_enabled() -> bool:
    """Global capability flag — registration is also per-build opt-in (caller-checked)."""
    from . import config_service
    return config_service.get_bool("passwordsafe_registration_enabled", False)


def _functional_account_name(tag: str) -> str:
    """The operator-configured functional account for this cloud (per-cloud key,
    then the generic fallback)."""
    t = (tag or "").lower()
    return _cfg(f"passwordsafe_vm_functional_account_{t}") or _cfg("passwordsafe_vm_functional_account")


async def register(db, job_id: str, vm_name: str, hostname: str, *,
                   result: dict, tag: str = "cloud",
                   private_key: str = "", ssh_key_secret: str = "") -> None:
    """Onboard a built VM into Password Safe as a managed system + managed account.

    The SSH private key is the VM's own keypair (the key cloud-init injected for the
    bt-ready ``adminuser``), resolved the same way the Entitle SSH registration does
    (``ssh_key_secret`` = the per-launch override when set, else the configured
    default). The per-cloud functional account is resolved to its id + platform via
    the Password Safe REST API. Writes ``ps_managed_system_id`` /
    ``ps_registration_tf_state`` onto ``result``. Non-fatal."""
    from . import entitle_vm_hook, ps_api_service, ps_resource_service, job_service
    try:
        pk = private_key or await entitle_vm_hook._resolve_vm_private_key(tag, ssh_key_secret)
        if not pk:
            raise ps_resource_service.PSResourceError(
                "no SSH private key resolved for the VM keypair — Password Safe manages "
                "the account by key; the chosen secret must carry a private key")

        fa_name = _functional_account_name(tag)
        if not fa_name:
            raise ps_resource_service.PSResourceError(
                f"no Password Safe functional account configured for {tag!r} "
                f"(set passwordsafe_vm_functional_account_{(tag or '').lower()})")
        fa = await ps_api_service.get_functional_account(fa_name)
        workgroup_id = await ps_api_service.get_workgroup_id(_cfg("passwordsafe_workgroup"))

        r = await ps_resource_service.register_managed_system(
            name=vm_name,
            host_name=vm_name,
            ip_address=hostname,
            private_key=pk,
            functional_account_id=fa["id"],
            platform_id=fa["platform_id"],
            workgroup_id=workgroup_id,
            entity_type_id=int(_cfg("passwordsafe_entity_type_id") or "1"),
            managed_account_name=_cfg("passwordsafe_managed_account_name") or "adminuser",
            ssh_key_enforcement_mode=int(_cfg("passwordsafe_ssh_key_enforcement_mode") or "2"),
            application_host_id=int(_cfg("passwordsafe_application_host_id") or "0"),
        )
        result["ps_managed_system_id"] = r.get("managed_system_id")
        result["ps_managed_account_id"] = r.get("managed_account_id")
        result["ps_registration_tf_state"] = r.get("tf_state_json")
        job_service.update_progress(
            db, job_id, 96, f"Onboarded into Password Safe (system {r.get('managed_system_id')}).")
    except Exception as e:  # noqa: BLE001 — registration must never fail the deploy
        result["ps_error"] = str(e)
        logger.warning("Password Safe registration failed for %s: %s", vm_name, e)


async def deregister(meta: dict, result: dict) -> None:
    """Off-board the managed system + account a deploy registered (if any). Non-fatal."""
    state = (meta or {}).get("ps_registration_tf_state")
    if not state:
        return
    from . import ps_resource_service
    try:
        await ps_resource_service.deregister(state)
        result["ps_registration_removed"] = (meta or {}).get("ps_managed_system_id")
    except Exception as e:  # noqa: BLE001
        logger.warning("Password Safe deregister failed: %s", e)
        result["ps_error"] = str(e)
