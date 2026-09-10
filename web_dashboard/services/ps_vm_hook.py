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


# Methods whose managed account's stored credential IS an SSH private key minted by a
# cloud-native plugin, and which can therefore feed a PRA Vault Private Key account.
# The traditional ``ssh`` method is excluded on purpose: there the key is one the
# dashboard pushed rather than one Password Safe minted, so syncing it into PRA would
# publish a key that already exists in a cloud secret store.
_VAULT_SYNC_METHODS = frozenset({"ssm", "azurevm", "gcpvm"})


def vault_sync_enabled() -> bool:
    """Global opt-in for mirroring the VM's Password Safe-managed SSH key into a PRA
    Vault Private Key account (off by default — it needs a hand-imported plugin)."""
    from . import config_service
    return config_service.get_bool("passwordsafe_vault_sync_enabled", False)


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


def _platform_name_ok(platform_name: str, *required_tokens: str) -> bool:
    """Sanity-check that the functional account's platform is the expected custom
    plugin, tolerant of admin renames.

    Matches on lowercased substring *tokens* (all must be present) rather than one
    rigid contiguous phrase, so renaming the platform in Password Safe — e.g.
    ``Azure VM SSH Rotation`` → ``Azure Waagent VM SSH Rotation`` — doesn't break
    onboarding. The tokens still discriminate the intended plugin from siblings
    (``Azure SSH Key Vault`` lacks "ssh rotation"; ``GCP VM SSH Rotation`` lacks
    "azure"). A blank name means the best-effort lookup failed, so we never block."""
    p = (platform_name or "").strip().lower()
    if not p:
        return True
    return all(tok.lower() in p for tok in required_tokens)


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
            if not _platform_name_ok(pname, "systems manager"):
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
            if not _platform_name_ok(pname, "azure", "ssh rotation"):
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
            if not _platform_name_ok(pname, "gcp", "ssh rotation"):
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

    # Opt-in: make the key Password Safe now rotates usable in PRA. Deliberately
    # OUTSIDE the try/except above — the sync has its own failure key, and folding it
    # in would let a PRA-side problem overwrite ps_error and report the onboarding
    # itself as failed when the managed system is fine.
    if result.get("ps_managed_account_id") and not result.get("ps_error"):
        await wire_pra_vault_key_sync(db, job_id, vm_name, result=result, tag=tag)


