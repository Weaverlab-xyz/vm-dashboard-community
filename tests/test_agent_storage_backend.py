"""Unit tests for the agent-brokered remote filesystem / UNC storage backend.

Three separate surfaces meet here and each has its own failure mode:

  * ``agent_storage_meta`` — the closed allowlist that decides what may cross to the
    agent. Its job is to make a path unsayable rather than merely refused.
  * ``storage_service`` — the dispatch table, which now holds coroutine ops beside the
    synchronous ones, plus the listing cache whose key must be scoped per share.
  * ``runners/agent/agent.py`` — the policy grant and the second, independent path check.
    The agent must not trust the dashboard's validation; both are tested.

Pure Python: config_service, cloud_executor, cache_service and the agent bridge are
stubbed, so no DB, no FastAPI and no cloud SDKs are needed. Runs under pytest, or
standalone:
    python tests/test_agent_storage_backend.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import textwrap
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
CACHE = {}
CALLS = []


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


META = _load(os.path.join(_ROOT, "web_dashboard", "services", "agent_storage_meta.py"),
             "agent_storage_meta")
AGENT = _load(os.path.join(_ROOT, "runners", "agent", "agent.py"), "agent_under_test")


# ── agent_storage_meta: what may cross the wire ──────────────────────────────

def test_a_name_may_not_be_a_path():
    """The property the whole design rests on. A traversal is not refused by a check
    that could be forgotten — there is no field it could be written in."""
    for bad in ("../../etc/passwd", "sub/site.yml", "sub\\site.yml", "..",
                ".ssh", "/etc/passwd", "C:\\Windows\\x.yml"):
        problem = META.check({"op": "fetch", "share": "playbooks", "name": bad})
        assert problem, f"{bad!r} was accepted as a file name"
        assert "plain file name" in problem, f"{bad!r} got the wrong refusal: {problem}"
    assert META.check({"op": "fetch", "share": "playbooks", "name": "site.yml"}) == ""


def test_a_traversing_name_normalises_to_empty_rather_than_to_itself():
    """normalize() is forgiving so an old job row stays cancellable, but forgiving must
    not mean passing the bad value through — every write path then refuses the empty
    name in check() instead of acting on a half-parsed one."""
    assert META.normalize({"op": "fetch", "share": "p", "name": "../x"})["name"] == ""


def test_an_absolute_subpath_is_refused_not_silently_made_relative():
    """Stripping the leading separator would accept an absolute path while telling the
    operator absolute paths are refused. A trailing one is genuinely noise."""
    for bad in ("/etc", "C:\\Windows", "\\\\srv\\c$", "a/../x"):
        problem = META.check({"op": "list", "share": "p", "subpath": bad})
        assert problem, f"{bad!r} was accepted as a subpath"
    assert META.check({"op": "list", "share": "p", "subpath": "win/prod/"}) == ""
    assert META.normalize({"op": "list", "share": "p",
                           "subpath": "win/prod/"})["subpath"] == "win/prod"
    assert META.normalize({"op": "list", "share": "p", "subpath": "/etc"})["subpath"] == ""


def test_a_share_name_cannot_express_a_path():
    assert META.check({"op": "list", "share": "../evil"})
    assert META.check({"op": "list", "share": ""})
    assert META.check({"op": "list", "share": "corp-automation"}) == ""


def test_the_operation_set_is_closed():
    assert META.VALID_OPS == ("list", "fetch", "upload", "delete")
    assert META.check({"op": "exec", "share": "p", "name": "x.yml"})
    # No `move`: it is a fetch plus an upload plus a delete, each granted separately.
    assert "move" not in META.VALID_OPS


def test_write_ops_are_the_ones_policy_grants_separately():
    """policy.yaml grants read and write apart, so the dashboard has to agree with the
    agent about which operations are writes or it refuses the wrong set."""
    assert set(META.WRITE_OPS) == {"upload", "delete"}


def test_the_envelope_carries_nothing_beyond_the_allowlist():
    payload = META.envelope_payload({"op": "list", "share": "p", "description": "hi",
                                     "path": "/etc", "extra": 1})
    assert set(payload) == set(META.STORAGE_META_KEYS)
    assert "path" not in payload and "description" not in payload


# ── storage_service: dispatch, cache scoping, size ceiling ───────────────────

def _install_stubs():
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    ce = types.ModuleType("web_dashboard.services.cloud_executor")

    class CloudCallError(Exception):
        pass

    async def run(_provider, fn, *args, **kwargs):
        CALLS.append(("thread", getattr(fn, "__name__", str(fn))))
        return fn(*args, **kwargs)

    ce.CloudCallError = CloudCallError
    ce.run = run
    sys.modules["web_dashboard.services.cloud_executor"] = ce
    services.cloud_executor = ce

    cache = types.ModuleType("web_dashboard.services.cache_service")
    cache.TTL = {"agent_storage_list": 120}
    cache.key_param = lambda name, **p: (
        "vmcli:" + name + ":" + ":".join(f"{k}={v}" for k, v in sorted(p.items())))

    async def get_or_refresh(key, _ttl, fetcher, background=True):
        CALLS.append(("cache", key))
        if key not in CACHE:
            CACHE[key] = await fetcher()
        return CACHE[key], "now"

    async def invalidate(key):
        CACHE.pop(key, None)
        CALLS.append(("invalidate", key))

    cache.get_or_refresh = get_or_refresh
    cache.invalidate = invalidate
    sys.modules["web_dashboard.services.cache_service"] = cache
    services.cache_service = cache

    ags = types.ModuleType("web_dashboard.services.agent_storage_service")

    class AgentStorageError(Exception):
        pass

    async def run_op(op, *, name="", content_b64=""):
        CALLS.append(("agent", op, name))
        if op == "list":
            return {"assets": [{"name": "site.yml", "size": 12},
                               {"name": "notes.txt", "size": 3}]}
        if op == "fetch":
            return {"content_b64": "aGk="}
        return {}

    ags.AgentStorageError = AgentStorageError
    ags.run_op = run_op
    ags.configured = lambda: (CONF.get("storage_agent_id", ""),
                              CONF.get("storage_agent_share", ""),
                              CONF.get("storage_agent_subpath", ""))
    sys.modules["web_dashboard.services.agent_storage_service"] = ags
    services.agent_storage_service = ags

    svc = types.ModuleType("web_dashboard.services.agent_service")
    svc.MAX_RESULT_BYTES = 256 * 1024
    sys.modules["web_dashboard.services.agent_service"] = svc
    services.agent_service = svc


_install_stubs()
_STORAGE = os.path.join(_ROOT, "web_dashboard", "services", "storage_service.py")


def _fresh(**conf):
    CONF.clear()
    CACHE.clear()
    CALLS.clear()
    CONF.update({
        "storage_agent_id": "agent-1",
        "storage_agent_share": "playbooks",
        "storage_active_backend": "agent_local",
    })
    CONF.update({k: str(v) for k, v in conf.items()})
    return _load(_STORAGE, "web_dashboard.services.storage_service")


def test_the_backend_is_configured_by_both_halves_of_the_join():
    """An agent with no share has nothing to open; a share with no agent has nobody to
    open it. Either alone must not read as configured, or the page offers a backend
    every operation then fails on."""
    mod = _fresh()
    assert "agent_local" in mod.configured_backends()
    assert "agent_local" not in _fresh(storage_agent_share="").configured_backends()
    assert "agent_local" not in _fresh(storage_agent_id="").configured_backends()


def test_the_async_op_is_awaited_and_never_handed_to_the_thread_pool():
    """A coroutine passed to _to_thread returns a coroutine OBJECT, which every caller
    would then store, list or upload as though it were the result. This is why one
    dispatcher exists rather than an `if` at each of the eight call sites."""
    mod = _fresh()
    assets = asyncio.run(mod.list_assets())
    assert [a["name"] for a in assets] == ["site.yml"], assets
    assert ("agent", "list", "") in CALLS
    assert not any(kind == "thread" for kind, *_ in CALLS), (
        f"an agent op was put on the storage thread pool: {CALLS}")


def test_a_sync_backend_still_goes_to_the_thread_pool():
    """The other half of the dispatcher's contract — the change must not quietly move
    every blocking SDK call onto the event loop."""
    mod = _fresh(storage_s3_bucket="assets", storage_active_backend="s3")
    try:
        asyncio.run(mod.list_assets())
    except Exception:                                     # noqa: BLE001 — no boto3 here
        pass
    assert any(kind == "thread" for kind, *_ in CALLS), CALLS


def test_the_listing_filters_to_asset_extensions():
    """The agent applies its own extension list, but this is the one that has to agree
    with the rest of the dashboard: an asset the pickers cannot render is worse than
    one they never saw."""
    mod = _fresh()
    assert [a["name"] for a in asyncio.run(mod.list_assets())] == ["site.yml"]


def test_a_zero_byte_file_fetches_as_empty_not_as_an_error():
    """base64 of b"" is "", so a truthiness check here would report an empty file on the
    share as a protocol failure — sending the operator to look at the agent instead of
    at the file. Presence of the field is the question, not its truthiness."""
    mod = _fresh()
    ags = sys.modules["web_dashboard.services.agent_storage_service"]
    original = ags.run_op

    async def empty(op, **kwargs):
        return {"content_b64": "", "size": 0}

    ags.run_op = empty
    try:
        assert asyncio.run(mod.fetch_asset_in("agent_local", "empty.yml")) == b""
    finally:
        ags.run_op = original

    async def missing(op, **kwargs):
        return {"size": 0}

    ags.run_op = missing
    try:
        asyncio.run(mod.fetch_asset_in("agent_local", "empty.yml"))
        raise AssertionError("a reply with no content field was accepted")
    except mod.StorageError as exc:
        assert "no content field" in str(exc)
    finally:
        ags.run_op = original


def test_the_listing_cache_key_is_scoped_per_share():
    """An unscoped key_global would be correct for exactly as long as one dashboard
    talks to one share, and silently wrong afterwards — serving the previous share's
    filenames for a full TTL to a page with no way to know."""
    keys = set()
    for conf in ({}, {"storage_agent_share": "other"},
                 {"storage_agent_id": "agent-2"}, {"storage_agent_subpath": "win"}):
        mod = _fresh(**conf)
        keys.add(mod._agent_cache_key())
    assert len(keys) == 4, f"cache key does not distinguish agent/share/subpath: {keys}"


def test_a_write_invalidates_the_cached_listing():
    """Otherwise an upload appears to succeed and the asset table keeps showing the old
    contents for two minutes, which reads as a failed upload."""
    mod = _fresh()
    asyncio.run(mod.list_assets())
    key = mod._agent_cache_key()
    assert key in CACHE
    asyncio.run(mod.upload_asset("new.yml", b"- hosts: all\n"))
    assert key not in CACHE, "the listing survived an upload"


def test_an_oversize_upload_is_refused_before_it_is_queued():
    """Refused here rather than on the agent: a job that fails on the far side reads as
    an agent fault, and the operator cannot see that the file was simply too big for
    the transport."""
    mod = _fresh()
    big = b"x" * (256 * 1024)
    try:
        asyncio.run(mod.upload_asset("big.yml", big))
        raise AssertionError("an oversize upload was queued")
    except mod.StorageError as exc:
        assert "capped" in str(exc) and "cloud backend" in str(exc), str(exc)
    assert not any(kind == "agent" for kind, *_ in CALLS), (
        "the oversize upload reached the agent anyway")


def test_the_agent_error_becomes_a_storage_error():
    """Dozens of `except StorageError` handlers turn a failure into a 503 or an
    unavailable tile. They must keep working without knowing an agent was involved."""
    mod = _fresh()
    ags = sys.modules["web_dashboard.services.agent_storage_service"]
    original = ags.run_op

    async def boom(op, **kwargs):
        raise ags.AgentStorageError("agent 'a' is offline")

    ags.run_op = boom
    try:
        asyncio.run(mod.list_assets())
        raise AssertionError("the agent failure did not surface")
    except mod.StorageError as exc:
        assert "offline" in str(exc)
    finally:
        ags.run_op = original


def test_it_cannot_be_the_image_hub_and_holds_no_terraform_state():
    """Both follow from the transport, not from taste: multi-GB VHDs do not fit a job
    envelope, and terraform.py maps no state backend for a filesystem — so state stays
    in the deploy dir exactly as it does for `local`."""
    mod = _fresh()
    assert "agent_local" not in mod.CLOUD_BACKENDS
    assert "agent_local" in mod._DEPLOY_DIR_STATE_BACKENDS
    assert "agent_local" not in mod._STATE_KEYS


def test_it_is_not_runner_locked_which_is_the_whole_point():
    """The `local` backend is pinned to ansible_runner=local because THIS container
    opens the SMB socket. Here the agent does, so the constraint does not apply — if it
    were copied across, the feature would be unusable on the deployments that need it."""
    # Read as source rather than imported: api/storage.py pulls in FastAPI and the
    # database, and the assertion is about the membership of two module-level sets,
    # which is exactly what the text says.
    with open(os.path.join(_ROOT, "web_dashboard", "api", "storage.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert '_LOCAL_RUNNER_ONLY_BACKENDS = {"local"}' in src, (
        "agent_local must not be runner-locked")
    assert '_NO_HUB_BACKENDS = {"local", "agent_local"}' in src


# ── The agent side: the grant, and the second path check ─────────────────────

def _policy(body=""):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "policy.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""
            targets:
              - cidr: 10.20.0.0/24
                ports: [443]
            job_types: [agent_discover, agent_storage]
        """) + textwrap.dedent(body))
    return AGENT.Policy.load(p)


