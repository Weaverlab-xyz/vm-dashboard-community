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
  * a FILE reference resolves, in both the shapes a self-hosted runtime mounts, and
    every broken form of it raises rather than looking unconfigured

Stdlib only, with a fake boto3; runs with nothing installed and reaches no cloud.
"""
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import secretref

_ENV_KEYS = ("FN_THING", "FN_THING_FILE", "FN_THING_SECRET_ID", "FN_THING_LEGACY_ID",
             "AWS_REGION")

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


# ── The reference the platform never resolved ────────────────────────────────
#
# Azure leaves an unreadable Key Vault reference in the app setting VERBATIM: no
# error, no empty value, nothing in the app's own logs. Live, a portainer_access
# adapter whose identity was short a vault grant sent that text as its API key and
# reported the only thing it could see — Portainer answering 401 — which reads as a
# revoked token and sends the operator to the wrong system entirely.

def test_an_unresolved_key_vault_reference_is_refused_not_forwarded():
    """The failure has to surface HERE. A reference is never a credential, so there
    is no case where passing it on does anything but fail somewhere less obvious."""
    _reset(FN_THING="@Microsoft.KeyVault(SecretUri=https://v.vault.azure.net/secrets/k/)")
    try:
        secretref.resolve("FN_THING")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("an unresolved Key Vault reference was returned as a "
                             "credential")
    assert "FN_THING" in message, message
    # The remedy, not just the diagnosis: the identity is the thing to go and fix.
    assert "identity" in message.lower(), message


def test_an_app_configuration_reference_is_refused_too():
    """Same mechanism, same silent failure mode, so the same answer."""
    _reset(FN_THING="@Microsoft.AppConfiguration(Endpoint=https://c.azconfig.io; Key=k)")
    try:
        secretref.resolve("FN_THING")
    except RuntimeError:
        return
    raise AssertionError("an unresolved App Configuration reference was returned")


def test_a_resolved_value_that_merely_mentions_the_vault_is_not_refused():
    """The guard is a PREFIX test on purpose. A credential is opaque bytes and may
    contain anything; refusing one for its content would be a new way to break a
    working function."""
    _reset(FN_THING="ptr_secret@Microsoft.KeyVault(nonsense)")
    assert secretref.resolve("FN_THING") == "ptr_secret@Microsoft.KeyVault(nonsense)"


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
    import re
    from fnworkloads import azure_role_grant, db_grant, portainer_access
    for module, var in ((db_grant, "FN_DB_ADMIN_PASSWORD"),
                        (portainer_access, "FN_PORTAINER_API_KEY"),
                        (azure_role_grant, "FN_AZURE_CLIENT_SECRET")):
        # Whitespace-stripped, so line breaking the call is not a test failure.
        compact = re.sub(r"\s+", "", inspect.getsource(module))
        assert f'secretref.resolve("{var}"' in compact, \
            f"{module.NAME} does not resolve {var} through fnruntime.secretref"
        assert f'_env("{var}")' not in compact, \
            f"{module.NAME} still reads {var} as a plaintext env var"


# ── The file path (OpenFaaS, Nuclio, a plain Kubernetes Secret volume) ────────
#
# The fourth channel, and the only one where "the reference is set but broken" is a
# routine operator mistake rather than an exotic one: a secret absent from a
# function's ``secrets:`` list is simply not mounted, and the file is silently not
# there. So every failure here has to raise and name the setting — returning ""
# would report the operator's mistake as the workload having no credential.

def _mkdtemp() -> str:
    path = tempfile.mkdtemp(prefix="secretref-")
    _TEMPDIRS.append(path)
    return path


_TEMPDIRS = []


def _cleanup_tempdirs():
    while _TEMPDIRS:
        shutil.rmtree(_TEMPDIRS.pop(), ignore_errors=True)


def _write(directory: str, name: str, body: str) -> str:
    target = os.path.join(directory, name)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(body)
    return target


def test_a_file_reference_resolves_and_is_stripped():
    """Stripped because every way of writing one of these adds a newline.

    ``kubectl create secret --from-file``, a heredoc, an editor — all of them. A
    credential with a trailing newline compares unequal to the same credential
    without one, so the target reports a WRONG password rather than a malformed
    one, and the hunt starts in the wrong place.
    """
    try:
        path = _write(_mkdtemp(), "bearer", "s3cret-from-a-file\n")
        _reset(FN_THING_FILE=path)
        assert secretref.resolve("FN_THING") == "s3cret-from-a-file"
    finally:
        _cleanup_tempdirs()


def test_the_platform_injected_value_still_wins_over_a_file():
    try:
        path = _write(_mkdtemp(), "bearer", "from-the-file")
        _reset(FN_THING="from-the-env", FN_THING_FILE=path)
        assert secretref.resolve("FN_THING") == "from-the-env"
    finally:
        _cleanup_tempdirs()


def test_a_file_outranks_an_id_so_a_self_hosted_target_never_reaches_boto3():
    """Ordering matters, and not only for tidiness.

    A self-hosted runtime has no Secrets Manager to fall through to, so reaching the
    AWS branch there costs an import that cannot succeed — and the operator gets an
    ImportError in place of the real condition. boto3 is deliberately NOT installed
    in this test: if the order ever flips, this fails with that import rather than
    passing quietly.
    """
    sys.modules.pop("boto3", None)
    try:
        path = _write(_mkdtemp(), "bearer", "from-the-file")
        _reset(FN_THING_FILE=path, FN_THING_SECRET_ID="some/secret")
        assert secretref.resolve("FN_THING") == "from-the-file"
    finally:
        _cleanup_tempdirs()


def test_a_directory_holding_one_file_resolves():
    """A Kubernetes Secret volume mounted without ``items`` is a DIRECTORY.

    OpenFaaS names one file per secret; a plain volume names a directory with one
    file per key. An adapter cannot tell which it got, so both resolve.
    """
    try:
        directory = _mkdtemp()
        _write(directory, "bearer", "from-a-directory\n")
        _reset(FN_THING_FILE=directory)
        assert secretref.resolve("FN_THING") == "from-a-directory"
    finally:
        _cleanup_tempdirs()


def test_kubernetes_dotdot_projection_entries_are_not_mistaken_for_keys():
    """Kubernetes projects a Secret through ``..data`` and a timestamped directory.

    Those are the projection machinery, not keys. Counting them would make every
    real single-key mount look ambiguous and refuse — i.e. the ordinary case would
    be the broken one.
    """
    try:
        directory = _mkdtemp()
        os.mkdir(os.path.join(directory, "..2026_09_22_00_00_00.12345"))
        os.mkdir(os.path.join(directory, "..data"))
        _write(directory, "bearer", "the-real-key")
        _reset(FN_THING_FILE=directory)
        assert secretref.resolve("FN_THING") == "the-real-key"
    finally:
        _cleanup_tempdirs()


def test_a_directory_with_two_files_raises_and_names_both():
    """Picking one would hand out whichever secret sorted earlier."""
    try:
        directory = _mkdtemp()
        _write(directory, "bearer", "one")
        _write(directory, "password", "two")
        _reset(FN_THING_FILE=directory)
        try:
            secretref.resolve("FN_THING")
            raise AssertionError("an ambiguous secret directory resolved")
        except RuntimeError as exc:
            message = str(exc)
        assert "FN_THING_FILE" in message, message
        assert "bearer" in message and "password" in message, message
    finally:
        _cleanup_tempdirs()


def test_an_empty_directory_raises():
    try:
        _reset(FN_THING_FILE=_mkdtemp())
        try:
            secretref.resolve("FN_THING")
            raise AssertionError("an empty secret directory resolved")
        except RuntimeError as exc:
            assert "FN_THING_FILE" in str(exc), str(exc)
    finally:
        _cleanup_tempdirs()


def test_a_missing_file_raises_rather_than_looking_unconfigured():
    """The distinction this whole channel turns on.

    "nothing is configured" is a condition several workloads handle themselves; "you
    named a file that is not there" is an operator error. Collapsing them means a
    secret missing from the function's ``secrets:`` list presents as a workload with
    no credential, and the error message sends you to the workload.
    """
    try:
        _reset(FN_THING_FILE=os.path.join(_mkdtemp(), "never-mounted"))
        try:
            secretref.resolve("FN_THING")
            raise AssertionError("a missing secret file resolved to something")
        except RuntimeError as exc:
            message = str(exc)
        assert "FN_THING_FILE" in message, message
        assert "secrets:" in message, f"the remedy is not in the message: {message}"
    finally:
        _cleanup_tempdirs()


def test_an_empty_file_raises_because_empty_cannot_be_told_from_unset():
    try:
        path = _write(_mkdtemp(), "bearer", "   \n")
        _reset(FN_THING_FILE=path)
        try:
            secretref.resolve("FN_THING")
            raise AssertionError("an empty secret file resolved to something")
        except RuntimeError as exc:
            assert "empty" in str(exc).lower(), str(exc)
    finally:
        _cleanup_tempdirs()


def test_file_env_for_follows_the_same_convention_as_id_env_for():
    # The dashboard derives this name when it writes the Function's environment, so
    # a change here is a change to a contract with an image baked weeks earlier.
    assert secretref.file_env_for("FN_SHARED_SECRET") == "FN_SHARED_SECRET_FILE"
    assert secretref.file_env_for("FN_THING") == "FN_THING_FILE"


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
