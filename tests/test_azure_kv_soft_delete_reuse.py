"""A soft-deleted Key Vault secret holds its own name, and the write has to heal it.

Pairing a Portainer adapter on Azure after a previous adapter had been retired failed
at the staging step with:

    could not stage the Portainer API token in the azure_kv secret store ...
    (Conflict) Secret portainer-adapter-pat is currently in a deleted but recoverable
    state, and its name cannot be reused; in this state, the secret can only be
    recovered or purged.  Inner error: {"code": "ObjectIsDeletedButRecoverable"}

Nothing about the vault or the freshly minted token was wrong. Soft delete is
mandatory on every vault created since 2020, so the retire's `begin_delete_secret`
left a tombstone squatting on `portainer-adapter-pat` for the vault's retention
window, and `_stage_pat_secret` writes exactly that one fixed name. The AWS twin
force-deletes and GCP's delete is permanent, so Azure was the only backend where
retire + re-pair could not be done twice.

Pinned here:
  * the tombstone conflict is recovered and re-written, not surfaced
  * a conflict that is NOT the tombstone still propagates
  * when recovery itself is refused, the message names the permission and the
    command — the job detail view shows error_message and nothing else
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

CONF = {}

def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, as the sibling suites do: gating on whether azure-keyvault-
# secrets happens to be installed is how a suite passes locally and fails in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace(gcp_project_id=""))

from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402


def _read(path):
    return open(path, encoding="utf-8").read()


_CONFLICT = (
    "(Conflict) Secret portainer-adapter-pat is currently in a deleted but "
    "recoverable state, and its name cannot be reused; in this state, the secret "
    "can only be recovered or purged.\nCode: Conflict\n"
    'Inner error: {\n    "code": "ObjectIsDeletedButRecoverable"\n}'
)


class _Poller:
    def __init__(self, on_wait=None):
        self._on_wait = on_wait

    def wait(self):
        if self._on_wait:
            self._on_wait()


class _FakeClient:
    """A SecretClient that refuses the first write the way a real tombstone does."""

    def __init__(self, *, conflict=_CONFLICT, recover_error=None, conflicts=1):
        self.conflict = conflict
        self.recover_error = recover_error
        self.remaining_conflicts = conflicts
        self.writes = []
        self.recovered = []

    def set_secret(self, name, value):
        if self.remaining_conflicts > 0:
            self.remaining_conflicts -= 1
            raise RuntimeError(self.conflict)
        self.writes.append((name, value))

    def begin_recover_deleted_secret(self, name):
        if self.recover_error:
            raise RuntimeError(self.recover_error)
        self.recovered.append(name)
        return _Poller()


def _install(client):
    sbs._azure_kv_client = lambda: (client, "https://kv-demo.vault.azure.net")


_REAL_CLIENT = sbs._azure_kv_client


def _restore():
    sbs._azure_kv_client = _REAL_CLIENT


# -- The heal ------------------------------------------------------------------

def test_a_tombstoned_name_is_recovered_and_then_written():
    client = _FakeClient()
    _install(client)
    try:
        ref = sbs.write_azure_kv("portainer_adapter_pat", "ptr_newtoken")
    finally:
        _restore()
    assert ref == "portainer-adapter-pat"
    assert client.recovered == ["portainer-adapter-pat"]
    # The recovered value is the STALE token; the point of recovering is to get the
    # name back so the new one can be written over it.
    assert client.writes == [("portainer-adapter-pat", "ptr_newtoken")]


def test_the_retry_is_not_a_loop():
    """A vault that keeps refusing must fail, not spin. One recover, one retry."""
    client = _FakeClient(conflicts=2)
    _install(client)
    try:
        sbs.write_azure_kv("portainer_adapter_pat", "ptr_newtoken")
    except Exception as exc:
        assert "recoverable" in str(exc)
    else:
        raise AssertionError("a second conflict was swallowed")
    finally:
        _restore()
    assert len(client.recovered) == 1


# -- What must still propagate -------------------------------------------------

def test_an_unrelated_conflict_is_not_recovered():
    """ResourceExistsError covers more than the tombstone. Recovering on any 409
    would turn, say, a disabled-secret conflict into a confusing second error."""
    client = _FakeClient(conflict="(Forbidden) Caller is not authorized to perform "
                                  "action on resource")
    _install(client)
    try:
        sbs.write_azure_kv("portainer_adapter_pat", "ptr_newtoken")
    except Exception as exc:
        assert "not authorized" in str(exc)
    else:
        raise AssertionError("a Forbidden was swallowed as a soft delete")
    finally:
        _restore()
    assert client.recovered == []


def test_a_refused_recovery_names_the_permission_and_the_command():
    """Purge protection or a service principal without `recover` is a real vault
    configuration; the operator needs to know which of the two ways out to take."""
    client = _FakeClient(recover_error="(Forbidden) does not have secrets recover "
                                       "permission on key vault")
    _install(client)
    try:
        sbs.write_azure_kv("portainer_adapter_pat", "ptr_newtoken")
    except RuntimeError as exc:
        text = str(exc)
        assert "portainer-adapter-pat" in text
        assert "recover" in text
        # A runnable command, not a template. The real refusal (an access-policy
        # vault with no `recover` for the dashboard's SP) is copied straight out of
        # the job detail view, and a `<vault>` there is one more thing to go look up
        # on a vault this very message already names. Pinned on the vault's own
        # label rather than its full host: CodeQL reads a `"host.tld" in x` as an
        # incomplete URL check, and this is an assertion about a sentence, not a
        # sanitizer. `kv-demo` reaches the message from _install's URL and nowhere
        # else, so it distinguishes the vault just as well.
        assert "az keyvault secret recover --vault-name kv-demo" in text
        assert "<vault>" not in text
    else:
        raise AssertionError("a refused recovery reported success")
    finally:
        _restore()
    assert client.writes == []


# -- The matcher ---------------------------------------------------------------

def test_the_matcher_reads_the_inner_code_and_the_prose():
    """Azure has spelled this both ways across SDK versions; either is the tombstone."""
    assert sbs._kv_soft_deleted(RuntimeError('{"code": "ObjectIsDeletedButRecoverable"}'))
    assert sbs._kv_soft_deleted(RuntimeError("is currently in a deleted but "
                                             "recoverable state"))
    assert not sbs._kv_soft_deleted(RuntimeError("(Conflict) Secret is disabled"))


def test_the_vault_name_comes_out_of_the_url():
    """The SDK only ever hands back a URL; every az remedy is keyed on the bare
    name. A vault-shaped URL loses scheme, domain and any trailing path."""
    assert sbs._kv_vault_name("https://kv-demo.vault.azure.net") == "kv-demo"
    assert sbs._kv_vault_name("https://kv-demo.vault.azure.net/") == "kv-demo"
    # Sovereign clouds put a different domain after the same label.
    assert sbs._kv_vault_name("https://kv-demo.vault.usgovcloudapi.net") == "kv-demo"


def test_an_unparseable_endpoint_degrades_to_itself():
    """A hand-typed endpoint must not turn the remedy into `--vault-name` with
    nothing after it — a blank there reads as a bug in the dashboard, whereas
    whatever the operator actually configured is a clue. (An EMPTY url never gets
    this far: _azure_kv_client raises on it before any secret call.)"""
    assert sbs._kv_vault_name("not-a-url") == "not-a-url"
    assert sbs._kv_vault_name("kv-demo") == "kv-demo"


def test_the_update_path_does_not_need_the_heal():
    """update_azure_kv's ref comes out of list_azure_kv, which cannot list a deleted
    secret — so a tombstone is unreachable there, and the write path is the one
    place this belongs."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "secrets_backend_service.py"))
    body = src.split("def write_azure_kv(")[1].split("\ndef ")[0]
    assert "_kv_soft_deleted" in body
    assert "begin_recover_deleted_secret" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
