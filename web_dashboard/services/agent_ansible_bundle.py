"""The sealed run bundle a remote agent fetches to execute one Config-Management job.

The signed job envelope carries four scalars and nothing else (``agent_ansible_meta``).
Everything a run actually needs — the playbook bytes, the SSH key, the become password, a
database login — is assembled here, per job, and sealed to a key the agent generated for
that one fetch. Two things follow from that split, and both are the point:

* **No executable content is ever merely *signed*.** A signature proves the dashboard sent
  it. The seal proves it was sent *for this agent, this job and this target*, and the
  ciphertext is opaque to the TLS-inspecting proxy the whole remote-agent feature exists to
  work through.
* **Nothing sensitive is written down.** Job metadata holds refs; this module turns them
  into values at the moment the agent asks, through the same
  :mod:`ansible_credentials` resolver the dashboard-local runner uses. There is no second
  credential path to drift out of step with the first.

One credential here does not begin life as a ref, and it is worth knowing why. A POV
Resource Broker run takes its Windows login from the **lab platform's** stored credentials
(``services/pov_credentials``), read at this moment rather than stored — so the job row
names an environment and a VM instead of a source, and there is no per-POV Windows
password in this database at all.

**What this deliberately does NOT send: an inventory, or any ``ansible_*`` variable.**
That is not tidiness, it is the security boundary. An inventory is a place to say
``ansible_connection: local`` — which turns "configure that VM over SSH" into "execute this
playbook inside the runner container", on a network the dashboard cannot otherwise reach,
with the cloud image's ``kubectl`` and ``helm`` on ``PATH``. ``ansible_python_interpreter``
and ``ansible_ssh_executable`` are the same hole wearing different hats, and ``extra_vars``
is worse than all of them because ``-e`` outranks every inventory variable in Ansible's
precedence order.

So the credential fields below are **typed**, one key per meaning, and the agent renders its
own inventory from the envelope scalars it has already signature-checked. A field this module
does not have a name for cannot be smuggled through as a variable: the agent refuses any
``extra_vars`` key matching ``^ansible_``. That refusal lives on the agent because that is
the side whose threat model includes a compromised dashboard — but the filter is applied here
too, so an operator who typed one gets a clean 400 at enqueue instead of a puzzling refusal
in Live Output half a minute later.
"""
import base64
import json
import logging
import os
import re

from . import (agent_ansible_meta, ansible_credentials, ansible_local_service,
               storage_service)

logger = logging.getLogger(__name__)


class BundleError(Exception):
    """The bundle could not be assembled. The message is operator-facing and travels to
    the agent, which puts it straight into the job's error message."""


# A variable an operator may not set, because Ansible reads it as connection configuration
# rather than data. Matched on the agent as well; this copy only moves the refusal earlier.
_RESERVED_VAR = re.compile(r"^ansible_", re.IGNORECASE)

# Bundles above this are refused with a message naming the limit. The agent's own
# `_MAX_RESPONSE_BYTES` is 1 MiB and trips as an opaque "response exceeded the size cap",
# so the useful limit is the one stated here — and base64 plus the JSON wrapper means the
# sealed body is roughly 4/3 of this before the cap on the far side applies.
MAX_BUNDLE_BYTES = 256 * 1024

# Where the agent extracts the run's files. NOT /tmp: the sibling mounts a tmpfs over /tmp
# at start, which would shadow anything placed there before start and delete it with no
# diagnostic at all. Stated here because the path is half of a contract with the agent.
JOB_DIR = "/opt/job"


def reserved_vars(extra_vars) -> list:
    """Any ``extra_vars`` key Ansible would read as connection config rather than data.

    Returned rather than raised so the enqueue endpoint can name every offending key at
    once instead of making the operator fix them one run at a time.
    """
    if not isinstance(extra_vars, dict):
        return []
    return sorted(k for k in extra_vars if _RESERVED_VAR.match(str(k)))


def _winrm_options(transport: str, port: int) -> dict:
    """Non-secret WinRM connection options for a Windows guest.

    Deliberately not ``ansible_local_service._hyperv_hostvars``: that one describes the
    *hypervisor management host* and reads its options off a connection row. This describes
    a **guest**, whose WinRM listener has nothing to do with how the agent talks to the
    hypervisor brokering it. Values are named options, not ``ansible_*`` keys — the agent
    maps them, so this module never hands over a variable name.
    """
    if transport != "winrm":
        return {}
    return {
        # 5986 is WinRM over TLS; 5985 is plain. Derived from the port rather than a
        # separate flag, because two fields that must agree eventually will not.
        "scheme": "https" if int(port) == 5986 else "http",
        "transport": "ntlm",
        # A guest's WinRM listener almost always carries a self-signed certificate, and the
        # agent is already inside the network. Stated rather than defaulted so it is visible
        # in review: this does not weaken the credential's protection, which is the seal.
        "cert_validation": "ignore",
    }


