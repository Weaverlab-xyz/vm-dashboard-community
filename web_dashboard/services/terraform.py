"""
Terraform subprocess wrapper.
Manages per-deployment state directories and runs terraform apply/destroy.
Uses asyncio.to_thread() so long-running applies don't block the event loop.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import contextlib
try:
    import fcntl  # POSIX-only; the app runs in Linux containers (absent on Windows dev hosts → locking is a no-op there).
except ImportError:  # pragma: no cover
    fcntl = None
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from ..config import settings

logger = logging.getLogger(__name__)


class TerraformError(Exception):
    """Raised when a Terraform command fails."""


class JobCancelled(Exception):
    """Raised by an ``on_line`` callback to abort a streamed apply/destroy when the
    job was flipped to ``cancelled``. ``_stream`` catches it, terminates the terraform
    subprocess, and re-raises so the caller can finalize the job."""


# Path to the ec2_instance template (relative to this file → ../../terraform/ec2_instance)
_TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "ec2_instance")
)

# Terraform state is stored in the user's ACTIVE storage backend (the same
# bucket/container + creds the /storage system uses), under this prefix and keyed
# per deployment job id, so a container recreate no longer orphans cloud resources.
# See docs/infrastructure-as-code.md#state-the-thing-that-makes-iac-work.
# `local` keeps state in the deploy dir.
_TF_STATE_PREFIX = "terraform-state"


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _state_key(deploy_dir: str) -> str:
    """`terraform-state/<job_id>` — the job id is the deploy-dir basename."""
    job = os.path.basename(os.path.normpath(deploy_dir))
    return f"{_TF_STATE_PREFIX}/{job}"


def _backend_settings(deploy_dir: str):
    """Resolve ``(backend_type, backend_config, backend_env)`` from the user's
    active storage backend. Cloud backends store state in the same bucket/container
    /storage uses, under ``terraform-state/<job_id>/``, authenticated with the same
    credentials. ``local`` (or no configured backend) → state stays in the deploy
    dir. The ``backend_env`` is merged into the terraform subprocess env so state
    access works even cross-cloud (e.g. an S3 state backend while provisioning GCP).
    """
    from . import storage_service
    backend = storage_service.active_backend()
    key = f"{_state_key(deploy_dir)}/terraform.tfstate"

    if backend == "s3":
        cfg = {
            "bucket": _cfg("storage_s3_bucket"),
            "key": key,
            "region": _cfg("storage_s3_region") or _cfg("aws_region") or "us-east-1",
            # S3-native state locking (Terraform >= 1.10, pinned in the Dockerfile)
            # — no DynamoDB table required.
            "use_lockfile": "true",
        }
        # The state backend authenticates separately from the provider, so the dynamic
        # tier has to be honoured here too. Wire only the provider and API calls succeed
        # while `terraform init` fails — a confusing place to land.
        from . import workload_credential_lease as _leases
        env = _leases.aws_subprocess_env() or {}
        if not env:
            ak, sk = _cfg("aws_access_key_id"), _cfg("aws_secret_access_key")
            if ak and sk:
                env = {"AWS_ACCESS_KEY_ID": ak, "AWS_SECRET_ACCESS_KEY": sk}
        return ("s3", cfg, env)

    if backend == "azure_blob":
        cfg = {
            "storage_account_name": _cfg("storage_azure_account"),
            "container_name": _cfg("storage_azure_container") or "playbooks",
            "key": key,
            "use_azuread_auth": "true",
        }
        # Same split-failure risk as the S3 backend above: the state backend
        # authenticates separately from the provider, and `use_azuread_auth` means it
        # authenticates at all. Wire only the provider and `terraform init` fails while
        # API calls succeed.
        from . import workload_credential_lease as _leases
        env = _leases.azure_subprocess_env() or {}
        if not env:
            for ck, ak in (("azure_client_id", "ARM_CLIENT_ID"),
                           ("azure_client_secret", "ARM_CLIENT_SECRET"),
                           ("azure_tenant_id", "ARM_TENANT_ID"),
                           ("azure_subscription_id", "ARM_SUBSCRIPTION_ID")):
                v = _cfg(ck)
                if v:
                    env[ak] = v
        return ("azurerm", cfg, env)

    if backend == "gcs":
        cfg = {"bucket": _cfg("storage_gcs_bucket"), "prefix": _state_key(deploy_dir)}
        env = {}
        creds = _cfg("gcp_service_account_json") or _cfg("gcp_credentials_json")
        if creds:
            env["GOOGLE_CREDENTIALS"] = creds
        return ("gcs", cfg, env)

    return ("local", {}, {})


def _write_backend_tf(deploy_dir: str, backend_type: str) -> None:
    """Write (or clear) ``backend.tf`` selecting the backend type. Values are
    supplied at init via ``-backend-config`` since backend blocks can't take vars."""
    path = os.path.join(deploy_dir, "backend.tf")
    if backend_type == "local":
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w") as fh:
        fh.write('terraform {\n  backend "%s" {}\n}\n' % backend_type)


