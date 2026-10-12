"""Batched, read-only status reads from each cloud, for the resources area.

Every function here answers for MANY resources in as few calls as the API allows -- one
describe per AWS region, one aggregated list per GCP project, one list per Azure
subscription -- and returns plain dicts keyed by the id the dashboard recorded. A
resource that is absent from the answer is reported as missing by the caller; a call
that fails raises, and the caller turns that into an "unknown" for that batch only.

Blocking SDK calls; the collector runs them with ``asyncio.to_thread``.
"""
from __future__ import annotations

_AWS_CHUNK = 100


def aws_instances(region: str, instance_ids: list) -> dict:
    """``{instance_id: {state, system, instance, events}}`` for EC2 instances.

    ``describe_instances`` by FILTER, not by id: an id that no longer exists makes the
    by-id form fail the whole call, and one deleted VM must not blind the rest.
    """
    import boto3
    from ... import aws_service
    ec2 = aws_service.aws_client("ec2", region)
    out: dict = {}
    for i in range(0, len(instance_ids), _AWS_CHUNK):
        chunk = instance_ids[i:i + _AWS_CHUNK]
        pages = ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-id", "Values": chunk}])
        for page in pages:
            for res in page.get("Reservations") or []:
                for inst in res.get("Instances") or []:
                    out[inst["InstanceId"]] = {
                        "state": (inst.get("State") or {}).get("Name", ""),
                        "system": "", "instance": "", "events": []}
        live = [iid for iid in chunk if iid in out and out[iid]["state"] != "terminated"]
        if live:
            resp = ec2.describe_instance_status(InstanceIds=live, IncludeAllInstances=True)
            for st in resp.get("InstanceStatuses") or []:
                row = out.get(st.get("InstanceId"))
                if row is None:
                    continue
                row["system"] = (st.get("SystemStatus") or {}).get("Status", "")
                row["instance"] = (st.get("InstanceStatus") or {}).get("Status", "")
                row["events"] = [e.get("Description") or e.get("Code") or ""
                                 for e in st.get("Events") or []
                                 if "[Completed]" not in (e.get("Description") or "")]
    return out


def aws_databases(region: str) -> dict:
    """``{id: status}`` for every RDS instance in a region, keyed by BOTH its identifier
    and its DbiResourceId: the Terraform provider's ``id`` output is one or the other
    depending on its version."""
    import boto3
    from ... import aws_service
    rds = aws_service.aws_client("rds", region)
    out: dict = {}
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances") or []:
            status = db.get("DBInstanceStatus", "")
            for key in (db.get("DBInstanceIdentifier"), db.get("DbiResourceId")):
                if key:
                    out[key] = status
    return out


def aws_cluster(region: str, name: str) -> dict:
    """``{status, issues, nodegroups: {name: {status, issues}}}`` for one EKS cluster,
    or ``{}`` when it does not exist."""
    import boto3
    from ... import aws_service
    eks = aws_service.aws_client("eks", region)
    try:
        cluster = eks.describe_cluster(name=name)["cluster"]
    except eks.exceptions.ResourceNotFoundException:
        return {}
    out = {"status": cluster.get("status", ""),
           "issues": [i.get("message") or i.get("code") or ""
                      for i in (cluster.get("health") or {}).get("issues") or []],
           "nodegroups": {}}
    for ng in eks.list_nodegroups(clusterName=name).get("nodegroups") or []:
        info = eks.describe_nodegroup(clusterName=name, nodegroupName=ng)["nodegroup"]
        out["nodegroups"][ng] = {
            "status": info.get("status", ""),
            "issues": [i.get("message") or i.get("code") or ""
                       for i in (info.get("health") or {}).get("issues") or []]}
    return out


def gcp_instances(project: str) -> dict:
    """``{(zone, name): status}`` for every Compute Engine instance in a project."""
    from ... import gcp_service
    session = gcp_service._authed_session()
    url = (f"https://compute.googleapis.com/compute/v1/projects/{project}"
           f"/aggregated/instances")
    out, token = {}, None
    while True:
        resp = session.get(url, params={"pageToken": token} if token else None)
        if resp.status_code != 200:
            raise RuntimeError(f"GCP answered HTTP {resp.status_code}")
        body = resp.json()
        for scope, block in (body.get("items") or {}).items():
            zone = scope.rsplit("/", 1)[-1]
            for inst in block.get("instances") or []:
                out[(zone, inst.get("name"))] = inst.get("status", "")
        token = body.get("nextPageToken")
        if not token:
            return out


