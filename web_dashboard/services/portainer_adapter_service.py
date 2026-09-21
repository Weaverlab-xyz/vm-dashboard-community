"""Pair the configured Portainer with its Cloud Functions Entitle adapter.

Portainer has **no Entitle connector at all**, so the ``portainer_access`` Cloud
Function is the only route to just-in-time access for it. That function is already
complete — it serves the Entitle Remote Adapter contract, mints ``jit-``-prefixed
standard users, grants through team membership, and refuses to touch any account it
did not create (see ``functions/fnworkloads/portainer_access.py``).

What was missing is a supported way to *get* one. A hand-deploy from the Cloud
Functions form has to be told its target, and the target is read from the function's
own environment, so a deploy that named the wrong Portainer — or no Portainer —
cannot be finished afterwards. This module owns the pairing instead: it pushes the
Portainer API token into the cloud's own secret store, deploys the adapter in the
node's cloud and region, opens the node's firewall to it, and registers it in Entitle.

Deliberately modelled on :mod:`cloud_db_adapter_service`, which solved the same shape
for ``db_grant``. Where the two agree, the comment explaining why lives there.

The one place they differ is *reach*. A database has no public endpoint, so the
db_grant adapter is VPC-attached and that is the end of it. The Portainer node does
have a public IP, but its firewall is **fail-closed** — ``refresh_portainer_firewall``
admits only the manual CIDRs, the dashboard's own egress /32 and the Gateway /32s —
and a public Lambda / Cloud Run / Function App has no stable egress address to add to
that list. A public adapter would therefore deploy green and then time out on every
single grant. So a managed node is reached over the **VPC, at its internal IP**, and
the adapter's own subnet range joins the firewall's merged set the same way a
Gateway's egress /32 does.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import config_service, job_service

logger = logging.getLogger(__name__)


class AdapterPairingError(Exception):
    pass


#: The Cloud Functions workload that IS the adapter. Public: the Portainer page needs
#: it to find the existing adapter, and a second spelling of "portainer_access" is how
#: the lookup and the deploy drift apart.
ADAPTER_WORKLOAD = "portainer_access"

#: Portainer is a singleton in this dashboard — one ``portainer_url`` — so the adapter
#: gets one fixed, deterministic name. Re-pairing then finds the same function instead
#: of accumulating one per attempt.
_ADAPTER_NAME = "jit-portainer"

#: Key the Portainer API token is staged under, in the cloud's own secret store.
_SECRET_KEY = "portainer-adapter-pat"

#: Where that token is staged so the FUNCTION can read it. Never passed to the
#: function as a value — each cloud resolves a reference.
_SECRET_BACKEND = {"aws": "aws_sm", "azure": "azure_kv", "gcp": "gcp_sm"}

#: For the staging failure message: the Secrets page panel an operator has to go fix,
#: spelled as that page spells it — "gcp_sm" is the internal key, not a place.
_BACKEND_LABEL = {"aws_sm": "AWS Secrets Manager", "azure_kv": "Azure Key Vault",
                  "gcp_sm": "GCP Secret Manager"}

#: The node serves its UI on 9443 over a self-signed certificate.
_NODE_PORT = 9443

#: Config key holding the adapter function's own subnet range, so
#: ``portainer_node_service.refresh_portainer_firewall`` can admit it. Runtime-set on
#: pair and cleared on retire, exactly like ``portainer_ui_jumpoint_egress_ip``.
SOURCE_CIDR_KEY = "portainer_adapter_source_cidr"


def adapter_name() -> str:
    """The deterministic Cloud Functions name for the Portainer adapter."""
    from . import cloud_function_service
    return cloud_function_service.normalize_name(_ADAPTER_NAME)


def _entitle_registration_enabled() -> bool:
    """Whether the Entitle integration is switched on. Read at run time, not import
    time, because config_service is backed by the app_config table."""
    return config_service.get_bool("entitle_registration_enabled", False)


def ineligible_reason() -> Optional[str]:
    """Why the Portainer adapter cannot be deployed, or None — the **config-only**
    half, phrased in the operator's terms.

    Single source of truth for the card's enabled state and for :func:`start_pairing`,
    so the card can never offer what the endpoint refuses. Deliberately cheap and
    synchronous: it makes no cloud calls, so the page can render it on every poll.

    The checks that DO need a cloud call — is the node running, does its region have a
    functions subnet — belong to :func:`preflight`, which the pair route awaits before
    queuing anything.
    """
    if not config_service.get_bool("portainer_enabled", True):
        return "Portainer is disabled — enable it in Settings -> Integrations"
    if not config_service.get_bool("cloud_functions_enabled", False):
        return ("Cloud Functions is disabled — the portainer_access adapter is a Cloud "
                "Function, so enable it in Settings -> Integrations first")
    if not (config_service.get("portainer_url") or "").strip():
        return ("no Portainer is configured — deploy the managed node above, or set "
                "portainer_url in Settings -> Containers")
    if not (config_service.get("portainer_pat") or "").strip():
        return ("no Portainer API token is stored, and the adapter authenticates with "
                "one — add it in Settings -> Containers")
    return None


# ── Where the adapter points ──────────────────────────────────────────────────

async def resolve_target(*, cloud: str = "", region: str = "",
                         network_mode: str = "") -> dict:
    """Resolve the Portainer the adapter will aim at, and where the adapter must run.

    Returns ``{url, verify_ssl, via, cloud, region, network_mode, managed, node_name,
    node_status}``.

    A **managed** node is aimed at over the VPC at its internal IP: see the module
    docstring for why the public URL is not an option under a fail-closed firewall.
    Its own cloud and region are also the adapter's, because a VPC is regional and a
    function in another region could not attach to the node's network at all.

    A Portainer this dashboard merely points at is a different situation entirely — we
    know nothing about its network — so it is aimed at over its configured public URL,
    and the caller has to say which cloud and region the adapter runs in.
    """
    from . import managed_node_service

    spec = managed_node_service.PORTAINER
    node_cloud = managed_node_service.node_cloud(spec)
    configured_url = (config_service.get("portainer_url") or "").strip()
    verify_ssl = config_service.get_bool("portainer_verify_ssl", True)

    placement: dict = {}
    nodes: list = []
    try:
        placement = managed_node_service.resolve_placement(node_cloud, spec)
        if placement.get("account"):
            nodes = await managed_node_service.list_nodes(node_cloud, spec, placement)
    except Exception as exc:
        # Not fatal: an unreachable node cloud only means we cannot offer the VPC
        # path, and the public path may still be exactly right.
        logger.warning("portainer adapter: could not list the managed node (%s) — "
                       "falling back to the configured URL", exc)

    node = nodes[0] if nodes else {}
    internal_ip = (node.get("internal_ip") or "").strip()
    if node and internal_ip:
        return {
            "url": f"https://{internal_ip}:{_NODE_PORT}",
            # The node's certificate is self-signed and is issued for neither its
            # internal nor its external address, so verification cannot succeed on
            # this path. Stated here rather than left to the operator, because a
            # silently-failing TLS handshake reads as an unreachable Portainer.
            "verify_ssl": False,
            "via": "internal",
            "cloud": node_cloud,
            "region": placement.get("region", ""),
            "network_mode": "vpc",
            "managed": True,
            "node_name": node.get("name", ""),
            "node_status": node.get("status", "UNKNOWN"),
        }

    return {
        "url": configured_url,
        "verify_ssl": verify_ssl,
        "via": "public",
        "cloud": (cloud or "").strip().lower(),
        "region": (region or "").strip(),
        "network_mode": (network_mode or "public").strip(),
        "managed": False,
        "node_name": node.get("name", "") if node else "",
        "node_status": node.get("status", "") if node else "",
    }


async def preflight(*, cloud: str = "", region: str = "",
                    network_mode: str = "") -> dict:
    """The cloud-touching half of eligibility. Returns the resolved target.

    Raises :class:`AdapterPairingError` with a message an operator can act on. Called
    from the pair route so an impossible pairing fails at the click, not three minutes
    into a job — and again from :func:`run_pairing`, because the two are minutes apart
    and the node can stop in between.
    """
    from . import cloud_function_service

    reason = ineligible_reason()
    if reason:
        raise AdapterPairingError(reason)

    target = await resolve_target(cloud=cloud, region=region,
                                  network_mode=network_mode)
    if target["managed"] and target["node_status"] != "RUNNING":
        raise AdapterPairingError(
            f"the Portainer node is {target['node_status']!r} — the adapter is "
            f"configured against a running Portainer (it reads the team list before "
            f"registering), so start the node first")
    if not target["url"]:
        raise AdapterPairingError(
            "no address for Portainer — the adapter reads its target from its own "
            "environment and cannot be told one later")
    if not target["cloud"]:
        raise AdapterPairingError(
            "no cloud chosen for the adapter function, and there is no managed "
            "Portainer node to take one from")
    if not target["region"]:
        raise AdapterPairingError(
            "no region chosen for the adapter function, and there is no managed "
            "Portainer node to take one from")

    # The real resolver, not a re-derivation of it: without a functions subnet for
    # THIS region, _resolved_network falls every network field back to the flat keys
    # and the function would come up on the DEFAULT region's network while the row,
    # the job and the operator all say otherwise. Same restriction the Databases
    # page's adapter-pair endpoint applies.
    try:
        target["network"] = cloud_function_service._resolved_network(
            target["cloud"], target["region"],
            network_mode=target["network_mode"],
            subnet_ids=None, subnet_id="", vpc_connector="",
            security_group_ids=None)
    except Exception as exc:
        raise AdapterPairingError(
            f"the adapter has to run in {target['region']} to reach Portainer, and "
            f"that region has no functions network configured: {exc}") from exc
    return target


def build_environment(*, portainer_url: str, verify_ssl: bool,
                      dry_run: bool = False) -> dict:
    """The NON-SECRET environment the adapter needs to find its Portainer.

    Pure — every value is passed in. Everything the function is told about its target
    comes from here rather than from a request, which is what stops a caller
    redirecting a grant at another Portainer.
    """
    if not portainer_url:
        raise AdapterPairingError("portainer_url is required")
    return {
        "FN_PORTAINER_URL": portainer_url,
        "FN_PORTAINER_VERIFY_SSL": "1" if verify_ssl else "0",
        # Armed unless the caller explicitly asks otherwise. The workload's own default
        # is dry run, and that is right for a hand-deploy from the Functions form; it
        # is wrong for this button, which exists to make Portainer requestable. A
        # silently no-op adapter is the worse surprise — the same call the Databases
        # page makes for db_grant.
        "FN_PORTAINER_DRY_RUN": "1" if dry_run else "0",
    }


# ── The staged API token ──────────────────────────────────────────────────────

def _backend_for(cloud: str) -> str:
    backend = _SECRET_BACKEND.get(cloud)
    if not backend:
        raise AdapterPairingError(f"no secret backend for cloud {cloud!r}")
    return backend


def _stage_pat_secret(cloud: str) -> dict:
    """Write the Portainer API token to the cloud's own secret store and return the
    ``secret_environment`` entry the function resolves it through.

    The dashboard holds this token encrypted in ``app_config``, which the function
    cannot read — it is in a different trust domain by design. Staging it in the
    cloud's store is what lets each platform resolve it for the function without the
    value ever passing through Terraform state, the job record, or the function's own
    settings page.
    """
    from . import secrets_backend_service

    pat = (config_service.get("portainer_pat") or "").strip()
    if not pat:
        raise AdapterPairingError(
            "no Portainer API token is stored — add it in Settings -> Containers")
    backend = _backend_for(cloud)
    try:
        name = secrets_backend_service.write_sync(backend, _SECRET_KEY, pat)
    except Exception as exc:
        # The job detail view shows error_message and nothing else, so a bare SDK
        # error here reads as a broken secret store. Name the stage and the backend,
        # because staging is the one step whose prerequisite (a configured secret
        # store) is not implied by having a Portainer at all.
        raise AdapterPairingError(
            f"could not stage the Portainer API token in the {backend} secret store, "
            f"which the adapter reads it from — check Secrets -> "
            f"{_BACKEND_LABEL.get(backend, backend)} and use its Test button: {exc}"
        ) from exc

    if cloud == "aws":
        # The ARN, not the name: the execution role's policy names ARNs, and AWS
        # appends a random six-character suffix to every one, so the name cannot be
        # turned into one by string concatenation.
        return {"FN_PORTAINER_API_KEY": _aws_secret_arn(name)}
    # GCP takes the Secret Manager secret id; Azure takes the Key Vault secret name.
    return {"FN_PORTAINER_API_KEY": name}


def _aws_secret_arn(name: str) -> str:
    import boto3
    from . import aws_service
    client = boto3.client("secretsmanager",
                          **aws_service._aws_kwargs(config_service.get("aws_region") or ""))
    return str(client.describe_secret(SecretId=name)["ARN"])


def _already_gone(exc: Exception) -> bool:
    """Whether a delete failed because there was nothing there.

    Three SDKs, three shapes; matched structurally rather than by class so an absent
    optional dependency cannot become the reason a teardown reports a leaked
    credential. The reasoning in full is in
    :func:`cloud_db_adapter_service._already_gone`.
    """
    if type(exc).__name__ in ("NotFound", "ResourceNotFoundError",
                              "ResourceNotFoundException"):
        return True
    if getattr(exc, "code", None) == 404 or getattr(exc, "status_code", None) == 404:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return code in ("ResourceNotFoundException", "ResourceNotFound")
    return False


async def restage_pat(db) -> dict:
    """Push the token currently in ``portainer_pat`` to the paired adapter.

    The staging step runs exactly once, inside the pairing job, so before this the
    only way to give a deployed adapter a different token was to retire it and pair
    again — destroying and rebuilding a working function, re-registering it in
    Entitle, and taking a new Entitle integration id, all to rewrite one secret.
    Every reason a token changes (a re-mint here, a rotation in Portainer's UI, an
    ephemeral node that came back with a new DB) hit that.

    The function is restarted where the platform would otherwise keep serving the old
    value — see :func:`cloud_function_service.restart_function`, which is also where
    the two clouds that need no restart are explained.

    Returns ``{restaged, fn_id, cloud, restarted, note}``; ``restaged`` is False, and
    nothing is written, when no adapter is paired.
    """
    from . import cloud_function_service

    row = find_adapter(db)
    if row is None:
        return {"restaged": False, "fn_id": "", "cloud": "", "restarted": False,
                "note": "No adapter is paired, so there is no staged copy to update."}
    cloud = row.cloud or ""
    await asyncio.to_thread(_stage_pat_secret, cloud)
    restarted = await cloud_function_service.restart_function(row)
    if restarted:
        note = (f"{row.name} was restarted so it re-reads the Key Vault reference; "
                f"give it a few seconds to come back.")
    elif cloud == "gcp":
        note = (f"{row.name} resolves the secret at instance start, so the next cold "
                f"start uses the new token; an instance still warm keeps the old one "
                f"for a few minutes.")
    else:
        note = (f"{row.name} re-reads Secrets Manager every 5 minutes, so the new "
                f"token takes effect within that.")
    logger.info("portainer adapter: restaged the API token for %s in %s (restarted=%s)",
                row.name, cloud, restarted)
    return {"restaged": True, "fn_id": row.id, "cloud": cloud,
            "restarted": restarted, "note": note}


def retire_pat_secret(cloud: str) -> str:
    """Delete the token :func:`_stage_pat_secret` put in the cloud's secret store.
    Returns the ref it removed, or ``""`` when there was nothing to do.

    Blocking (three cloud SDKs); call it off the event loop.

    Absence is the NORMAL outcome, not a failure: a Portainer that was never paired has
    no staged token. Anything else raises, with the ref in the message — what is left
    behind is a live Portainer API token in a store nothing justifies any more, and an
    orphan nothing ever names is an orphan nobody finds.
    """
    from . import secrets_backend_service

    backend = _SECRET_BACKEND.get(cloud)
    if not backend:
        return ""
    # The ref is the WRITER's, via secrets_backend_service.ref_for: each store mangles
    # the key on the way in and the mangled form is what identifies the secret
    # afterwards, so a delete addressed at the key would 404 forever while the token
    # stayed live.
    ref = ""
    try:
        ref = secrets_backend_service.ref_for(backend, _SECRET_KEY)
        secrets_backend_service.delete_sync(backend, ref)
    except Exception as exc:
        if _already_gone(exc):
            logger.info("portainer adapter: no staged API token to retire in %s", cloud)
            return ""
        raise AdapterPairingError(
            f"the adapter's staged Portainer API token {ref or _SECRET_KEY} in "
            f"{_BACKEND_LABEL.get(backend, backend)} was not deleted — it is a live "
            f"Portainer credential, remove it by hand: {exc}") from exc
    logger.info("portainer adapter: retired the staged API token in %s", cloud)
    return ref


# ── The node firewall ─────────────────────────────────────────────────────────

async def _subnet_cidrs(cloud: str, region: str, network: dict) -> list:
    """The CIDR(s) of the subnet(s) the adapter function is attached to.

    The node's firewall is source-restricted and applies to intra-VPC ingress too, so
    without this the function reaches the node's internal IP and is dropped. Resolved
    from each cloud's existing network-options call — which already reports every
    subnet with its range — rather than three new per-cloud lookups.

    Best-effort by design: it returns ``[]`` rather than raising, because a pairing
    that deployed a working function should not fail on the firewall step. The caller
    warns instead, and ``portainer_allowed_source_cidrs`` remains the manual way in.
    """
    if not network:
        return []
    try:
        if cloud == "aws":
            from . import aws_service
            wanted = set(network.get("subnet_ids") or [])
            opts = await aws_service.get_network_options(region)
            return sorted({s["cidr"] for s in opts.get("subnets", [])
                           if s.get("id") in wanted and s.get("cidr")})
        if cloud == "azure":
            from . import azure_service
            wanted = (network.get("subnet_id") or "").strip().lower()
            opts = await azure_service.get_network_options(
                region,
                config_service.get("azure_vnet_resource_group") or "",
                config_service.get("azure_resource_group") or "")
            return sorted({s["address_prefix"] for s in opts.get("subnets", [])
                           if (s.get("id") or "").lower() == wanted
                           and s.get("address_prefix")})
        if cloud == "gcp":
            from . import gcp_service
            # A BARE subnetwork name, which is what _resolved_network hands Terraform
            # for Direct VPC egress; the options call reports the same bare name.
            wanted = (network.get("vpc_subnetwork") or "").strip()
            if not wanted:
                return []
            project = config_service.get("gcp_project_id") or ""
            opts = await gcp_service.get_network_options(project, region, "")
            return sorted({s["ip_cidr_range"] for s in opts.get("subnets", [])
                           if s.get("name") == wanted and s.get("ip_cidr_range")})
    except Exception as exc:
        logger.warning("portainer adapter: could not resolve the adapter's subnet "
                       "range in %s/%s (%s)", cloud, region, exc)
    return []


def _node_placement(cloud: str, region: str):
    """The node's placement for ``region``, or None if it cannot be resolved.

    Passed to ``refresh_portainer_firewall`` rather than letting it re-resolve: with no
    region it falls back to the DEFAULT region on AWS/Azure, and would then apply the
    allow-list to that region's VPC / resource group while the node — and the adapter
    beside it — are somewhere else.
    """
    from . import managed_node_service
    try:
        return managed_node_service.resolve_placement(
            cloud, managed_node_service.PORTAINER, region=region or None)
    except Exception as exc:
        logger.warning("portainer adapter: could not resolve the node placement in "
                       "%s/%s (%s)", cloud, region, exc)
        return None


async def _open_firewall_to_adapter(db, target: dict) -> list:
    """Persist the adapter's subnet range and re-apply the node's firewall.

    Returns the ranges added, so the job result can say what was opened. A no-op for a
    Portainer this dashboard does not manage: there is no firewall of ours to change.

    Loud on failure, deliberately. The alternative — warn and carry on — registers an
    integration whose every grant times out, and the timeout surfaces in Entitle rather
    than here. Failing the job leaves the function deployed but unregistered, which is
    a state the card explains and an operator can finish by hand.
    """
    from . import portainer_node_service

    if not target.get("managed"):
        return []
    cidrs = await _subnet_cidrs(target["cloud"], target["region"],
                               target.get("network") or {})
    if not cidrs:
        logger.warning("portainer adapter: no subnet range resolved, so the node "
                       "firewall was left alone — if grants time out, add the "
                       "function's range to portainer_allowed_source_cidrs")
        return []
    config_service.set(SOURCE_CIDR_KEY, ",".join(cidrs))
    try:
        await portainer_node_service.refresh_portainer_firewall(
            db, placement=_node_placement(target["cloud"], target["region"]))
    except Exception as exc:
        raise AdapterPairingError(
            f"the adapter deployed, but the node firewall was not opened to "
            f"{', '.join(cidrs)} — every grant would time out. Add that range to "
            f"portainer_allowed_source_cidrs, or remove the adapter and retry: {exc}"
        ) from exc
    return cidrs


# ── Status ────────────────────────────────────────────────────────────────────

def find_adapter(db):
    """The ``portainer_access`` function this module deployed, or None.

    Looked up by the deterministic name AND the workload, so a function an operator
    happened to call ``jit-portainer`` with some other workload is not mistaken for
    the adapter.
    """
    from . import cloud_function_service
    name = adapter_name()
    return cloud_function_service.find_by_names(
        db, [name], workload=ADAPTER_WORKLOAD).get(name)


def status(db) -> dict:
    """What the Portainer page's just-in-time access card renders.

    Synchronous and cloud-free, so the card can poll it. ``viable`` is the button's
    enabled state and comes from :func:`ineligible_reason`, which the pair endpoint
    checks again — so the card can never offer what the endpoint refuses.
    """
    reason = ineligible_reason()
    row = find_adapter(db)
    env = {}
    if row is not None:
        import json
        try:
            env = json.loads(row.env_ref or "{}") or {}
        except (TypeError, ValueError):
            env = {}
    return {
        "name": adapter_name(),
        "workload": ADAPTER_WORKLOAD,
        "viable": reason is None,
        "ineligible_reason": reason or "",
        "entitle_enabled": _entitle_registration_enabled(),
        "fn_id": row.id if row is not None else "",
        "status": row.status if row is not None else "",
        "cloud": row.cloud if row is not None else "",
        "region": row.region if row is not None else "",
        "network_mode": row.network_mode if row is not None else "",
        "invoke_url": (row.invoke_url or "") if row is not None else "",
        "entitle_integration_id": ((row.entitle_integration_id or "")
                                   if row is not None else ""),
        "target_url": env.get("FN_PORTAINER_URL", ""),
        # The workload treats anything truthy — including unset — as dry run, so the
        # card must read "armed" from the value actually deployed, not from its absence.
        "dry_run": env.get("FN_PORTAINER_DRY_RUN", "1") not in ("0", "false", "False"),
        "source_cidrs": [c.strip() for c
                         in (config_service.get(SOURCE_CIDR_KEY) or "").split(",")
                         if c.strip()],
    }


# ── Retire ────────────────────────────────────────────────────────────────────

async def retire_adapter(db, *, created_by: str = "portainer-teardown") -> str:
    """Deregister and destroy the Portainer adapter, and retire its staged token.
    Returns the function id it removed, or ``""`` when there was never one.

    An adapter that outlives its Portainer is a running, billable function that can
    only ever fail, still holding a live API token in the cloud's secret store and a
    grantable integration in Entitle's catalogue. Entitle goes first — shut the tap,
    then drain — but a failure there does not skip the destroy: the function is the
    part that costs money either way.
    """
    from . import cloud_function_service

    row = find_adapter(db)
    if row is None:
        # Still clear the firewall entry: a retired adapter's range has no business
        # staying in the node's allow-list, and a pairing that failed after the
        # firewall step leaves one behind with no function to find.
        _clear_source_cidr()
        return ""
    fn_id = row.id
    # Read off the row, not from config: for a managed pairing these ARE the node's
    # cloud and region (the adapter has to share them to reach its VPC), and the row is
    # the only record that survives a config key being cleared out from under us.
    cloud = row.cloud
    region = row.region or ""
    problems: list = []

    if row.entitle_integration_id:
        try:
            job = cloud_function_service.start_entitle_register(
                db, fn_id, action="deregister", created_by=created_by)
            await cloud_function_service.run_entitle_register(
                db, fn_id=fn_id, job_id=job["job_id"], action="deregister")
        except Exception as exc:                                # pragma: no cover
            problems.append(f"Entitle deregistration raised: {exc}")
        db.refresh(row)
        # run_entitle_register reports through the job, not by raising, and clears the
        # id only when the removal really happened — so the column is the outcome.
        if row.entitle_integration_id:
            problems.append(
                f"the adapter's Entitle integration {row.entitle_integration_id} was "
                f"not removed — see the cloudfn_entitle_register job")

    try:
        job = cloud_function_service.start_decommission(db, fn_id, created_by=created_by)
        await cloud_function_service.run_decommission(db, fn_id=fn_id,
                                                      job_id=job["job_id"])
    except Exception as exc:
        problems.append(f"adapter function destroy raised: {exc}")
    db.refresh(row)
    if row.status != "deleted":
        problems.append(
            f"the adapter function {adapter_name()} was not destroyed (status: "
            f"{row.status or 'unknown'}) — it is still billable and can only fail now; "
            f"delete it from the Cloud Functions page")

    # Last, and only once the function that reads it is gone: retiring the token first
    # would leave a live adapter authenticating with a credential that no longer exists,
    # which fails as "Portainer refused the token" rather than as a teardown problem.
    try:
        await asyncio.to_thread(retire_pat_secret, cloud)
    except Exception as exc:
        problems.append(str(exc))

    # Only when there was a range to give back — otherwise this is a cloud call that
    # can only re-apply what is already applied.
    had_cidr = bool(config_service.get(SOURCE_CIDR_KEY))
    _clear_source_cidr()
    if had_cidr:
        try:
            from . import portainer_node_service
            await portainer_node_service.refresh_portainer_firewall(
                db, placement=_node_placement(cloud, region))
        except Exception as exc:                                # pragma: no cover
            # Best-effort here, unlike on the way in: what is left behind is a range
            # allowed to reach the node, not an integration that cannot work.
            logger.warning("portainer adapter: firewall refresh after retire failed "
                           "(continuing): %s", exc)

    if problems:
        raise AdapterPairingError("; ".join(problems))
    logger.info("portainer adapter: retired adapter %s fn_id=%s", adapter_name(), fn_id)
    return fn_id


def _clear_source_cidr() -> None:
    try:
        config_service.set(SOURCE_CIDR_KEY, "")
    except Exception as exc:                                    # pragma: no cover
        logger.warning("portainer adapter: could not clear %s (continuing): %s",
                       SOURCE_CIDR_KEY, exc)


# ── The job ───────────────────────────────────────────────────────────────────

def start_pairing(db, *, created_by: str = "", cloud: str = "", region: str = "",
                  network_mode: str = "", dry_run: bool = False) -> dict:
    """Queue a ``portainer_adapter_pair`` job.

    Validates the config-only half here so an impossible pairing fails at the click.
    The cloud-touching half is :func:`preflight`, which the route awaits before
    calling this.
    """
    reason = ineligible_reason()
    if reason:
        raise AdapterPairingError(reason)
    job = job_service.create_job(
        db, job_type="portainer_adapter_pair", created_by=created_by,
        metadata={"action": "pair", "cloud": cloud, "region": region,
                  "network_mode": network_mode, "dry_run": bool(dry_run)})
    return {"ok": True, "job_id": job.id}


def start_retire(db, *, created_by: str = "") -> dict:
    """Queue a ``portainer_adapter_pair`` job that removes the adapter.

    Deliberately unconditional: an adapter that should not have been deployed is
    exactly the one that most needs removing, so this refuses nothing.
    """
    job = job_service.create_job(
        db, job_type="portainer_adapter_pair", created_by=created_by,
        metadata={"action": "retire"})
    return {"ok": True, "job_id": job.id}


async def run_job(db, *, job_id: str, meta: dict) -> None:
    """Worker entry for a ``portainer_adapter_pair`` job."""
    if (meta or {}).get("action") == "retire":
        await _run_retire(db, job_id=job_id)
    else:
        await _run_pair(db, job_id=job_id, meta=meta or {})


async def _run_pair(db, *, job_id: str, meta: dict) -> None:
    """Stage the token, deploy the adapter, open the firewall to it, register it.

    One job for all four stages: they are useless individually, and an operator
    watching a half-finished pairing cannot tell whether to retry or clean up.
    """
    from . import cloud_function_service

    job_service.set_running(db, job_id)
    try:
        target = await preflight(cloud=meta.get("cloud", ""),
                                 region=meta.get("region", ""),
                                 network_mode=meta.get("network_mode", ""))
        dry_run = bool(meta.get("dry_run"))

        # Refuse rather than redeploy. cloud_function_service.deploy does not look the
        # name up and every deploy starts from an empty Terraform directory, so a
        # second pairing leaves a duplicate row wedged in 'deploying' and an "already
        # exists" apply failure.
        existing = find_adapter(db)
        if existing is not None:
            raise AdapterPairingError(
                f"Portainer already has the adapter function {adapter_name()!r} "
                f"({existing.status}) — remove it from the Portainer page, or delete "
                f"it on the Cloud Functions page, before deploying another")

        job_service.update_progress(db, job_id, 15,
                                    "Staging the Portainer API token…")
        secret_environment = await asyncio.to_thread(_stage_pat_secret,
                                                     target["cloud"])
        environment = build_environment(portainer_url=target["url"],
                                        verify_ssl=target["verify_ssl"],
                                        dry_run=dry_run)

        job_service.update_progress(db, job_id, 30, "Deploying the adapter function…")
        deployed = cloud_function_service.deploy(
            db, cloud=target["cloud"], region=target["region"],
            name=adapter_name(), workload=ADAPTER_WORKLOAD,
            created_by="portainer-pairing",
            network_mode=target["network_mode"],
            environment=environment,
            secret_environment=secret_environment)
        fn_id = deployed["fn_id"]
        await cloud_function_service.run_deploy_apply(
            db, fn_id=fn_id, job_id=deployed["job_id"],
            tf_variables=deployed["tf_variables"])

        fn_row = cloud_function_service.get_function(db, fn_id)
        if not fn_row or fn_row.status != "available":
            raise AdapterPairingError(
                f"adapter function did not deploy (status: "
                f"{getattr(fn_row, 'status', 'missing')}) — see its job for the "
                "terraform output")

        # Before Entitle, because the registration preflight calls the adapter's own
        # check_config route, which talks to Portainer: a closed firewall would surface
        # there as "Portainer is unreachable" and leave the operator debugging the
        # wrong half.
        job_service.update_progress(db, job_id, 60,
                                    "Opening the node firewall to the adapter…")
        opened = await _open_firewall_to_adapter(db, target)

        # The Entitle leg is skippable, and only this leg: an adapter that is deployed
        # and pointed at its Portainer is useful on its own, and refusing to deploy one
        # because the Entitle integration happens to be switched off would make the
        # card dead weight on exactly the installs still being set up.
        entitle_skipped = not _entitle_registration_enabled()
        if entitle_skipped:
            job_service.update_progress(
                db, job_id, 90,
                "Adapter deployed. Entitle registration is disabled — register it from "
                "the Cloud Functions page once entitle_registration_enabled is set.")
        else:
            job_service.update_progress(db, job_id, 75,
                                        "Registering the adapter in Entitle…")
            register = cloud_function_service.start_entitle_register(
                db, fn_id, action="register", created_by="portainer-pairing")
            await cloud_function_service.run_entitle_register(
                db, fn_id=fn_id, job_id=register["job_id"], action="register")
            db.refresh(fn_row)

        job_service.set_completed(db, job_id, {
            "fn_id": fn_id,
            "target_url": target["url"],
            "via": target["via"],
            "cloud": target["cloud"],
            "region": target["region"],
            "network_mode": target["network_mode"],
            "firewall_opened_to": opened,
            "entitle_integration_id": fn_row.entitle_integration_id,
            "entitle_skipped": entitle_skipped,
            "dry_run": dry_run,
        })
        logger.info("portainer adapter paired fn_id=%s integration=%s dry_run=%s "
                    "entitle_skipped=%s", fn_id, fn_row.entitle_integration_id,
                    dry_run, entitle_skipped)
    except Exception as exc:
        logger.error("portainer adapter pairing failed: %s", exc)
        job_service.set_failed(db, job_id, str(exc))


async def _run_retire(db, *, job_id: str) -> None:
    job_service.set_running(db, job_id)
    try:
        job_service.update_progress(db, job_id, 20, "Removing the adapter…")
        fn_id = await retire_adapter(db, created_by="portainer-adapter-retire")
        job_service.set_completed(db, job_id, {
            "fn_id": fn_id,
            "removed": bool(fn_id),
        })
    except Exception as exc:
        logger.error("portainer adapter retire failed: %s", exc)
        job_service.set_failed(db, job_id, str(exc))
