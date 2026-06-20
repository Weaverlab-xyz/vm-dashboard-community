"""
Shared "register a freshly-built VM in Entitle" hook for the AWS / Azure / GCP
deploy paths.

Each cloud's deploy background task, after provisioning the VM (and its PRA Shell
Jump), calls :func:`register` to optionally register the host as an Entitle SSH
ephemeral-accounts integration, and :func:`deregister` on teardown. Both are
**non-fatal** — a failure is recorded on the job ``result`` dict but never fails the
deploy/destroy. Gating on the per-build opt-in + the global capability flag is the
caller's job (they carry the choice on the job metadata).
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
    return config_service.get_bool("entitle_registration_enabled", False)


def resolve_ssh_private_key(ref: str) -> str:
    """Resolve the Entitle SSH private key from a config ref or an inline PEM."""
    if not ref:
        return ""
    if "BEGIN" in ref:           # operator pasted the PEM directly
        return ref
    from . import config_service
    key = ref[len("config://"):] if ref.startswith("config://") else ref
    return config_service.get(key) or ""


async def register(db, job_id: str, vm_name: str, hostname: str, *,
                   private: bool, result: dict, tag: str = "cloud") -> None:
    """Register a built VM as an Entitle SSH ephemeral-accounts integration.

    ``private`` attaches the shared Entitle agent (unreachable hosts); a public VM
    needs no agent. Writes ``entitle_integration_id`` + ``entitle_registration_tf_state``
    onto ``result`` (the latter is stored in job metadata for teardown). Non-fatal."""
    from . import entitle_registration_service as ent, job_service
    try:
        r = await ent.register_ssh_host(
            name=vm_name,
            hostname=hostname,
            sudo_user=_cfg("entitle_ssh_sudo_user"),
            private_key=resolve_ssh_private_key(_cfg("entitle_ssh_private_key_ref")),
            private=private,
            tag=tag,
        )
        result["entitle_integration_id"] = r.get("integration_id")
        result["entitle_registration_tf_state"] = r.get("tf_state_json")
        job_service.update_progress(
            db, job_id, 95, f"Registered in Entitle (integration {r.get('integration_id')}).")
    except Exception as e:  # noqa: BLE001 — registration must never fail the deploy
        result["entitle_error"] = str(e)
        logger.warning("Entitle registration failed for %s: %s", vm_name, e)


async def deregister(meta: dict, result: dict) -> None:
    """Remove the Entitle integration a deploy registered (if any). Non-fatal."""
    state = (meta or {}).get("entitle_registration_tf_state")
    if not state:
        return
    from . import entitle_registration_service as ent
    try:
        await ent.deregister(state)
        result["entitle_integration_removed"] = (meta or {}).get("entitle_integration_id")
    except Exception as e:  # noqa: BLE001
        logger.warning("Entitle deregister failed: %s", e)
        result["entitle_error"] = str(e)