def gcp_databases(project: str) -> dict:
    """``{name: {state, activation}}`` for every Cloud SQL instance in a project."""
    from ... import gcp_service
    session = gcp_service._authed_session()
    resp = session.get(f"https://sqladmin.googleapis.com/v1/projects/{project}/instances")
    if resp.status_code != 200:
        raise RuntimeError(f"GCP answered HTTP {resp.status_code}")
    return {i.get("name"): {"state": i.get("state", ""),
                            "activation": (i.get("settings") or {}).get("activationPolicy", "")}
            for i in resp.json().get("items") or []}


def gcp_cluster(project: str, location: str, name: str) -> dict:
    """``{status, message, nodepools: {name: status}}`` for one GKE cluster, or ``{}``."""
    from ... import gcp_service
    session = gcp_service._authed_session()
    resp = session.get(f"https://container.googleapis.com/v1/projects/{project}"
                       f"/locations/{location}/clusters/{name}")
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        raise RuntimeError(f"GCP answered HTTP {resp.status_code}")
    body = resp.json()
    return {"status": body.get("status", ""), "message": body.get("statusMessage", ""),
            "nodepools": {p.get("name"): p.get("status", "")
                          for p in body.get("nodePools") or []}}


def oci_instance(ocid: str) -> str:
    """An OCI instance's lifecycle state, or ``""`` when it does not exist."""
    import oci
    from ... import oci_service
    compute = oci.core.ComputeClient(oci_service._oci_config())
    try:
        return compute.get_instance(ocid).data.lifecycle_state or ""
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            return ""
        raise


# ── Azure, through the dashboard's own identity on ARM (async) ────────────────

_AZURE_DB_API = {"Microsoft.DBforPostgreSQL/flexibleServers": "2022-12-01",
                 "Microsoft.DBforMySQL/flexibleServers": "2023-12-30",
                 "Microsoft.Sql/servers": "2021-11-01"}


async def azure_vm(resource_group: str, name: str) -> dict:
    """``{power, provisioning}`` for one VM from its instance view, or ``{}``."""
    from ... import directory_service
    sub = await directory_service._azure_subscription()
    status, body = await directory_service._arm_get(
        f"/subscriptions/{sub}/resourceGroups/{resource_group}/providers/"
        f"Microsoft.Compute/virtualMachines/{name}/instanceView",
        {"api-version": "2024-03-01"})
    if status == 404:
        return {}
    if status != 200:
        raise RuntimeError(f"Azure answered HTTP {status}")
    out = {"power": "", "provisioning": ""}
    for s in body.get("statuses") or []:
        code = s.get("code") or ""
        if code.startswith("PowerState/"):
            out["power"] = code.split("/", 1)[1]
        elif code.startswith("ProvisioningState/"):
            out["provisioning"] = code.split("/", 1)[1]
    return out


async def azure_database(resource_id: str) -> dict:
    """``{state}`` for an Azure database server by its resource id, or ``{}``."""
    from ... import directory_service
    api = next((v for k, v in _AZURE_DB_API.items() if k.lower() in resource_id.lower()),
               None)
    if api is None:
        raise RuntimeError("an Azure database type this check does not know")
    status, body = await directory_service._arm_get(resource_id, {"api-version": api})
    if status == 404:
        return {}
    if status != 200:
        raise RuntimeError(f"Azure answered HTTP {status}")
    return {"state": (body.get("properties") or {}).get("state", "")}


async def azure_clusters() -> dict:
    """``{name: {provisioning, power, pools: {name: provisioning}}}`` for every AKS
    cluster in the dashboard's subscription."""
    from ... import directory_service
    sub = await directory_service._azure_subscription()
    status, body = await directory_service._arm_get(
        f"/subscriptions/{sub}/providers/Microsoft.ContainerService/managedClusters",
        {"api-version": "2024-02-01"})
    if status != 200:
        raise RuntimeError(f"Azure answered HTTP {status}")
    out = {}
    for c in body.get("value") or []:
        props = c.get("properties") or {}
        out[c.get("name")] = {
            "provisioning": props.get("provisioningState", ""),
            "power": (props.get("powerState") or {}).get("code", ""),
            "pools": {p.get("name"): p.get("provisioningState", "")
                      for p in props.get("agentPoolProfiles") or []}}
    return out
