"""Cloud Functions: credential resolution by reference (fnruntime.secretref).

This module is the reason no workload needs a plaintext credential setting, so the
properties worth pinning are the ones that would quietly reintroduce one:

  * the platform-injected value wins, so GCP and Azure never touch the AWS branch
  * an id resolves through Secrets Manager, under BOTH the conventional name and
    the legacy one a workload shipped with
  * a JSON payload yields the credential, and an ambiguous one raises instead of
    returning something plausible and wrong
  * nothing is read twice inside the TTL — a grant should not cost a Secrets
    Manager call per invocation

Stdlib only, with a fake boto3; runs with nothing installed and reaches no cloud.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import secretref

_ENV_KEYS = ("FN_THING", "FN_THING_SECRET_ID", "FN_THING_LEGACY_ID", "AWS_REGION")

CALLS = []


class _FakeClient:
    def __init__(self, payloads):
        self._payloads = payloads

    def get_secret_value(self, SecretId):  # noqa: N803 — boto3's spelling
        CALLS.append(SecretId)
        if SecretId not in self._payloads:
            raise AssertionError(f"unexpected secret id {SecretId!r}")
        return {"SecretString": self._payloads[SecretId]}


def _install_boto3(**payloads):
    """A boto3 whose only job is to answer get_secret_value."""
    module = types.ModuleType("boto3")
    module.client = lambda service, **kw: _FakeClient(payloads)
    sys.modules["boto3"] = module


def _reset(**env):
    CALLS.clear()
    secretref.clear_cache()
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    for key, value in env.items():
        os.environ[key] = value


# ── The platform-resolved path (GCP, Azure) ──────────────────────────────────

def test_the_injected_value_wins_and_aws_is_never_consulted():
    """On GCP and Azure the platform has already put the value in the environment.
    Reaching for boto3 there would fail — there is no boto3, and no Secrets Manager."""
    _reset(FN_THING="injected-by-the-platform", FN_THING_SECRET_ID="arn:aws:...:x")
    sys.modules.pop("boto3", None)
    assert secretref.resolve("FN_THING") == "injected-by-the-platform"
    assert CALLS == []


def test_nothing_configured_is_empty_not_an_exception():
    """Several workloads handle 'no credential' themselves — dry run needs none —
    and their messages say more about the fix than a generic one could."""
    _reset()
    assert secretref.resolve("FN_THING") == ""


# ── The AWS path ─────────────────────────────────────────────────────────────

def test_an_id_resolves_through_secrets_manager():
    _reset(FN_THING_SECRET_ID="arn:aws:secretsmanager:us-east-1:1:secret:thing-Ab12Cd")
    _install_boto3(**{"arn:aws:secretsmanager:us-east-1:1:secret:thing-Ab12Cd": "hunter2"})
    assert secretref.resolve("FN_THING") == "hunter2"
    assert len(CALLS) == 1


def test_the_conventional_id_name_is_the_value_name_plus_a_suffix():
    """The dashboard derives this name when it wires an AWS function up, which is
    what lets one secret_environment entry work on all three clouds."""
    assert secretref.id_env_for("FN_THING") == "FN_THING_SECRET_ID"


def test_a_legacy_id_name_still_resolves_and_wins():
    """db_grant shipped with FN_DB_ADMIN_SECRET_ID and portainer_access advertised
    FN_PORTAINER_KEY_SECRET_ID. Deployments carrying those must keep working."""
    _reset(FN_THING_LEGACY_ID="arn:aws:legacy", FN_THING_SECRET_ID="arn:aws:conventional")
    _install_boto3(**{"arn:aws:legacy": "from-legacy", "arn:aws:conventional": "from-new"})
    assert secretref.resolve("FN_THING", "FN_THING_LEGACY_ID") == "from-legacy"


# ── Payload shapes ───────────────────────────────────────────────────────────

def test_a_json_payload_yields_the_credential():
    _reset(FN_THING_SECRET_ID="arn:aws:json")
    _install_boto3(**{"arn:aws:json": '{"username": "admin", "password": "hunter2"}'})
    assert secretref.resolve("FN_THING") == "hunter2"


def test_a_single_key_payload_is_unambiguous_whatever_it_is_called():
    _reset(FN_THING_SECRET_ID="arn:aws:one")
    _install_boto3(**{"arn:aws:one": '{"portainer_token": "ptr_abc"}'})
    assert secretref.resolve("FN_THING") == "ptr_abc"


def test_an_ambiguous_payload_raises_rather_than_guessing():
    """A wrong credential fails later, somewhere else, looking like a permissions
    problem. The message names the KEYS and never the values."""
    _reset(FN_THING_SECRET_ID="arn:aws:ambiguous")
    _install_boto3(**{"arn:aws:ambiguous": '{"left": "hunter2", "right": "swordfish"}'})
    try:
        secretref.resolve("FN_THING")
    except RuntimeError as exc:
        assert "left" in str(exc) and "right" in str(exc), exc
        assert "hunter2" not in str(exc) and "swordfish" not in str(exc), exc
    else:
        raise AssertionError("an ambiguous payload resolved to something")


def test_a_bare_string_that_happens_to_start_with_a_brace_survives():
    _reset(FN_THING_SECRET_ID="arn:aws:notjson")
    _install_boto3(**{"arn:aws:notjson": "{this is not json"})
    assert secretref.resolve("FN_THING") == "{this is not json"


# ── Caching ──────────────────────────────────────────────────────────────────

def test_a_warm_function_does_not_re_read_the_secret():
    """db_grant resolves the admin password once per connection and twice per grant.
    Uncached, that is a Secrets Manager call — and a throttling quota — per grant."""
    _reset(FN_THING_SECRET_ID="arn:aws:cached")
    _install_boto3(**{"arn:aws:cached": "hunter2"})
    for _ in range(5):
        assert secretref.resolve("FN_THING") == "hunter2"
    assert len(CALLS) == 1, CALLS


def test_clearing_the_cache_forces_a_re_read():
    """The escape hatch for a rotated credential."""
    _reset(FN_THING_SECRET_ID="arn:aws:cached")
    _install_boto3(**{"arn:aws:cached": "hunter2"})
    secretref.resolve("FN_THING")
    secretref.clear_cache()
    secretref.resolve("FN_THING")
    assert len(CALLS) == 2, CALLS


# ── The workloads actually use it ────────────────────────────────────────────

def test_every_credential_using_workload_resolves_by_reference():
    """The regression that matters: a workload reading os.environ directly for a
    credential is a workload that needs a plaintext setting."""
    import inspect
    from fnworkloads import azure_role_grant, db_grant, portainer_access
    for module, var in ((db_grant, "FN_DB_ADMIN_PASSWORD"),
                        (portainer_access, "FN_PORTAINER_API_KEY"),
                        (azure_role_grant, "FN_AZURE_CLIENT_SECRET")):
        source = inspect.getsource(module)
        assert f'secretref.resolve("{var}"' in source, \
            f"{module.NAME} does not resolve {var} through fnruntime.secretref"
        assert f'_env("{var}")' not in source, \
            f"{module.NAME} still reads {var} as a plaintext env var"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