async def wire_pra_vault_key_sync(db, job_id: str, vm_name: str, *,
                                  result: dict, tag: str = "cloud") -> None:
    """Mirror the VM's Password Safe-managed SSH key into a **PRA Vault Private Key**
    account, so a rep can check the key out in PRA and PRA can inject it into the VM's
    Shell Jump.

    Three artifacts, each recorded on ``result`` the moment it exists so a repair pass
    retries only what is missing (the same shape as the k8s token mirror and the OT
    cell's checkout pair):

      1. ``ps_vault_tf_state`` — a PRA Vault SSH account named ``{vm}-{login}``,
         associated to the VM's Jump Group for injection, seeded with a throwaway key;
      2. ``ps_vault_mirror_tf_state`` — a managed system + account on the "PRA Vault
         Private Key" plugin, named exactly like the Vault account (the plugin resolves
         its PRA-side target by NAME);
      3. ``ps_vault_synced`` — the SyncedAccounts link making the mirror a *subscriber*
         of the VM's own managed account, then one Change on the parent so PRA holds a
         real key now instead of after the next scheduled rotation.

    Password Safe owns the propagation from then on — no key passes through the
    dashboard. Wholly non-fatal: failures land on ``result["ps_vault_error"]``.

    The Vault account is created with the configured PRA credential
    (``bt_client_secret``); a deploy's per-launch ``pra_credential_ref`` override
    applies to its Shell Jump only, and is not threaded here."""
    from . import config_service, job_service, managed_accounts, ot_service, \
        ps_api_service, ps_resource_service, terraform_pra_service as pra

    if not vault_sync_enabled():
        return
    method = _registration_method(tag)
    if method not in _VAULT_SYNC_METHODS:
        result["ps_vault_skipped"] = (
            f"method {method!r} is not a cloud-native SSH-key plugin — nothing to mirror")
        return
    # Every cloud's destroy path gates the whole Password Safe off-boarding on
    # ps_registration_tf_state, and _scrub_state drops that state fail-closed rather
    # than stash an unscrubbable secret. Without it the managed system already needs
    # manual cleanup — so wiring PRA objects now would only add two more that nothing
    # would ever remove. Contain the failure instead of widening it.
    if not result.get("ps_registration_tf_state"):
        result["ps_vault_skipped"] = (
            "the onboarding recorded no Terraform state, so the destroy path cannot "
            "reach this VM's Password Safe objects — not adding PRA objects that would "
            "outlive the VM")
        return

    # The OS login user, not the Password Safe account name: the AWS Systems Manager
    # plugin qualifies its account as ``{user};{suffix}`` and that suffix is a naming
    # detail, not part of the username PRA injects.
    login = managed_accounts.ssh_login_user(
        _cfg("passwordsafe_managed_account_name") or "adminuser") or "adminuser"
    vault_name = f"{vm_name}-{login}"
    platform_name = _cfg("passwordsafe_vault_sync_platform") or "PRA Vault Private Key"

    try:
        if not result.get("ps_vault_tf_state"):
            # The deploy's own Jump Group if its Shell Jump was provisioned (this runs
            # after that step), else the same fallback chain the Shell Jump itself
            # uses. Blank means PRA has nowhere to inject the key, so there is nothing
            # worth creating.
            jump_group = (result.get("bt_jump_group_name") or "").strip() \
                or ot_service.resolve_jump_targets(None, None, (tag or "").lower())[0]
            if not jump_group:
                result["ps_vault_skipped"] = (
                    "no PRA Jump Group resolved for this VM — set bt_jump_group_name "
                    "(or the per-cloud override) to associate the Vault account")
                return
            job_service.update_progress(
                db, job_id, 97,
                f"Creating the PRA Vault key account {vault_name} (checkout/injection)…")
            group_raw = (_cfg("bt_vault_account_group_id") or "").strip()
            res = await pra.provision_vault_ssh_account(
                name=vault_name, username=login, jump_group_name=jump_group,
                vault_account_group_id=int(group_raw) if group_raw.isdigit() else None)
            result["ps_vault_account_id"] = str(res.get("vault_account_id") or "")
            result["ps_vault_account_name"] = vault_name
            result["ps_vault_tf_state"] = res.get("tf_state_json") or ""

        if not result.get("ps_vault_mirror_tf_state"):
            job_service.update_progress(
                db, job_id, 97, f"Onboarding the {platform_name} mirror into Password Safe…")
            # No fallback to the OT / cloud-database pravault functional-account keys:
            # those functional accounts live on the "PRA Vault Username Password"
            # platform, so borrowing one would land this mirror on a plugin that writes
            # a password field and never a key — a registration that reports success and
            # syncs nothing into PRA.
            fa_name = _cfg("passwordsafe_vault_sync_functional_account")
            if not fa_name:
                raise ps_resource_service.PSResourceError(
                    f"no functional account configured for the {platform_name!r} plugin "
                    "— create one in Password Safe (username = the PRA OAuth client id, "
                    "password = its secret) and set "
                    "passwordsafe_vault_sync_functional_account")
            fa = await ps_api_service.get_functional_account(fa_name)
            pname = fa.get("platform_name") or ""
            # Tokens from the CONFIGURED platform name, not a fixed "pra vault": that
            # phrase is a substring of "PRA Vault Username Password" too, so it would
            # wave through a functional account on the OT / cloud-database plugin — the
            # one mistake this whole path has to refuse. Deriving the tokens from the
            # operator's own platform name keeps the check rename-tolerant.
            if not _platform_name_ok(pname, *platform_name.split()):
                raise ps_resource_service.PSResourceError(
                    f"functional account {fa_name!r} is on platform {pname!r}, not "
                    f"{platform_name!r} — the mirror would land on the wrong plugin and "
                    "never write a key into PRA")
            platform_id = await ps_api_service.get_platform_id(platform_name)
            workgroup_id = await ps_api_service.get_workgroup_id(_cfg("passwordsafe_workgroup"))
            pra_url = _cfg("bt_api_host")
            if not pra_url.lower().startswith("http"):
                pra_url = f"https://{pra_url}"
            # A per-VM label as host_name and the appliance URL as dns_name, NOT the URL
            # as both: Password Safe names a workgroup-created managed system after its
            # HostName and the provider attaches an account to its system BY NAME, so
            # every VM sharing the URL as host_name would pile its account onto whichever
            # "PRA Vault" system was created first (see ps_resource_service.register_
            # managed_system's pravault branch).
            label = f"{vm_name}-pravault-key"
            reg = await ps_resource_service.register_managed_system(
                name=label, host_name=label, dns_name=pra_url, ip_address="127.0.0.1",
                port=443, functional_account_id=fa["id"], platform_id=platform_id,
                workgroup_id=workgroup_id, managed_account_name=vault_name,
                method="pravault")
            result["ps_vault_mirror_tf_state"] = reg.get("tf_state_json") or ""
            result["ps_vault_mirror_system_id"] = str(reg.get("managed_system_id") or "")
            result["ps_vault_mirror_account_id"] = str(reg.get("managed_account_id") or "")

        if not result.get("ps_vault_synced"):
            sub = str(result.get("ps_vault_mirror_account_id") or "").strip()
            if not sub.isdigit():
                raise ps_resource_service.PSResourceError(
                    f"the Password Safe mirror system for {vault_name} exists but "
                    f"recorded no managed-account id — remove managed system "
                    f"{vm_name}-pravault-key in Password Safe and re-register")
            job_service.update_progress(
                db, job_id, 98, f"Syncing {vault_name} to the VM's {login} key…")
            # Parent FIRST, subscriber second. Both path segments are plain account ids,
            # so a swapped pair links happily and then syncs backwards — pushing the
            # Vault account's throwaway key onto the VM's real managed account.
            link = await ps_api_service.link_synced_account(
                parent_account_id=int(result["ps_managed_account_id"]),
                synced_account_id=int(sub),
                expect_subscriber_platform=platform_name)
            if not link.get("confirmed"):
                raise ps_resource_service.PSResourceError(
                    f"Password Safe accepted the sync of account {sub} to "
                    f"{result['ps_managed_account_id']} but the subscriber is not in the "
                    f"parent's synced list — rotations would not reach PRA")
            result["ps_vault_synced"] = True
            # Converge now rather than at the next scheduled rotation: the link is born
            # after the onboarding mint, so PRA still holds the throwaway key.
            #
            # Deliberately NOT the cloud's change-on-register flag. That one answers
            # "rotate the credential when we first onboard it?"; this answers "a
            # subscriber appeared after the mint, so push one change through it". They
            # only look alike on Azure/GCP, where that flag defaults on. On AWS
            # (passwordsafe_ssm_change_password_on_register defaults OFF, because SSM
            # auto-management rotates on its own schedule) reading it here would leave
            # every VM's Vault account holding the throwaway — a checkout that hands out
            # a key which does not log in, until some later rotation.
            if config_service.get_bool("passwordsafe_vault_sync_converge", True):
                try:
                    await ps_api_service.change_managed_account_password(
                        int(result["ps_managed_account_id"]))
                    result["ps_vault_change_triggered"] = True
                except Exception as ce:  # noqa: BLE001
                    logger.warning("PRA Vault key sync for %s: post-link Change Password "
                                   "failed (the pair converges at the next scheduled "
                                   "rotation): %s", vm_name, ce)
        job_service.update_progress(
            db, job_id, 98, f"{vault_name} is synced into PRA Vault.")
    except Exception as e:  # noqa: BLE001 — the sync must never fail the deploy
        result["ps_vault_error"] = str(e)
        logger.warning("PRA Vault key sync failed for %s: %s", vm_name, e)


