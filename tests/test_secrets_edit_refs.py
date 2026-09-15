"""Editing a secret must write at the ref it was given, not re-derive one.

PATCH /api/secrets/items/{backend}/{ref} addresses a secret by the reference the
listing handed out. It used to hand that ref to write_sync_validated, whose
writers take a KEY and derive the backend's own name from it -- an AWS prefix and
a slash, a Key Vault dash-swap, a GCP prefix, a Secrets Safe folder. Deriving a
second time off an already-derived ref writes somewhere nothing was ever stored:

    dashboard/aws_secret_access_key  ->  dashboard/dashboard/aws_secret_access_key

The original was untouched, the aws_sm:// reference in app_config kept resolving
to the OLD value, and the Secrets page reported the save as successful. An
operator who "rotated" a migrated credential from Browse & Edit had silently
changed nothing.

Each test below pins both halves: the ref the updater actually writes at, AND the
double-derived shape it replaces, so the regression cannot come back looking like
a passing test.

Runs under pytest, or standalone:  python tests/test_secrets_edit_refs.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, as the sibling suites do: gating on whether boto3 /
# azure / google happen to be installed is how a suite passes locally and fails
# in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: (CALLS.append(("db.set", key, value)),
                              CONF.__setitem__(key, value))[1],
      delete=lambda key: CONF.pop(key, None))

_stub("web_dashboard.services.workload_credentials_service",
      write_static=lambda name, value, folder="": CALLS.append(
          ("wlc.write_static", name, value, folder)))


class _FakeSm:
    """Only the two calls the AWS paths make."""

    def put_secret_value(self, **kw):
        CALLS.append(("aws.put", kw["SecretId"], kw["SecretString"]))

    def create_secret(self, **kw):  # pragma: no cover - a create here is a bug
        CALLS.append(("aws.create", kw["Name"], kw.get("SecretString", "")))


_stub("boto3", client=lambda service, **kw: _FakeSm())

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace(
        aws_region="", gcp_project_id=""))

from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402


class _FakeKv:
    def set_secret(self, name, value):
        CALLS.append(("kv.set", name, value))


class _FakeGcp:
    def add_secret_version(self, request):
        CALLS.append(("gcp.add_version", request["parent"],
                      request["payload"]["data"].decode()))

    def create_secret(self, request):  # pragma: no cover - a create here is a bug
        CALLS.append(("gcp.create", request["secret_id"]))


sbs._azure_kv_client = lambda: (_FakeKv(), "https://kv.example/")
sbs._gcp_client = lambda: _FakeGcp()


def _reset(**cfg):
    CONF.clear()
    CONF.update(cfg)
    CALLS.clear()


def _only(kind):
    """The single recorded call of a kind -- asserting there is exactly one is half
    the point, since the old behaviour wrote an EXTRA secret rather than none."""
    hits = [c for c in CALLS if c[0] == kind]
    assert len(hits) == 1, f"expected one {kind}, got {CALLS}"
    return hits[0]


# -- AWS Secrets Manager -------------------------------------------------------

def test_editing_an_aws_secret_writes_at_its_own_path():
    """list_aws_sm hands out s["Name"], which already carries the prefix."""
    _reset(secrets_aws_region="us-east-1", secrets_aws_prefix="dashboard")
    ref = "dashboard/aws_secret_access_key"
    assert sbs.update_sync("aws_sm", ref, '{"v": 2}') == ref
    assert _only("aws.put") == ("aws.put", ref, '{"v": 2}')


def test_the_aws_double_prefix_is_what_the_write_path_would_have_produced():
    """The shape being replaced, pinned off the writer's own helper so this test
    fails if the derivation itself ever changes rather than going stale."""
    _reset(secrets_aws_region="us-east-1", secrets_aws_prefix="dashboard")
    ref = "dashboard/aws_secret_access_key"
    assert sbs._aws_secret_name(ref) == "dashboard/dashboard/aws_secret_access_key"
    sbs.update_sync("aws_sm", ref, '{"v": 2}')
    assert _only("aws.put")[1] != sbs._aws_secret_name(ref)


def test_an_aws_edit_never_creates():
    """create_secret on an edit is how the second, double-prefixed secret appeared;
    the original kept serving its old value beside it."""
    _reset(secrets_aws_region="us-east-1", secrets_aws_prefix="dashboard")
    sbs.update_sync("aws_sm", "dashboard/x", '{}')
    assert not [c for c in CALLS if c[0] == "aws.create"]


# -- Azure Key Vault -----------------------------------------------------------

def test_editing_a_key_vault_secret_writes_at_its_own_name():
    _reset()
    ref = "azure-client-secret"
    assert sbs.update_sync("azure_kv", ref, '{"v": 2}') == ref
    assert _only("kv.set") == ("kv.set", ref, '{"v": 2}')


def test_the_key_vault_updater_does_not_mangle_the_ref():
    """Key Vault names are dash-only, so re-mangling a live ref is a no-op today.
    The guarantee this path needs is that the write lands where the caller
    pointed, not that one particular transform is currently harmless."""
    _reset()
    ref = "legacy_name"
    sbs.update_sync("azure_kv", ref, '{}')
    assert sbs._kv_name(ref) == "legacy-name"
    assert _only("kv.set")[1] == "legacy_name"


# -- GCP Secret Manager --------------------------------------------------------

def test_editing_a_gcp_secret_writes_at_its_own_secret_id():
    _reset(gcp_project_id="proj", secrets_gcp_prefix="dashboard")
    ref = "dashboard-gcp-service-account-json"
    assert sbs.update_sync("gcp_sm", ref, '{"v": 2}') == ref
    assert _only("gcp.add_version") == (
        "gcp.add_version", f"projects/proj/secrets/{ref}", '{"v": 2}')


def test_the_gcp_double_prefix_is_what_the_write_path_would_have_produced():
    _reset(gcp_project_id="proj", secrets_gcp_prefix="dashboard")
    ref = "dashboard-gcp-service-account-json"
    assert sbs._gcp_secret_id(ref) == "dashboard-dashboard-gcp-service-account-json"
    sbs.update_sync("gcp_sm", ref, '{}')
    assert _only("gcp.add_version")[1].endswith("/" + ref)


def test_a_gcp_edit_never_creates():
    """A missing secret id must raise NotFound out of add_secret_version. Creating
    is what turned a failed edit into a silently duplicated secret."""
    _reset(gcp_project_id="proj", secrets_gcp_prefix="dashboard")
    sbs.update_sync("gcp_sm", "dashboard-x", '{}')
    assert not [c for c in CALLS if c[0] == "gcp.create"]


# -- BeyondTrust Secrets Safe --------------------------------------------------
#
# ps-cli addresses a secret by its FOLDER-QUALIFIED title, which is exactly what
# list_bt_secrets_safe returns as the ref -- so _bt_secret_title, which builds
# that qualified form out of a bare key, must not run on one again.

BT_STORE = {"Dashboard/pscli_client_secret": '{"v": 1}',
            "Archive/old_secret": '{"v": 1}'}
BT_FOLDERS = [{"Name": "Dashboard", "Id": "f-dash"},
              {"Name": "Archive", "Id": "f-arch"}]


def _fake_ps_run(args, timeout=30):
    verb = tuple(args[:2])
    if verb == ("folders", "list"):
        return list(BT_FOLDERS)
    if verb == ("secrets", "create-secret"):
        title = args[args.index("-t") + 1]
        value = args[args.index("--text") + 1]
        CALLS.append(("bt.create", title, value, args[args.index("-fid") + 1]))
        BT_STORE[title] = value
        return {}
    if verb == ("secrets", "get"):
        title = args[args.index("-t") + 1]
        return [{"Text": BT_STORE[title]}] if title in BT_STORE else []
    raise AssertionError(f"unexpected ps-cli call: {args}")


sbs._ps_run = _fake_ps_run
sbs._bt_owner_id = lambda: "2"


def _reset_bt(**cfg):
    _reset(**cfg)
    BT_STORE.clear()
    BT_STORE.update({"Dashboard/pscli_client_secret": '{"v": 1}',
                     "Archive/old_secret": '{"v": 1}'})


def test_editing_a_secrets_safe_secret_writes_at_its_own_title():
    _reset_bt(secrets_bt_folder="Dashboard")
    ref = "Dashboard/pscli_client_secret"
    assert sbs.update_sync("bt_secrets_safe", ref, '{"v": 2}') == ref
    assert _only("bt.create") == ("bt.create", ref, '{"v": 2}', "f-dash")
    assert BT_STORE[ref] == '{"v": 2}'


def test_the_secrets_safe_double_folder_is_what_the_write_path_would_produce():
    _reset_bt(secrets_bt_folder="Dashboard")
    ref = "Dashboard/pscli_client_secret"
    assert sbs._bt_secret_title(ref) == "Dashboard/Dashboard/pscli_client_secret"
    sbs.update_sync("bt_secrets_safe", ref, '{"v": 2}')
    assert "Dashboard/Dashboard/pscli_client_secret" not in BT_STORE


def test_the_folder_comes_from_the_ref_not_from_config():
    """An operator browsing another folder edits the secret they are looking at.
    Re-deriving the folder would write a copy into the configured one and leave
    the original serving its old value."""
    _reset_bt(secrets_bt_folder="Dashboard")
    sbs.update_sync("bt_secrets_safe", "Archive/old_secret", '{"v": 2}')
    assert _only("bt.create")[3] == "f-arch"
    assert BT_STORE["Archive/old_secret"] == '{"v": 2}'


def test_a_secrets_safe_edit_that_did_not_take_raises():
    """ps-cli has exited 0 without persisting before, which is why the write path
    already verifies. On an EDIT the pre-existing value reads back unchanged, so
    only comparing the value catches it -- a presence check would pass."""
    _reset_bt(secrets_bt_folder="Dashboard")
    sbs._ps_run = lambda args, timeout=30: (
        list(BT_FOLDERS) if tuple(args[:2]) == ("folders", "list")
        else [{"Text": '{"v": 1}'}] if tuple(args[:2]) == ("secrets", "get")
        else {})
    try:
        sbs.update_sync("bt_secrets_safe", "Dashboard/pscli_client_secret", '{"v": 2}')
    except ValueError as exc:
        assert "previous value" in str(exc)
    else:
        raise AssertionError("a dropped write reported success")
    finally:
        sbs._ps_run = _fake_ps_run


def test_an_invisible_folder_names_the_folder_rather_than_failing_at_ps_cli():
    _reset_bt(secrets_bt_folder="Dashboard")
    try:
        sbs.update_sync("bt_secrets_safe", "Nowhere/x", '{}')
    except ValueError as exc:
        assert "Nowhere" in str(exc)
    else:
        raise AssertionError("an unresolvable folder reached create-secret")


# -- Workload Credentials and the database backend -----------------------------

def test_editing_a_wlc_secret_goes_through_its_own_updater():
    """The wlc updater itself is covered in test_workload_credentials.py, which
    owns that backend; what is pinned here is that the dispatch reaches it.

    write_static is create-or-update, so a re-derived folder does not fail -- it
    creates a SECOND secret beside the one being edited."""
    _reset(secrets_wlc_folder="dashboard")
    ref = "dashboard/sub/wlc-probe"
    assert sbs.update_sync("wlc", ref, '{"v": 2}') == ref
    assert _only("wlc.write_static") == (
        "wlc.write_static", "wlc-probe", '{"v": 2}', "dashboard/sub")


def test_the_wlc_folder_is_split_off_the_ref_not_read_from_config():
    """Listing is recursive, so a secret at dashboard/sub/x has a ref whose folder
    is not the configured one."""
    _reset(secrets_wlc_folder="dashboard")
    sbs.update_sync("wlc", "dashboard/sub/x", '{}')
    assert _only("wlc.write_static")[3] == "dashboard/sub"


def test_editing_a_database_secret_writes_at_its_own_key():
    _reset()
    assert sbs.update_sync("database", "aws_secret_access_key", '{"v": 2}') \
        == "aws_secret_access_key"
    assert _only("db.set") == ("db.set", "aws_secret_access_key", '{"v": 2}')


# -- Dispatch + the API edge ---------------------------------------------------

def test_every_writable_backend_has_an_updater():
    """A backend in _WRITE_FN but not _UPDATE_FN is the bug: it used to fall
    through to the key-deriving writer, which reported success."""
    assert set(sbs._UPDATE_FN) == set(sbs._WRITE_FN)


def test_an_unknown_backend_raises_rather_than_falling_through_to_write():
    _reset()
    try:
        sbs.update_sync("nope", "ref", '{}')
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("an unknown backend silently wrote somewhere")


def test_the_update_path_validates_json_like_the_write_path():
    _reset()
    try:
        sbs.update_sync_validated("database", "k", "not json")
    except ValueError as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("a non-JSON value reached the backend")
    assert not CALLS


def test_the_patch_route_uses_the_update_path_not_the_write_path():
    """The whole bug in one line: PATCH holds a ref, and write_sync_validated
    takes a key."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "secrets.py"),
               encoding="utf-8").read()
    body = src.split("async def update_secret_item(")[1].split("\n@router")[0]
    assert "sbs.update_sync_validated" in body
    assert "write_sync_validated" not in body


def test_the_create_route_still_uses_the_write_path():
    """POST /items takes a key from the operator and must keep deriving."""
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "secrets.py"),
               encoding="utf-8").read()
    body = src.split("async def create_secret_item(")[1].split("\n@router")[0]
    assert "sbs.write_sync_validated" in body


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
