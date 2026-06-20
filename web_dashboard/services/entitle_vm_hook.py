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


async def _resolve_vm_private_key(tag: str) -> str:
    """Resolve the SSH private key that pairs with the key cloud-init injected into
    the VM — sourced from the dashboard's existing per-cloud keypair secret, NOT a
    separate Entitle key. Azure is clean today; AWS/GCP private-key retrieval is not
    wired yet, so they fall back to the optional ``entitle_ssh_private_key_ref``
    override (see docs/design/entitle-resource-registration.md)."""
    t = (tag or "").lower()
    try:
        if t == "azure":
            from . import azure_service
            kv = _cfg("azure_key_vault_url")
            if not kv:
                return ""
            return await azure_service.resolve_azure_ssh_private_key(
                kv, _cfg("azure_ssh_keypair_secret_name"),
                _cfg("azure_ssh_private_key_secret_name"),
            )
        # AWS: private key lives at the ec2/keypairs/<name> convention but the key
        # name isn't tracked per-deploy yet. GCP: only a public-key resolver exists.
        # Both fall back to the optional override below until wired.
    except Exception as e:  # noqa: BLE001
        logger.warning("Entitle: VM private-key resolve (%s) failed: %s", t, e)
    return ""


async def register(db, job_id: str, vm_name: str, hostname: str, *,
                   private: bool, result: dict, tag: str = "cloud",
                   private_key: str = "", sudo_user: str = "") -> None:
    """Register a built VM as an Entitle SSH ephemeral-accounts integration.

    ``private`` attaches the shared Entitle agent (unreachable hosts); a public VM
    needs no agent. The SSH private key is the VM's own keypair (resolved per-cloud);
    callers may pass a resolved ``private_key``/``sudo_user`` explicitly. Writes
    ``entitle_integration_id`` + ``entitle_registration_tf_state`` onto ``result``
    (the latter is stored in job metadata for teardown). Non-fatal."""
    from . import entitle_registration_service as ent, job_service
    pk = (private_key
          or await _resolve_vm_private_key(tag)
          or resolve_ssh_private_key(_cfg("entitle_ssh_private_key_ref")))
    su = sudo_user or _cfg("entitle_ssh_sudo_user")
    try:
        r = await ent.register_ssh_host(
            name=vm_name,
            hostname=hostname,
            sudo_user=su,
            private_key=pk,
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