def test_the_agent_refuses_a_share_with_no_storage_block():
    """Fails closed like every other grant in that file: an absent block is not an
    implicit yes."""
    try:
        _policy().check_share("playbooks", write=False)
        raise AssertionError("an ungranted share was allowed")
    except AGENT.PolicyRefusal as exc:
        assert "storage:" in str(exc)


def test_write_is_a_separate_grant_and_defaults_to_off():
    """The common case is a share somebody else populates and the dashboard only reads.
    An operator who enables the block without reading gets that, not a writable share."""
    pol = _policy("""
        storage:
          enabled: true
          shares:
            - name: playbooks
    """)
    pol.check_share("playbooks", write=False)
    try:
        pol.check_share("playbooks", write=True)
        raise AssertionError("write was allowed on a read-only share")
    except AGENT.PolicyRefusal as exc:
        assert "read-only" in str(exc)

    pol = _policy("""
        storage:
          enabled: true
          shares:
            - name: playbooks
              write: true
    """)
    pol.check_share("playbooks", write=True)


def test_an_ungranted_share_name_is_refused_by_name():
    pol = _policy("""
        storage:
          enabled: true
          shares:
            - name: playbooks
    """)
    try:
        pol.check_share("secrets", write=False)
        raise AssertionError("an ungranted share was allowed")
    except AGENT.PolicyRefusal as exc:
        assert "secrets" in str(exc)


