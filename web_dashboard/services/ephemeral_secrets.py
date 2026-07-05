"""Pure helpers for ephemeral cloud secrets (managed-account checkout on the
ECS / Cloud Run runners). Stdlib-only so naming + GC logic is unit-testable by
file path, mirroring services/cloud_ansible_secrets.py and managed_accounts.py.

The credential I/O (write/lock-down/delete in AWS SM / GCP SM) lives in
services/secrets_backend_service; the run wiring lives in api/config_mgmt. This
module only builds the deterministic names and answers "which ephemerals are
expired" for the garbage-collector.
"""
import re

# Every ephemeral secret carries this so the GC sweeper (and a human) can find
# them, and so a store-side filter is cheap. AWS: a name prefix + a resource tag;
# GCP: a name prefix + a label.
NAME_PREFIX = "dash-ephemeral/ansible"          # AWS secret name prefix
GCP_ID_PREFIX = "dash-ephemeral-ansible"        # GCP secret id prefix (no "/")
TAG_KEY = "vm-dashboard-ephemeral"              # AWS tag key / GCP label key
TAG_VALUE = "ansible-managed-account"

_SANITIZE = re.compile(r"[^a-zA-Z0-9-]")


def aws_secret_name(job_id: str, idx: int) -> str:
    """Deterministic AWS Secrets Manager name for the idx-th ephemeral of a job."""
    return f"{NAME_PREFIX}-{_SANITIZE.sub('-', job_id)}-{idx}"


def gcp_secret_id(job_id: str, idx: int) -> str:
    """Deterministic GCP Secret Manager id (letters/digits/hyphen only)."""
    return f"{GCP_ID_PREFIX}-{_SANITIZE.sub('-', job_id)}-{idx}"


def expired(items: list, ttl_min: int, now_ts: float) -> list:
    """Given ``items`` = ``[{"id": <name/id>, "created_ts": <epoch seconds>}]``,
    return the ids older than ``ttl_min`` minutes — the GC delete set. Items with
    no/zero ``created_ts`` are treated as expired (better to reap an unknown-age
    ephemeral than leak it). ttl_min <= 0 disables (returns [])."""
    if ttl_min <= 0:
        return []
    cutoff = now_ts - ttl_min * 60
    out = []
    for it in items or []:
        created = it.get("created_ts") or 0
        if created <= cutoff:
            out.append(it.get("id"))
    return [i for i in out if i]