async def _playbook_and_asset(asset: str, asset_backend: str,
                              prefetched_b64: str = "") -> tuple:
    """``(playbook_yaml, asset_name, asset_bytes)`` for one run.

    A ``.yml`` asset is the playbook. Anything else is wrapped in a generated play that
    copies or runs it, and its bytes travel beside the playbook — the agent's sibling has
    **no bind mounts**, so the wrapper cannot point at a mounted asset directory the way the
    dashboard-local runner's does.

    ``prefetched_b64`` is the asset read at ENQUEUE time. Only the agent-brokered storage
    backend supplies it, and it is not an optimisation — reading that backend from here
    would deadlock. This function runs inside the agent's own bundle request, while that
    agent is blocked waiting on the response, so an ``agent_storage`` job queued for it
    could not be leased until the deadline expired. See api/config_mgmt.py.
    """
    if prefetched_b64:
        try:
            raw = base64.b64decode(prefetched_b64)
        except Exception as exc:  # noqa: BLE001
            raise BundleError(f"The stored copy of asset {asset!r} is unreadable: {exc}")
        return _split_playbook_and_asset(asset, raw)

    try:
        if asset_backend:
            raw = await storage_service.fetch_asset_in(asset_backend, asset)
        else:
            raw = base64.b64decode(await storage_service.fetch_asset_b64(asset))
    except storage_service.StorageError as exc:
        raise BundleError(f"Asset storage error: {exc}") from exc

    return _split_playbook_and_asset(asset, raw)


def _split_playbook_and_asset(asset: str, raw: bytes) -> tuple:
    """Turn the asset's bytes into ``(playbook_yaml, asset_name, asset_bytes)``.

    Split out from :func:`_playbook_and_asset` so the prefetched and the freshly-read
    paths cannot diverge on the auto-wrap semantics — the thing the ``replace`` below
    exists to keep identical between the two runners in the first place.
    """
    if ansible_local_service.asset_type(asset) == "playbook":
        return raw.decode("utf-8", "replace"), "", b""

    base = os.path.basename(asset)
    try:
        play = ansible_local_service.generate_playbook_yaml(asset)
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    # The generated play references the dashboard runner's bind-mount directory. Retarget it
    # at the directory the agent extracts into. A replace rather than a second generator so
    # there is one wrapper-play author and the auto-wrap semantics cannot drift between the
    # two runners.
    play = play.replace("/ansible/assets/", f"{JOB_DIR}/assets/")
    return play, base, raw