def test_a_malformed_storage_block_is_fatal_not_skipped():
    """A share entry silently dropped for being the wrong shape is a share the
    dashboard then reports as ungranted, which sends the operator to add a grant that
    is already there."""
    for body in ("storage: yes\n",
                 "storage:\n  enabled: true\n  shares: playbooks\n",
                 "storage:\n  enabled: true\n  shares:\n    - write: true\n",
                 "storage:\n  enabled: true\n  shares:\n    - name: p\n      write: 'false'\n"):
        try:
            _policy(body)
            raise AssertionError(f"a malformed storage block was accepted: {body!r}")
        except AGENT.AgentFatal:
            pass


def test_a_string_write_is_refused_rather_than_coerced():
    """`write: "false"` is truthy in Python, so accepting it would grant the opposite of
    what the operator wrote."""
    try:
        _policy("storage:\n  enabled: true\n  shares:\n    - name: p\n      write: 'true'\n")
        raise AssertionError("a string write flag was accepted")
    except AGENT.AgentFatal as exc:
        assert "true or false" in str(exc)


def test_the_agent_checks_the_path_itself():
    """The dashboard validated the name before queueing, but an agent that trusts the
    dashboard's validation has file access only as good as the dashboard's — and the
    whole point of policy.yaml is that it is not."""
    share = {"name": "p", "path": tempfile.gettempdir()}
    for bad in ("../escape.yml", "sub/x.yml", ".ssh", ""):
        try:
            AGENT._share_path(share, "", bad)
            raise AssertionError(f"the agent accepted {bad!r} as a file name")
        except AGENT.PolicyRefusal:
            pass
    assert AGENT._share_path(share, "", "site.yml").endswith("site.yml")


