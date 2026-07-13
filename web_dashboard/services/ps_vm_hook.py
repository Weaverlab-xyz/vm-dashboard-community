"""
Shared "register a freshly-built VM in BeyondTrust Password Safe" hook for the
AWS / Azure / GCP deploy paths — the Password Safe counterpart to entitle_vm_hook.

Each cloud's deploy background task, after provisioning the VM (and its Entitle
registration), calls :func:`register` to optionally onboard the host as a Password
Safe **managed system** with its baked-in ``adminuser`` account, and
:func:`deregister` on teardown. Both are **non-fatal** — failures are recorded on the
job ``result`` dict but never fail the deploy/destroy. Gating on the per-build opt-in +
the global capability flag is the caller's job (carried on job metadata).

Onboarding methods (see ps_resource_service):
  - **AWS** defaults to ``ssm`` — the cloud-native "AWS Systems Manager" Password Safe
    custom plugin (managed over SSM SendCommand; managed system DNS = {instance-id}:{region};
    no SSH key pushed). Configurable via ``passwordsafe_aws_registration_method``.
  - **Azure** defaults to ``azurevm`` — the cloud-native "Azure VM SSH Rotation" Password
    Safe custom plugin (managed over Azure VM Run Command; managed system address =
    tenantId/subscriptionId/resourceGroup/vmName; no SSH key pushed). Configurable via
    ``passwordsafe_azure_registration_method``.
  - **GCP** defaults to ``gcpvm`` — the cloud-native "GCP VM SSH Rotation" Password Safe
    custom plugin (managed by writing the public key into the GCE instance's ``ssh-keys``
    metadata; managed system address = projectId/zone/instanceName; no SSH key pushed;
    requires OS Login disabled on the instance). Configurable via
    ``passwordsafe_gcp_registration_method``.
  - Every other cloud (and AWS/Azure/GCP when set to ``ssh``) uses the traditional SSH
    flow: a managed system keyed by host_name/ip with the VM's own private key pushed.

Either way the operator configures a functional account per cloud
(``passwordsafe_vm_functional_account_{aws,azure,gcp}``); its platform decides the
management method, and the dashboard resolves the account's id + platform_id and
onboards against it.
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


def _registration_method(tag: str) -> str:
    """Onboarding method for this cloud. AWS defaults to the cloud-native AWS Systems
    Manager custom plugin (``ssm``); Azure defaults to the cloud-native Azure VM SSH
    Rotation custom plugin (``azurevm``); GCP defaults to the cloud-native GCP VM SSH
    Rotation custom plugin (``gcpvm``); every other cloud uses the traditional SSH flow."""
    t = (tag or "").lower()
    if t == "aws":
        return (_cfg("passwordsafe_aws_registration_method") or "ssm").lower()
    if t == "azure":
        return (_cfg("passwordsafe_azure_registration_method") or "azurevm").lower()
    if t == "gcp":
        return (_cfg("passwordsafe_gcp_registration_method") or "gcpvm").lower()
    return "ssh"


async def register(db, job_id: str, vm_name: str, hostname: str, *,
                   result: dict, tag: str = "cloud",
                   private_key: str = "", ssh_key_secret: str = "",
                   instance_id: str = "", region: str = "",
                   resource_group: str = "", project: str = "", zone: str = "") -> None:
    """Onboard a built VM into Password Safe as a managed system + managed account.

    Method is per-cloud (``_registration_method``):

    * **ssm** (AWS default) — the AWS Systems Manager custom plugin. The managed system's
      DNS name is ``{instance_id}:{region}`` (so ``instance_id`` + ``region`` are required)
      and the account name is ``{managed_account_name};{suffix}``. No SSH key is pushed —
      Password Safe mints it over SSM. Optionally triggers an initial Change Password.
    * **azurevm** (Azure default) — the Azure VM SSH Rotation custom plugin. The managed
      system's address is ``tenantId/subscriptionId/resourceGroup/vmName`` (tenant +
      subscription from Azure config, ``resource_group`` + ``vm_name`` from the deploy).
      No SSH key is pushed — Password Safe writes the key over Azure VM Run Command.
      Triggers an initial Change Password by default (``adminuser`` has no baked-in key).
    * **gcpvm** (GCP default) — the GCP VM SSH Rotation custom plugin. The managed system's
      address is ``projectId/zone/instanceName`` (``project`` + ``zone`` + ``vm_name`` from
      the deploy). No SSH key is pushed — Password Safe writes the public key into the GCE
      instance's ``ssh-keys`` metadata. Triggers an initial Change Password by default
      (``adminuser`` has no baked-in key).
    * **ssh** — the traditional flow. The SSH private key is the VM's own keypair (resolved
      the same way the Entitle SSH registration does: ``ssh_key_secret`` = the per-launch
      override when set, else the configured default).

    The per-cloud functional account is resolved to its id + platform via the Password
    Safe REST API (its platform binds the managed system — for ssm this is the custom
    plugin). Writes ``ps_managed_system_id`` / ``ps_managed_account_id`` /
    ``ps_registration_tf_state`` onto ``result``. Non-fatal."""
    from . import entitle_vm_hook, ps_api_service, ps_resource_service, job_service, config_service
    try:
        method = _registration_method(tag)

        fa_name = _functional_account_name(tag)
        if not fa_name:
            raise ps_resource_service.PSResourceError(
                f"no Password Safe functional account configured for {tag!r} "
                f"(set passwordsafe_vm_functional_account_{(tag or '').lower()})")
        fa = await ps_api_service.get_functional_account(fa_name)
        workgroup_id = await ps_api_service.get_workgroup_id(_cfg("passwordsafe_workgroup"))
        managed_account_name = _cfg("passwordsafe_managed_account_name") or "adminuser"
        entity_type_id = int(_cfg("passwordsafe_entity_type_id") or "1")

        if method == "ssm":
            if not (instance_id and region):
                raise ps_resource_service.PSResourceError(
                    "AWS Systems Manager onboarding needs the instance id + region "
                    f"(got instance_id={instance_id!r}, region={region!r})")
            # The managed system inherits the functional account's platform, so a non-SSM
            # functional account would silently create the system on the wrong platform.
            pname = fa.get("platform_name") or ""
            if pname and "systems manager" not in pname.lower():
                raise ps_resource_service.PSResourceError(
                    f"functional account {fa_name!r} is on platform {pname!r}, not an "
                    "'AWS Systems Manager' platform — the managed system would land on the "
                    "wrong platform. Point passwordsafe_vm_functional_account_aws at your "
                    "AWS Systems Manager Custom Plugin functional account.")
            r = await ps_resource_service.register_managed_system(
                name=vm_name,
                host_name=vm_name,
                functional_account_id=fa["id"],
                platform_id=fa["platform_id"],
                workgroup_id=workgroup_id,
                entity_type_id=entity_type_id,
                managed_account_name=managed_account_name,
                method="ssm",
                dns_name=f"{instance_id}:{region}",
                account_suffix=_cfg("passwordsafe_ssm_account_suffix") or "local",
            )
        elif method == "azurevm":
            tenant_id = _cfg("azure_tenant_id")
            subscription_id = _cfg("azure_subscription_id")
            missing = [n for n, v in (("azure_tenant_id", tenant_id),
                                      ("azure_subscription_id", subscription_id),
                                      ("resource_group", resource_group),
                                      ("vm_name", vm_name)) if not v]
            if missing:
                raise ps_resource_service.PSResourceError(
                    "Azure VM SSH Rotation onboarding needs " + ", ".join(missing)
                    + " (address is tenantId/subscriptionId/resourceGroup/vmName)")
            # The managed system inherits the functional account's platform, so a non-plugin
            # functional account would silently create the system on the wrong platform.
            pname = fa.get("platform_name") or ""
            if pname and "azure vm ssh rotation" not in pname.lower():
                raise ps_resource_service.PSResourceError(
                    f"functional account {fa_name!r} is on platform {pname!r}, not an "
                    "'Azure VM SSH Rotation' platform — the managed system would land on the "
                    "wrong platform. Point passwordsafe_vm_functional_account_azure at your "
                    "Azure VM SSH Rotation Custom Plugin functional account.")
            r = await ps_resource_service.register_managed_system(
                name=vm_name,
                host_name=vm_name,
                functional_account_id=fa["id"],
                platform_id=fa["platform_id"],
                workgroup_id=workgroup_id,
                entity_type_id=entity_type_id,
                managed_account_name=managed_account_name,
                method="azurevm",
                dns_name=f"{tenant_id}/{subscription_id}/{resource_group}/{vm_name}",
            )
        elif method == "gcpvm":
            missing = [n for n, v in (("project", project),
                                      ("zone", zone),
                                      ("vm_name", vm_name)) if not v]
            if missing:
                raise ps_resource_service.PSResourceError(
                    "GCP VM SSH Rotation onboarding needs " + ", ".join(missing)
                    + " (address is projectId/zone/instanceName)")
            # The managed system inherits the functional account's platform, so a non-plugin
            # functional account would silently create the system on the wrong platform.
            pname = fa.get("platform_name") or ""
            if pname and "gcp vm ssh rotation" not in pname.lower():
                raise ps_resource_service.PSResourceError(
                    f"functional account {fa_name!r} is on platform {pname!r}, not a "
                    "'GCP VM SSH Rotation' platform — the managed system would land on the "
                    "wrong platform. Point passwordsafe_vm_functional_account_gcp at your "
                    "GCP VM SSH Rotation Custom Plugin functional account.")
            r = await ps_resource_service.register_managed_system(
                name=vm_name,
                host_name=vm_name,
                functional_account_id=fa["id"],
                platform_id=fa["platform_id"],
                workgroup_id=workgroup_id,
                entity_type_id=entity_type_id,
                managed_account_name=managed_account_name,
                method="gcpvm",
                dns_name=f"{project}/{zone}/{vm_name}",
            )
        else:
            pk = private_key or await entitle_vm_hook._resolve_vm_private_key(tag, ssh_key_secret)
            if not pk:
                raise ps_resource_service.PSResourceError(
                    "no SSH private key resolved for the VM keypair — Password Safe manages "
                    "the account by key; the chosen secret must carry a private key")
            r = await ps_resource_service.register_managed_system(
                name=vm_name,
                host_name=vm_name,
                ip_address=hostname,
                private_key=pk,
                functional_account_id=fa["id"],
                platform_id=fa["platform_id"],
                workgroup_id=workgroup_id,
                entity_type_id=entity_type_id,
                managed_account_name=managed_account_name,
                ssh_key_enforcement_mode=int(_cfg("passwordsafe_ssh_key_enforcement_mode") or "2"),
                application_host_id=int(_cfg("passwordsafe_application_host_id") or "0"),
            )

        result["ps_managed_system_id"] = r.get("managed_system_id")
        result["ps_managed_account_id"] = r.get("managed_account_id")
        result["ps_registration_tf_state"] = r.get("tf_state_json")

        # Optional, best-effort: trigger an initial Change Password so Password Safe mints
        # the first SSH key immediately (over SSM SendCommand / Azure Run Command / GCE
        # metadata).
        #   • ssm — off by default (auto-management rotates on schedule regardless).
        #   • azurevm / gcpvm — ON by default: the baked-in adminuser has no key, so without
        #     an initial mint the account is unusable until the first scheduled rotation.
        # Never fails the deploy.
        mint = (
            (method == "ssm"
             and config_service.get_bool("passwordsafe_ssm_change_password_on_register", False))
            or (method == "azurevm"
                and config_service.get_bool("passwordsafe_azure_change_password_on_register", True))
            or (method == "gcpvm"
                and config_service.get_bool("passwordsafe_gcp_change_password_on_register", True))
        )
        if mint and r.get("managed_account_id"):
            try:
                await ps_api_service.change_managed_account_password(int(r["managed_account_id"]))
                result["ps_change_password_triggered"] = True
            except Exception as ce:  # noqa: BLE001
                result["ps_change_password_error"] = str(ce)
                logger.warning("Password Safe initial Change Password failed for %s: %s", vm_name, ce)

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