async def build(db, *, job, agent) -> tuple:
    """``(bundle, scrub)`` for one ``agent_ansible`` job.

    ``scrub`` is every value the agent must redact from output. It is **advisory**: the
    agent derives its own set from the typed fields it decodes, because a list is exactly
    the kind of thing that goes stale when a field is added. Sent anyway so a value the
    agent has no field for — a named ``secret_vars`` entry, say — is still redacted.

    Raises :class:`BundleError` for anything the operator can fix.
    """
    raw_meta = job.metadata_dict or {}
    meta = agent_ansible_meta.run_kwargs(raw_meta)
    problem = agent_ansible_meta.check(meta)
    if problem:
        raise BundleError(problem)

    offending = reserved_vars(meta.get("extra_vars"))
    if offending:
        raise BundleError(
            f"This run sets {', '.join(offending)} as extra vars. Ansible reads "
            f"ansible_* variables as connection configuration, not data, so a run may not "
            f"set them — one of them could redirect the play into the runner container "
            f"instead of the target. Remove them and re-run.")

    # Read off the RAW metadata, not `meta`: run_kwargs normalises to the closed key set
    # the agent's envelope uses, and these bytes deliberately are not in it — they stay on
    # the dashboard side of the bundle.
    playbook, asset_name, asset_bytes = await _playbook_and_asset(
        meta["asset"], meta["asset_backend"],
        prefetched_b64=str(raw_meta.get("asset_bytes_b64") or ""))

    run_kind = meta["run_kind"]
    transport = meta["transport"]
    target = meta["target_host"]

    # Credential refs → values, through the SAME resolver the dashboard-local runner uses.
    #
    # `cloud=""` is correct rather than lazy: there is no cloud secret store in play here, so
    # nothing should be looked for in one. The name hint is the VM's own name, which matters
    # because a Password Safe managed system for an on-prem host is as often onboarded by
    # NAME as by address — and unlike the cloud paths there is no deploy job to read a name
    # off, so this label is the only one available.
    try:
        creds = await ansible_credentials.resolve(
            db,
            secret_vars=meta.get("secret_vars"),
            secret_become_source=meta.get("secret_become_source"),
            secret_ssh_key_source=meta.get("secret_ssh_key_source"),
            epml_token_var=meta.get("epml_token_var"),
            managed_account=meta.get("managed_account"),
            managed_become=meta.get("managed_become"),
            target=target, cloud="",
            name_hint=meta.get("target_label") or "")
    except ansible_credentials.CredentialError as exc:
        raise BundleError(str(exc)) from exc

    scrub = [v for v in creds.scrub if v]

    # Operator vars, plus resolved named secret_vars — but never the connection material the
    # resolver merged into extra_vars for the inline runners. Those go in typed fields below,
    # so `ansible_*` stays absent from everything this module sends.
    extra_vars = dict(meta.get("extra_vars") or {})
    for name, value in (creds.extra_vars or {}).items():
        if not _RESERVED_VAR.match(str(name)):
            extra_vars[name] = value

    # A POV run's login comes from the LAB PLATFORM, not from this database. Fetched here,
    # at the moment the agent asks, so nothing has to be stored for it — and so a POV whose
    # template password changed picks the new one up on the next run with nothing to
    # update. The job row names an environment and a VM; neither is a credential.
    login_user = meta.get("login_user") or creds.managed_plain_vars.get("ansible_user") or ""
    login_password = creds.managed_cred_vars.get("ansible_password") or ""
    pov_env_id = str(meta.get("pov_environment_id") or "")
    pov_vm_id = str(meta.get("pov_vm_id") or "")
    if pov_env_id and pov_vm_id:
        from . import pov_resource_broker
        try:
            login_user, login_password = await pov_resource_broker.platform_login(
                db, pov_env_id, pov_vm_id)
        except pov_resource_broker.ResourceBrokerError as exc:
            raise BundleError(str(exc)) from None
        scrub.append(login_password)

    bundle = {
        "run_kind": run_kind,
        "transport": transport,
        "job_dir": JOB_DIR,
        "playbook": playbook,
        "asset_name": asset_name,
        "asset_b64": base64.b64encode(asset_bytes).decode() if asset_bytes else "",
        "extra_vars": extra_vars,
        # Typed connection material. One key per meaning; the agent maps these into host
        # vars itself so no variable name ever crosses the wire.
        "login_user": login_user,
        "login_password": login_password,
        "become_password": creds.extra_vars.get("ansible_become_password") or "",
        "ssh_private_key": creds.ssh_pem or "",
        "winrm": _winrm_options(transport, meta["target_port"]),
    }

    if run_kind == "database":
        from . import cloud_database_service
        try:
            conn = await cloud_database_service.ansible_connection_vars(db, meta["target_id"])
        except cloud_database_service.CloudDatabaseError as exc:
            raise BundleError(str(exc)) from exc
        engine = conn.get("db_engine")
        from . import ansible_cloud_run_service as acr
        if engine not in acr.ANSIBLE_DB_ENGINES:
            raise BundleError(
                f"engine {engine!r} is not supported for Ansible runs "
                f"(supported: {', '.join(acr.ANSIBLE_DB_ENGINES)}).")
        # db_* keys, not ansible_* — they are play *data* for the DB modules' login args,
        # which is why they are allowed through where a connection var is not.
        bundle["db"] = conn
        if conn.get("db_login_password"):
            scrub.append(conn["db_login_password"])

    # In-playbook BeyondTrust / Portainer lookups, the same auto-injected env the other
    # runners get. Non-`ansible_*` by construction, and carried as env rather than vars.
    from . import password_safe_runner as _psr, portainer_runner as _ptr
    env = {}
    env.update(_psr.runner_env() or {})
    env.update(_ptr.runner_env() or {})
    bundle["env"] = env
    for key in (_psr.SECRET_KEY, _ptr.SECRET_KEY):
        if env.get(key):
            scrub.append(env[key])

    for value in (bundle["login_password"], bundle["become_password"],
                  bundle["ssh_private_key"]):
        if value:
            scrub.append(value)

    payload = json.dumps(bundle)
    if len(payload.encode()) > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"This run's playbook and connection material total "
            f"{len(payload.encode()) // 1024} KB, over the {MAX_BUNDLE_BYTES // 1024} KB "
            f"limit for a run an agent executes. Split the playbook, or move large files "
            f"into a role the play fetches itself.")

    # De-duplicated, and short values dropped the way `hold_secret` would anyway — a
    # two-character password redacted everywhere would turn the log into asterisks.
    scrub = sorted({str(v) for v in scrub if v and len(str(v)) >= 4})
    return bundle, scrub