async def unwire_pra_vault_key_sync(meta: dict, result: dict) -> None:
    """Remove the PRA Vault key-sync artifacts a deploy created, in the order that
    keeps every step meaningful: unlink the pair, off-board the Password Safe mirror,
    then destroy the PRA Vault account. Each step is best-effort and keyed off its own
    metadata, so a partially-wired VM tears down exactly what it has.

    Called by :func:`deregister` before the parent managed account the link hangs off
    is off-boarded."""
    from . import ps_api_service, ps_resource_service, terraform_pra_service as pra
    meta = meta or {}

    if meta.get("ps_vault_mirror_tf_state"):
        parent_id = str(meta.get("ps_managed_account_id") or "")
        sub_id = str(meta.get("ps_vault_mirror_account_id") or "")
        if parent_id.isdigit() and sub_id.isdigit():
            try:
                await ps_api_service.unlink_synced_account(
                    parent_account_id=int(parent_id), synced_account_id=int(sub_id))
            except Exception as e:  # noqa: BLE001
                logger.warning("PRA Vault key sync unlink failed: %s", e)
        try:
            await ps_resource_service.deregister(meta["ps_vault_mirror_tf_state"])
            result["ps_vault_mirror_removed"] = meta.get("ps_vault_mirror_system_id") or True
        except Exception as e:  # noqa: BLE001
            logger.warning("PRA Vault key sync mirror removal failed: %s", e)
            result["ps_vault_error"] = f"PS mirror removal failed: {e}"

    if meta.get("ps_vault_tf_state"):
        try:
            await pra.remove_vault_account(meta["ps_vault_tf_state"])
            result["ps_vault_removed"] = meta.get("ps_vault_account_name") or True
        except Exception as e:  # noqa: BLE001
            logger.warning("PRA Vault key account removal failed: %s", e)
            result["ps_vault_error"] = (
                (result.get("ps_vault_error", "") + f" vault account removal failed: {e}")
                .strip())


async def deregister(meta: dict, result: dict) -> None:
    """Off-board the managed system + account a deploy registered (if any). Non-fatal.

    Removes the PRA Vault key-sync artifacts first — the SyncedAccounts link hangs off
    the managed account this then off-boards, and the mirror is a managed system of its
    own that nothing else would clean up. Doing it here rather than in each cloud's
    destroy path makes the ordering correct by construction for every caller."""
    # Belt and braces: no cloud's destroy path wraps this call, and off-boarding the
    # VM's own managed system must not be lost to a PRA-side problem.
    try:
        await unwire_pra_vault_key_sync(meta, result)
    except Exception as e:  # noqa: BLE001
        logger.warning("PRA Vault key sync teardown failed: %s", e)
        result["ps_vault_error"] = str(e)
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