def _materialize(deploy_dir: str, template_dir: str) -> None:
    """Copy a Terraform module template into deploy_dir. Used by apply, and by destroy
    to rebuild a deploy dir that a container recreate lost (remote state makes that
    destroy recoverable)."""
    if not os.path.isdir(template_dir):
        # The module isn't shipped in the image (e.g. a cloud's k8s_cluster/<cloud>
        # module missing from the Dockerfile COPY). Fail clearly instead of letting
        # os.listdir raise a bare FileNotFoundError mid-apply.
        raise TerraformError(
            f"Terraform module template not found: {template_dir} "
            "(is the module shipped in the image / build context?)"
        )
    os.makedirs(deploy_dir, exist_ok=True)
    for item in os.listdir(template_dir):
        src = os.path.join(template_dir, item)
        dst = os.path.join(deploy_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _run(cmd: list, cwd: str, timeout: int = 600,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run a terraform command synchronously.

    ``env`` entries are merged OVER os.environ rather than replacing it —
    terraform still needs PATH/HOME and, behind a TLS-inspecting proxy,
    SSL_CERT_FILE from the image env.
    """
    full_cmd = [settings.terraform_executable] + cmd
    return subprocess.run(
        full_cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **env} if env else None,
    )


def _init_lock_path() -> str:
    # Just a stable well-known path for the flock; in the image TF_PLUGIN_CACHE_DIR is
    # unset (providers come from the read-only mirror — see plugin_cache_lock) and this
    # falls back to the default, creating an empty directory to hold the lock file.
    cache = os.environ.get("TF_PLUGIN_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".terraform.d", "plugin-cache")
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError:
        cache = "/tmp"
    return os.path.join(cache, ".tf-init.lock")


@contextlib.contextmanager
def plugin_cache_lock():
    """Serialize ``terraform init`` across processes/jobs.

    A shared plugin cache (``TF_PLUGIN_CACHE_DIR``) is explicitly NOT concurrency-safe:
    parallel inits race to (re)place the same provider binary and fail with "text file
    busy" (ETXTBSY). A coarse exclusive file lock around init serializes provider
    placement across gunicorn workers and concurrent jobs. ``flock`` is advisory and
    auto-released if a worker dies. No-op where ``fcntl`` is absent (Windows dev).

    THIS LOCK IS NOT SUFFICIENT ON ITS OWN, and the published image does not rely on it.
    A plugin cache entry is SYMLINKED into ``deploy_dir/.terraform/providers``, so a
    running apply/destroy is *executing* the cached binary; an unrelated init that has to
    reinstall that provider rewrites the file underneath it and gets ETXTBSY. That is
    init-vs-APPLY — serializing inits against each other cannot prevent it (job
    cc8743c3: a clouddb_decommission init killed by a concurrent k8s destroy). The image
    therefore installs providers from a READ-ONLY filesystem mirror instead
    (TF_CLI_CONFIG_FILE=/etc/terraform.tfrc — see the Dockerfile), which nothing writes;
    this lock remains for runs off that image, where init downloads into a cache again.
    Do not re-introduce TF_PLUGIN_CACHE_DIR anywhere: pointed at the mirror it makes
    every init fail with "cannot install existing provider directory ... to itself".

    PUBLIC on purpose: this module is not the only thing that runs ``terraform init``.
    The PRA / Entitle / Password-Safe services each shell out to their own terraform in a
    tempdir — so they take this lock too (see their ``_run_tf``). Anything that adds a
    new ``terraform init`` call site must hold it; ``tests/test_worker_tiers.py`` fails
    the build if one doesn't.
    """
    if fcntl is None:
        yield
        return
    fd = open(_init_lock_path(), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


def _init_args(backend_type: str, backend_config: Optional[dict]) -> list:
    """`terraform init` args; remote backends get -reconfigure + -backend-config."""
    args = ["init", "-no-color", "-input=false", "-upgrade=false"]
    if backend_type != "local":
        args.append("-reconfigure")
        for k, v in (backend_config or {}).items():
            args.append(f"-backend-config={k}={v}")
    return args


def _init_sync(deploy_dir: str, env: Optional[dict] = None,
               backend_type: str = "local", backend_config: Optional[dict] = None) -> None:
    # Providers come from the image's read-only mirror (see plugin_cache_lock), which is
    # what keeps this init offline — NOT the template, which ships main.tf only and has
    # no pre-initialised .terraform to copy. The remote backend init still reaches the
    # state store (that is the point).
    _write_backend_tf(deploy_dir, backend_type)
    with plugin_cache_lock():
        r = _run(_init_args(backend_type, backend_config), deploy_dir, timeout=300, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform init failed:\n{r.stderr}")


def _apply_sync(deploy_dir: str, var_args: list, env: Optional[dict] = None) -> dict:
    """Run terraform apply and return parsed outputs."""
    apply_args = ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args
    r = _run(apply_args, deploy_dir, timeout=600, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform apply failed:\n{r.stderr}\n{r.stdout}")

    # Parse outputs. Pass env here too: `terraform output` re-instantiates the
    # providers, and azurerm rebuilds its ARM config at that point — without the
    # ARM_* Service Principal vars it falls back to the `az` CLI (absent in the
    # container) and fails. (AWS/GCP don't authenticate on output, so this only
    # bit Azure.)
    out_r = _run(["output", "-json"], deploy_dir, timeout=30, env=env)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    raw = json.loads(out_r.stdout)
    return {k: v["value"] for k, v in raw.items()}


def _destroy_args(var_args: Optional[list] = None, refresh: bool = True) -> list:
    """`terraform destroy` args; ``refresh=False`` skips the pre-destroy refresh
    (see :data:`_REFRESH_WEDGE_MARKERS`)."""
    cmd = ["destroy", "-auto-approve", "-no-color", "-input=false"]
    if not refresh:
        cmd.append("-refresh=false")
    return cmd + (var_args or [])


def _destroy_sync(deploy_dir: str, env: Optional[dict] = None,
                  var_args: Optional[list] = None, refresh: bool = True) -> None:
    r = _run(_destroy_args(var_args, refresh), deploy_dir, timeout=600, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform destroy failed:\n{r.stderr}\n{r.stdout}")


# ── Destroy-time refresh wedges ───────────────────────────────────────────────
# `terraform destroy` REFRESHES before it plans, so a provider read that can never
# succeed aborts the whole run before anything is deleted — and every retry burns
# the same failure, so the resources stay orphaned forever.
#
# The one hit in the wild is the google provider's GKE resume-on-read: it stores
# the in-flight cluster operation in state and every later READ resumes waiting on
# it (resource_container_cluster.go → ContainerOperationWait "resuming GKE
# cluster", read timeout 90m). A cluster whose CREATE died — e.g. the pinned zone
# ran out of capacity for the default pool's first node, so the create operation
# ended in GCE_STOCKOUT ~35 min in — then fails EVERY teardown with
#   Error waiting for resuming GKE cluster: … [GCE_STOCKOUT] … Expected 1, running 0
# leaving the cluster plus its VPC/router/NAT/address behind.
#
# STATE (not the refresh) decides what gets destroyed, so retrying once with
# -refresh=false is both safe and sufficient: the provider skips the read and goes
# straight to Delete, whose own wait treats ERROR/DEGRADED as a resting state and
# tolerates a 404 for anything already gone out-of-band. terraform_pra_service /
# ps_resource_service already destroy with -refresh=false unconditionally for the
# same "the provider errors on refresh" reason; here it stays a fallback so a
# normal destroy keeps the drift detection a refresh gives us.
_REFRESH_WEDGE_MARKERS = (
    "error waiting for resuming gke cluster",
)


def _is_refresh_wedge(output: str) -> bool:
    """True when a failed destroy looks like a doomed *refresh* rather than a real
    delete failure — worth exactly one retry with ``-refresh=false``."""
    low = (output or "").lower()
    return any(m in low for m in _REFRESH_WEDGE_MARKERS)


def _import_sync(deploy_dir: str, address: str, resource_id: str,
                 var_args: Optional[list] = None, env: Optional[dict] = None) -> None:
    cmd = ["import", "-no-color", "-input=false"] + (var_args or []) + [address, resource_id]
    r = _run(cmd, deploy_dir, timeout=300, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform import failed:\n{r.stderr}\n{r.stdout}")


def _build_var_args(variables: dict) -> list:
    """Convert a variables dict to a list of -var flags for the CLI."""
    args = []
    for k, v in variables.items():
        if isinstance(v, (list, dict, bool)) or v is None:
            # Non-string values must be HCL expressions, and JSON is valid HCL:
            #   -var 'security_group_ids=["sg-xxx"]'  -var 'tags={"team":"se"}'
            #   -var 'multi_az=true'
            # str(dict)/str(bool) would produce Python syntax ({'k': 'v'}, True),
            # which terraform rejects ("Single quotes are not valid").
            encoded = json.dumps(v)
            args += ["-var", f"{k}={encoded}"]
        else:
            args += ["-var", f"{k}={v}"]
    return args


# -- Stale state locks left by a cancelled run --------------------------------
# Cancelling a job kills the terraform subprocess (see :func:`_stream`), and a
# killed terraform never releases its state lock. The lock outlives the job
# forever, so every later run against that state -- including the DESTROY that
# would clean the cancelled deployment up -- dies with "Error acquiring the state
# lock", and the resources are orphaned with no in-app way out. Live case: a
# cancelled clouddb_provision left the GCS default.tflock held by the very worker
# that killed it; the follow-up decommission could not destroy, and the lock
# object had to be deleted by hand.
#
# So the cancel path releases the lock it just orphaned. This is deliberately the
# NARROW half of the problem: it only ever breaks a lock this process can PROVE is
# dead, because it killed the holder itself. Locks stranded any other way (worker
# OOM-killed, replica rolled mid-apply) are left alone -- nothing here can prove
# those holders are gone, and that case wants a deliberate operator action, which
# is what `terraform force-unlock` is for.
#
# Two conditions must BOTH hold before we unlock, and any of them failing -- or
# anything at all going wrong -- means we simply do not:
#   Who      matches this container's own "<user>@<hostname>", so we can never
#            break a lock held by another replica.
#   Created  is at or after the moment we spawned the subprocess, so we can never
#            break a lock that a DIFFERENT, still-live terraform on this same
#            container took in this same deploy dir before we started.

# `force-unlock` needs the lock's ID, and the backend-agnostic way to learn it is
# to offer an ID that cannot possibly match: terraform then fails the unlock and
# renders the CURRENT lock's info (statemgr.LockError -> LockInfo.String()), the
# same "Lock Info:" block an operator sees in a failed job.
#
# Ask terraform rather than reading the .tflock object, because the ID in that
# object is NOT always the ID force-unlock wants: the GCS backend replaces it with
# the lock object's GENERATION number. The live incident showed both -- the object
# held "ffa69909-e728-a06e-1556-af7e1852acb7" while the error (and the only value
# that would have unlocked it) read "1787928588291305". Parsing terraform's own
# output is the only thing that stays correct across backends.
#
# That output only appears if the backend gets far enough to READ the lock, which
# makes the sentinel's format load-bearing rather than cosmetic: it has to satisfy
# every backend's id FORMAT while still matching no real lock. "0" is the only
# value that does both:
#
#   gcs     — the lock id is the .tflock object's GENERATION, so the backend runs
#             strconv.ParseInt BEFORE it reads anything and rejects a non-numeric id
#             with a bare "Lock ID should be numerical value" and NO Lock Info block.
#             A word-shaped sentinel therefore made every GCS lock read as "no lock
#             held" — silently disabling both the cancel-path release below and the
#             operator force-unlock panel. Generation 0 does not exist (GCS assigns
#             positive int64s), and the conditional delete is refused CLIENT-side
#             ("storage: Delete: empty conditions") before any API call, so the probe
#             cannot break the lock it is reading.
#   s3      — id is compared as an opaque string; "0" mismatches the stored UUID and
#   azurerm   the backend returns its LockError with Info attached, same as before.
#
# Anything that changes this value must stay numeric; see the guard test in
# tests/test_terraform_cancel_lock_release.py.
_LOCK_PROBE_ID = "0"

_LOCK_FIELD_RE = re.compile(r"^\s*(ID|Path|Operation|Who|Version|Created):\s*(.+?)\s*$", re.M)

# terraform renders Created with Go's default time.Time layout
# ("2026-08-28 14:49:48.17098413 +0000 UTC") -- not RFC3339, and with up to nine
# fractional digits where datetime accepts at most six. Match a prefix and
# truncate the fraction; the trailing zone NAME is ignored (the offset is what
# matters). The tflock OBJECT stores RFC3339 ("...T14:49:48.17098413Z"), so both
# separators and both zone spellings are accepted.
_LOCK_TIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def _parse_lock_info(text: str) -> dict:
    """Pull terraform's ``Lock Info:`` block out of command output.

    ``{}`` when there is no such block -- which is equally what "no lock is held"
    and "this backend does not lock" look like, and callers treat all of them as
    nothing to release."""
    at = (text or "").find("Lock Info:")
    if at < 0:
        return {}
    # FIRST value wins per field. A job's error text can carry more than one block
    # (the error itself, then the same failure echoed in the captured output), and
    # dict() over findall would take the LAST -- blending two blocks into a lock that
    # never existed if they ever differ. The first block is the one "Lock Info:"
    # introduces, which is the one the operator is looking at.
    out: dict = {}
    for key, value in _LOCK_FIELD_RE.findall(text[at:]):
        out.setdefault(key, value)
    return out


# Text that means "the backend looked and there is genuinely no lock object",
# as opposed to "the probe could not reach the lock at all". Only the first may be
# reported as unlocked -- see :func:`_classify_lock_probe`.
_NO_LOCK_MARKERS = (
    "object doesn't exist",       # gcs: lockInfo() got storage.ErrObjectNotExist
    "object does not exist",
    "nosuchkey",                  # s3
    "status code: 404",
    "blobnotfound",               # azurerm
    "lock already broken",
)


def _classify_lock_probe(text: str):
    """``("locked", info)`` | ``("unlocked", {})`` | ``("unknown", {})``.

    The third case is the point. A probe whose output carries no ``Lock Info:``
    block is NOT evidence that nothing is locked -- a 403 on the state bucket, a
    TLS failure through the corp proxy, an uninitialised dir and a backend that
    rejected the sentinel's FORMAT all look identical to "no lock held". Reporting
    any of them as unlocked tells an operator the wedge cleared itself and to go
    retry the run that just failed, which is the one thing guaranteed not to work.
    """
    info = _parse_lock_info(text)
    if info.get("ID"):
        return "locked", info
    low = (text or "").lower()
    if any(m in low for m in _NO_LOCK_MARKERS):
        return "unlocked", {}
    return "unknown", {}


def _parse_lock_time(value: str):
    """Parse a lock ``Created`` stamp to an aware datetime; ``None`` if unparseable
    (which is a REFUSAL to unlock, not a pass -- see :func:`_release_own_lock_sync`)."""
    m = _LOCK_TIME_RE.match((value or "").strip())
    if not m:
        return None
    frac = (m.group("frac") or "").ljust(6, "0")[:6]
    tz = m.group("tz") or "Z"
    if tz == "Z":
        tz = "+00:00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        return datetime.fromisoformat(f"{m.group('date')}T{m.group('time')}.{frac}{tz}")
    except ValueError:  # pragma: no cover -- regex already constrains the shape
        return None


def _self_lock_owner() -> str:
    """This process's terraform lock identity. terraform builds ``Who`` as
    ``user.Current().Username + "@" + os.Hostname()``; in the container that is
    ``root@<replica>``, which is what we match against."""
    try:
        import pwd  # POSIX-only, like fcntl above
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # pragma: no cover -- non-POSIX dev host
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return f"{user}@{socket.gethostname()}"


def _release_own_lock_sync(deploy_dir: str, env: Optional[dict], started_at) -> str:
    """Release a state lock this process orphaned by killing its own terraform.

    Returns a short status string for the log. NEVER raises and never re-raises:
    the caller is already unwinding a cancel, and every failure mode here just
    leaves the lock exactly where a cancel used to leave it anyway."""
    try:
        probe = _run(["force-unlock", "-force", _LOCK_PROBE_ID], deploy_dir,
                     timeout=60, env=env)
        verdict, info = _classify_lock_probe(
            (probe.stdout or "") + "\n" + (probe.stderr or ""))
        if verdict == "unlocked":
            return "none held"
        if verdict == "unknown":
            # Never "none held": that reads as "nothing was orphaned" in the cancel
            # log and hides a lock this process may well still be holding.
            return ("could not read the lock: "
                    + ((probe.stderr or probe.stdout or "").strip()[:200] or "no output"))
        lock_id = info.get("ID", "")
        if not lock_id:
            return "held, but terraform reported no lock id"
        who, mine = info.get("Who", ""), _self_lock_owner()
        if who != mine:
            return f"left alone - held by {who!r}, not by us ({mine!r})"
        created = _parse_lock_time(info.get("Created", ""))
        if created is None:
            return f"left alone - unparseable Created {info.get('Created', '')!r}"
        if created < started_at:
            return (f"left alone - taken {created.isoformat()}, before this run started "
                    f"{started_at.isoformat()} (another terraform on this host holds it)")
        r = _run(["force-unlock", "-force", lock_id], deploy_dir, timeout=60, env=env)
        if r.returncode != 0:
            return f"force-unlock {lock_id} failed: {(r.stderr or r.stdout).strip()[:300]}"
        return f"released {lock_id}"
    except Exception as exc:
        return f"release failed: {exc}"


# -- Operator-driven force-unlock ---------------------------------------------
# The half the cancel path above deliberately refuses. A lock stranded any OTHER way
# -- worker OOM-killed, replica rolled mid-apply, container recreated -- is held by a
# process nothing in here can prove is dead, so breaking it is an operator's decision,
# taken against the lock's own Who/Created. That is what `terraform force-unlock` is
# for, and this is the in-app equivalent so the remedy is not "delete the .tflock
# object out of the bucket by hand".
#
# Two properties keep it narrow, and both live at the API layer's call site:
#   - The state to act on is READ OUT OF THE FAILED JOB'S OWN ERROR (see
#     :func:`reported_lock`), never supplied by the caller. An operator can only
#     break a lock a job actually complained about.
#   - The unlock is optimistic: it re-reads the lock and refuses unless the id still
#     matches the one the operator was shown, so a run that grabbed the lock between
#     the page load and the click is never the one that gets broken.

# A job id path segment, as it appears in `terraform-state/<job_id>/…`. Anchored and
# strict because it is interpolated into a filesystem path below: this comes out of a
# terraform error message, which is not a trusted source of path components.
_STATE_JOB_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
_STATE_PATH_RE = re.compile(
    re.escape(_TF_STATE_PREFIX) + r"/(?P<job>[0-9a-fA-F][0-9a-fA-F-]{7,63})/")


def reported_lock(text: str) -> dict:
    """The lock a failed run complained about: its ``Lock Info`` fields plus the
    ``state_job_id`` its Path names. ``{}`` when the text reports no lock.

    This is the only thing that decides WHICH state an operator unlock may touch, so
    it deliberately reads the state key out of the lock's own Path rather than
    assuming the failing job's id. Those differ in the common case: a decommission
    destroys in the PROVISION job's deploy dir, so its lock lives under the provision
    job's state key while the failure is recorded against the decommission job."""
    info = _parse_lock_info(text)
    if not info:
        return {}
    m = _STATE_PATH_RE.search(info.get("Path", ""))
    if not m:
        return {}
    return {**info, "state_job_id": m.group("job")}


@contextlib.contextmanager
def _lock_workdir(state_job_id: str):
    """A throwaway dir NAMED for the deployment, so :func:`_backend_settings` resolves
    the same state key that deployment uses.

    Only ``backend.tf`` goes in it: `terraform init` against a bare backend block is
    enough to reach the lock, so breaking one needs neither the module (which may not
    even be shipped in this image) nor any provider credentials -- only the backend's."""
    if not _STATE_JOB_ID_RE.match(state_job_id or ""):
        raise TerraformError(f"Refusing to act on state key {state_job_id!r}: "
                             "not a job id.")
    root = tempfile.mkdtemp(prefix="tf-unlock-")
    try:
        work = os.path.join(root, state_job_id)
        os.makedirs(work)
        yield work
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _read_lock_sync(work: str, backend_env: Optional[dict]) -> dict:
    """Probe an initialised dir for the lock its backend currently holds.

    ``{}`` means the backend confirmed there is no lock. Raises
    :class:`TerraformError` when the probe could not determine that either way, so
    callers surface "could not read the lock" instead of inventing "not locked".
    """
    probe = _run(["force-unlock", "-force", _LOCK_PROBE_ID], work, timeout=60,
                 env=backend_env)
    text = (probe.stdout or "") + "\n" + (probe.stderr or "")
    verdict, info = _classify_lock_probe(text)
    if verdict == "locked":
        return info
    if verdict == "unlocked":
        return {}
    raise TerraformError(
        "Could not determine whether this state is locked -- terraform neither "
        "reported a lock nor confirmed the absence of one:\n" + text.strip()[:600])


_LOCAL_BACKEND_DETAIL = (
    "The active storage backend is local, so state and its lock live in the deploy "
    "directory and the operating system drops that lock when the process dies. There "
    "is no remote lock to break."
)


def _inspect_state_lock_sync(state_job_id: str) -> dict:
    """Report the lock currently held on ``state_job_id``'s state."""
    with _lock_workdir(state_job_id) as work:
        backend_type, backend_config, backend_env = _backend_settings(work)
        if backend_type == "local":
            return {"backend": "local", "supported": False, "locked": False,
                    "info": {}, "detail": _LOCAL_BACKEND_DETAIL}
        _init_sync(work, backend_env, backend_type, backend_config)
        info = _read_lock_sync(work, backend_env)
        return {"backend": backend_type, "supported": True,
                "locked": bool(info.get("ID")), "info": info, "detail": ""}


def _force_unlock_state_sync(state_job_id: str, expected_id: str) -> dict:
    """Break the lock on ``state_job_id``'s state, but only if it is still the lock
    the caller was shown. Raises :class:`TerraformError` on every refusal."""
    with _lock_workdir(state_job_id) as work:
        backend_type, backend_config, backend_env = _backend_settings(work)
        if backend_type == "local":
            raise TerraformError(_LOCAL_BACKEND_DETAIL)
        _init_sync(work, backend_env, backend_type, backend_config)
        # Re-read rather than trusting what the page was rendered from. The gap
        # between an operator loading the panel and clicking the button is unbounded,
        # and a legitimate run can take the lock inside it.
        info = _read_lock_sync(work, backend_env)
        lock_id = info.get("ID", "")
        if not lock_id:
            raise TerraformError(
                "No lock is held on this state any more — nothing to break. "
                "Whatever held it released it; retry the operation that failed.")
        if lock_id != expected_id:
            raise TerraformError(
                f"This is no longer the lock you were shown (it is now {lock_id}, you "
                f"confirmed {expected_id}), so it was NOT broken — a new run has taken "
                "the lock since the page loaded. Re-read the lock and decide again.")
        r = _run(["force-unlock", "-force", lock_id], work, timeout=60, env=backend_env)
        if r.returncode != 0:
            raise TerraformError(
                f"terraform force-unlock failed:\n{(r.stderr or r.stdout).strip()}")
        return {"unlocked": True, "lock_id": lock_id, "info": info,
                "backend": backend_type}


async def inspect_state_lock(state_job_id: str) -> dict:
    """Async wrapper for :func:`_inspect_state_lock_sync`."""
    return await asyncio.to_thread(_inspect_state_lock_sync, state_job_id)


async def force_unlock_state(state_job_id: str, expected_id: str) -> dict:
    """Async wrapper for :func:`_force_unlock_state_sync`."""
    return await asyncio.to_thread(_force_unlock_state_sync, state_job_id, expected_id)


async def _stream(tf_args: list, cwd: str, env: Optional[dict],
                  on_line: Callable[[str], Awaitable[None]]) -> tuple[int, str]:
    """Run a terraform subcommand, streaming each stdout line to the async
    ``on_line`` callback (stderr merged into stdout). Returns (returncode,
    full_output). Mirrors packer_service._stream_command; merges env OVER
    os.environ like :func:`_run` so PATH / SSL_CERT_FILE survive."""
    # Stamped BEFORE the spawn: any lock our terraform takes is necessarily created
    # after this, which is how the cancel path below tells our own lock apart from
    # one a different terraform on this host already held (see _release_own_lock_sync).
    started_at = datetime.now(timezone.utc)
    proc = await asyncio.create_subprocess_exec(
        settings.terraform_executable, *tf_args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
    )
    lines: list = []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        try:
            await on_line(line)
        except JobCancelled:
            # Cooperative cancel: the job was flipped to 'cancelled'. Stop terraform
            # and re-raise so the caller finalizes the job.
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                # Reap it: the lock release below is only sound once the holder is
                # provably gone, and a SIGKILLed process cannot outlive this wait.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=10)
            # The terraform we just killed never released its state lock, and that
            # lock would otherwise wedge every later run against this state --
            # including the destroy that cleans this deployment up. Break the one we
            # orphaned, and only that one. Log-only on purpose: on_line just raised
            # JobCancelled, so it cannot carry this to the Live Output.
            try:
                status = await asyncio.to_thread(
                    _release_own_lock_sync, cwd, env, started_at)
                logger.info("terraform cancel in %s: state lock %s", cwd, status)
            except Exception as exc:  # pragma: no cover -- defence in depth
                logger.warning("terraform cancel in %s: releasing the state lock "
                               "failed: %s", cwd, exc)
            raise
        except Exception:
            pass  # a UI-broadcast hiccup must never abort the terraform run
    await proc.wait()
    return proc.returncode, "\n".join(lines)


# ── Public async API ──────────────────────────────────────────────────────────

async def apply(deploy_dir: str, variables: dict, template_dir: Optional[str] = None,
                env: Optional[dict] = None,
                on_line: Optional[Callable[[str], Awaitable[None]]] = None) -> dict:
    """
    Copy a Terraform template into deploy_dir, init, and apply. Returns a dict
    of the module's Terraform outputs.

    ``template_dir`` selects the module (defaults to the EC2 instance template
    for back-compat); the cloud-database service passes ``terraform/db_<engine>``.
    ``env`` is merged over the process environment for the terraform subprocess —
    callers use it to inject provider credentials (e.g. AWS_ACCESS_KEY_ID) the
    same way the packer flow does.
    deploy_dir should be unique per deployment (e.g. based on job_id).

    Templates are plain HCL — they are NOT pre-initialised, so every deploy dir runs its
    own `terraform init`. What keeps that init offline is the image's read-only provider
    mirror (see :func:`plugin_cache_lock`), not anything copied from the template.
    """
    src_template = template_dir or _TEMPLATE_DIR
    _materialize(deploy_dir, src_template)

    var_args = _build_var_args(variables)

    # State goes to the user's active storage backend; merge its creds OVER the
    # caller's provider env so both the backend and provider authenticate (they
    # can differ, e.g. an S3 state backend while provisioning GCP).
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}

    # No streaming callback → preserve the exact existing (non-streamed) path.
    if on_line is None:
        await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
        return await asyncio.to_thread(_apply_sync, deploy_dir, var_args, merged_env)

    # Streaming path: stream the apply (the long, interesting part) line-by-line to
    # on_line (e.g. the job's Live Output). Init runs first via the serialized,
    # non-streamed _init_sync — the shared plugin cache isn't concurrency-safe
    # (see plugin_cache_lock) and init output is brief. Outputs are still captured
    # via the post-apply `output -json` (parsing them out of the live stream is fragile).
    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    rc, out = await _stream(
        ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args,
        deploy_dir, merged_env, on_line)
    if rc != 0:
        raise TerraformError(f"terraform apply failed:\n{out}")
    out_r = await asyncio.to_thread(_run, ["output", "-json"], deploy_dir, 30, merged_env)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    return {k: v["value"] for k, v in json.loads(out_r.stdout).items()}


async def destroy(deploy_dir: str, env: Optional[dict] = None,
                  template_dir: Optional[str] = None,
                  variables: Optional[dict] = None,
                  on_line: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
    """
    Run terraform destroy for a deployment. State lives in the user's active
    storage backend (remote), so destroy works even if the local deploy dir was
    lost to a container recreate: pass ``template_dir`` and the module is rebuilt
    from it, the remote backend re-init pulls the state, and destroy proceeds.
    ``env`` carries provider credentials, same as :func:`apply`.

    ``variables`` must be the same -var set apply used: ``terraform destroy``
    evaluates the module config and errors on any required variable that isn't
    set ("No value for required variable"). The values don't change *what* is
    destroyed (resources come from state), but provider-config vars (e.g. the
    google provider's project/region) must be correct, so callers reconstruct
    the full set rather than passing placeholders.

    A destroy that fails on a refresh which can never succeed (see
    :data:`_REFRESH_WEDGE_MARKERS`) is retried once with ``-refresh=false`` —
    otherwise the teardown is wedged permanently and the resources are orphaned.
    """
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}
    var_args = _build_var_args(variables) if variables else []

    # Rebuild the module if the deploy dir was lost (only possible with a remote
    # backend — a local backend's state lived in that dir and is gone with it).
    if not os.path.exists(os.path.join(deploy_dir, "main.tf")):
        if template_dir and os.path.isdir(template_dir) and backend_type != "local":
            _materialize(deploy_dir, template_dir)
        elif not os.path.isdir(deploy_dir):
            raise TerraformError(f"Deployment directory not found: {deploy_dir}")
        elif backend_type == "local":
            raise TerraformError(
                f"No Terraform module/state in {deploy_dir} and backend is local — "
                "cannot destroy; the resource may need manual termination."
            )
        else:
            # main.tf is gone and we can't rebuild it: template_dir is missing/invalid
            # (e.g. a cloud's module isn't shipped in the image). Running `terraform
            # destroy` in the empty dir would emit confusing "Value for undeclared
            # variable" errors for every -var, so fail clearly instead.
            raise TerraformError(
                f"Cannot destroy {deploy_dir}: no main.tf and the module template "
                f"{template_dir!r} is unavailable (not shipped in the image?). "
                "Re-add the module + rebuild, then retry the teardown."
            )

    if on_line is None:
        await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
        try:
            await asyncio.to_thread(_destroy_sync, deploy_dir, merged_env, var_args)
        except TerraformError as exc:
            if not _is_refresh_wedge(str(exc)):
                raise
            logger.warning("terraform destroy in %s failed on an unrecoverable refresh "
                           "— retrying with -refresh=false: %s", deploy_dir, exc)
            await asyncio.to_thread(_destroy_sync, deploy_dir, merged_env, var_args,
                                    refresh=False)
        return

    # Streaming path (mirrors apply): serialized non-streamed init, then stream destroy.
    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    rc, out = await _stream(_destroy_args(var_args), deploy_dir, merged_env, on_line)
    if rc != 0 and _is_refresh_wedge(out):
        note = ("destroy failed on a refresh that can never succeed (a stale in-flight "
                "cloud operation in state) — retrying with -refresh=false")
        logger.warning("terraform destroy in %s: %s", deploy_dir, note)
        try:
            await on_line(f"[dashboard] {note}")
        except JobCancelled:
            raise  # the operator cancelled: don't start a second destroy
        except Exception:
            pass   # a UI-broadcast hiccup must not block the retry
        rc, retry_out = await _stream(
            _destroy_args(var_args, refresh=False), deploy_dir, merged_env, on_line)
        out = f"{out}\n{retry_out}"
    if rc != 0:
        raise TerraformError(f"terraform destroy failed:\n{out}")


async def import_resource(deploy_dir: str, address: str, resource_id: str,
                          env: Optional[dict] = None,
                          template_dir: Optional[str] = None,
                          variables: Optional[dict] = None) -> None:
    """Adopt an already-created cloud resource into this deployment's state via
    ``terraform import <address> <resource_id>``.

    Used to recover from a provider that dropped a resource from state on a
    transient create-wait error even though the cloud finished creating it (the
    GCP Cloud SQL create-wait bug): after the import, re-applying converges the
    rest of the module instead of failing on a name collision. Init + module-
    rebuild logic mirrors :func:`destroy`; ``variables`` must be the module's -var
    set because ``import`` evaluates the provider config (same reason destroy needs
    it). Non-streamed — import output is brief and not user-interesting.
    """
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}
    var_args = _build_var_args(variables) if variables else []

    # Rebuild the module if the deploy dir was lost (only possible with a remote
    # backend); mirrors destroy. The common caller (a failed apply retrying) still
    # has the materialized dir, so this is just a safety net.
    if not os.path.exists(os.path.join(deploy_dir, "main.tf")):
        if template_dir and os.path.isdir(template_dir) and backend_type != "local":
            _materialize(deploy_dir, template_dir)
        else:
            raise TerraformError(
                f"Cannot import into {deploy_dir}: no main.tf and the module template "
                f"{template_dir!r} is unavailable or the backend is local.")

    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    await asyncio.to_thread(_import_sync, deploy_dir, address, resource_id, var_args, merged_env)
