"""Job metadata for a VM (`ansible_local`) Config-Management run.

These runs used to execute as a FastAPI ``BackgroundTask`` with their parameters
held only in the task closure, so nothing survived a worker restart — a bulk run
that queued 50 of them left the un-started ones `pending` forever, because
``reconcile_stale_jobs`` deliberately ignores `pending` and no job runner claimed
`ansible_local`. Persisting the parameters is what lets the durable runner pick a
run up, which is the whole point of this module.

**Only refs, ids and non-secret values are ever written here.** Job metadata lands in
the database, so this is the boundary that has to hold:

  * ``secret_vars`` maps a var name to a *source ref* (a config-secret key or a
    ``bt_safe://`` vault ref), never a value — the value is resolved at run time;
  * ``secret_become_source`` / ``secret_ssh_key_source`` are the same kind of ref;
  * ``managed_account`` / ``managed_become`` carry Password Safe ids plus the account
    name and its SSH-key flag — the model that produces them is explicit that it
    "Never carries a credential";
  * ``extra_vars`` is operator-supplied and already persisted this way by the
    cloud-database / Kubernetes path (``api.config_mgmt._run_cloud_localhost``).

:data:`RUN_META_KEYS` is a **closed allowlist**, asserted by the tests, so a field
added later can't quietly carry a credential into the database.

Pure and stdlib-only, so the round-trip is testable without FastAPI or a database.
"""

# Everything an ansible_local run needs to be reconstructed, and nothing else.
RUN_META_KEYS = (
    "asset",
    "asset_backend",
    "target",
    "cloud",
    "ansible_user",
    "extra_vars",
    "secret_vars",
    "secret_become_source",
    "secret_ssh_key_source",
    "managed_account",
    "managed_become",
    # The NAME of the var an EPM-L installation token is bound to, never the token.
    # The token is minted at run time and only ever exists in the run's scrubbed
    # secret channel — see ansible_local_run_service.
    "epml_token_var",
)

# Defaults matching _run_job's own signature, so a run reconstructed from metadata
# written by an older build behaves exactly like one that never had the field.
_DEFAULTS = {
    "asset": "",
    "asset_backend": "",
    "target": "",
    "cloud": "",
    "ansible_user": "",
    # `{}` not None: _run_job declares extra_vars as a required dict and passes it
    # to config_drift.inputs_hash unguarded, so None would break the drift record.
    "extra_vars": {},
    "secret_vars": None,
    "secret_become_source": "",
    "secret_ssh_key_source": "",
    "managed_account": None,
    "managed_become": None,
    "epml_token_var": "",
}


def _plain(value):
    """A pydantic sub-model (ManagedAccountRef) as a plain dict, so the metadata is
    JSON-serializable. Anything already plain passes through."""
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value


def run_meta(payload, *, description: str, asset_backend: str) -> dict:
    """Job metadata for an ansible_local run, from the request payload.

    ``payload`` is read with ``getattr``, so a RunRequest and a plain object behave
    the same — the tests use the latter. ``asset_backend`` is passed separately
    because the endpoint resolves it (falling back to the active storage backend)
    before the job is created."""
    meta = {"description": description}
    for key in RUN_META_KEYS:
        meta[key] = _plain(getattr(payload, key, _DEFAULTS[key]))
    meta["asset_backend"] = asset_backend
    return meta


def run_kwargs(meta: dict) -> dict:
    """``_run_job`` keyword arguments reconstructed from job metadata.

    Missing keys fall back to :data:`_DEFAULTS` rather than raising: a job queued by
    an older build predates some of these, and failing to resume it would be worse
    than resuming it the way that build would have."""
    meta = meta or {}
    out = {}
    for key in RUN_META_KEYS:
        value = meta.get(key, _DEFAULTS[key])
        # Never hand back the shared default object itself — a caller that mutated it
        # would poison every later reconstruction.
        out[key] = dict(value) if value is _DEFAULTS[key] and isinstance(value, dict) else value
    return out