def test_a_unc_share_keeps_its_prefix():
    """A leading double separator is what makes it a UNC path at all; losing one turns
    it into an absolute path on the agent's own root."""
    root = (chr(92) * 2) + "fs01" + chr(92) + "automation"
    share = {"name": "u", "path": root}
    assert AGENT._share_is_unc(root)
    assert AGENT._share_path(share, "win", "site.yml").startswith(root)
    assert AGENT._share_dir(share, "") == root


def test_the_four_operations_round_trip_on_a_real_directory():
    """End to end against the plain-filesystem branch, which is also the branch an
    operator gets when they bind-mount the share instead of using SMB."""
    work = tempfile.mkdtemp()
    root = os.path.join(work, "playbooks")
    os.makedirs(root)
    with open(os.path.join(root, "existing.yml"), "w", encoding="utf-8") as fh:
        fh.write("- hosts: all\n")
    # Not an asset extension — it must never appear in a listing.
    with open(os.path.join(root, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("x\n")

    shares_path = os.path.join(work, "shares.yaml")
    with open(shares_path, "w", encoding="utf-8") as fh:
        fh.write("shares:\n  - name: playbooks\n    path: %s\n"
                 % root.replace(os.sep, "/"))
    AGENT.SHARES_FILE = shares_path

    pol = _policy("""
        storage:
          enabled: true
          shares:
            - name: playbooks
              write: true
    """)
    run = lambda p: AGENT.run_storage(p, pol, lambda _l: None, lambda: False, "j", None)

    assert [a["name"] for a in run({"op": "list", "share": "playbooks"})["assets"]] \
        == ["existing.yml"]
    import base64
    fetched = run({"op": "fetch", "share": "playbooks", "name": "existing.yml"})
    assert base64.b64decode(fetched["content_b64"]).startswith(b"- hosts: all")
    run({"op": "upload", "share": "playbooks", "name": "new.sh",
         "content_b64": base64.b64encode(b"echo hi\n").decode()})
    assert sorted(a["name"] for a in run({"op": "list", "share": "playbooks"})["assets"]) \
        == ["existing.yml", "new.sh"]
    run({"op": "delete", "share": "playbooks", "name": "new.sh"})
    # Deleting something already gone is the outcome the caller asked for. Reporting it
    # as a failure would leave the asset list showing a file no operator can clear.
    run({"op": "delete", "share": "playbooks", "name": "new.sh"})
    assert [a["name"] for a in run({"op": "list", "share": "playbooks"})["assets"]] \
        == ["existing.yml"]


def test_a_share_with_no_path_is_fatal_at_load():
    """A share entry with no path is one every job against it fails on, one job at a
    time, with an error that names the file rather than the entry."""
    work = tempfile.mkdtemp()
    p = os.path.join(work, "shares.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("shares:\n  - name: broken\n")
    try:
        AGENT.FileShares.load(p)
        raise AssertionError("a share with no path was accepted")
    except AGENT.AgentFatal as exc:
        assert "broken" in str(exc)


# ── The bundle deadlock ──────────────────────────────────────────────────────

def test_an_agent_run_prefetches_its_asset_instead_of_reading_it_from_the_bundle():
    """The one place the two features collide, and it deadlocks if you miss it.

    `agent_ansible_bundle.build` runs inside POST /api/agent/jobs/{id}/ansible-bundle,
    which the agent calls while it is RUNNING that job and blocked on the response. If
    the bundle fetched an agent-brokered asset there, it would queue an `agent_storage`
    job for an agent that cannot lease anything until the request it is waiting on
    returns — so it would sit until the storage deadline expired and then fail the run,
    blaming storage. One agent, one share, playbooks run on that network is the ordinary
    setup, not a corner case.

    Source-level because reproducing it needs a live agent: the assertion is that the
    enqueue path reads the asset and the bundle path prefers those bytes.
    """
    with open(os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py"),
              encoding="utf-8") as fh:
        enqueue = fh.read()
    assert 'if asset_backend == "agent_local":' in enqueue, (
        "the agent_ansible enqueue path no longer prefetches an agent-brokered asset")
    assert 'meta["asset_bytes_b64"]' in enqueue

    with open(os.path.join(_ROOT, "web_dashboard", "services",
                           "agent_ansible_bundle.py"), encoding="utf-8") as fh:
        bundle = fh.read()
    assert "prefetched_b64" in bundle, (
        "the bundle builder ignores the prefetched asset and would fetch it itself")
    # Off the RAW metadata: run_kwargs normalises to the closed envelope key set, which
    # these bytes are deliberately not in.
    assert 'raw_meta.get("asset_bytes_b64")' in bundle


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
    print("OK" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
