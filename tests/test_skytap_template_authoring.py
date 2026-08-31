"""The Skytap write surface a template builder needs: baking templates, publishing ports.

Until this landed the adapter could only *read* the catalogue, which left the whole POV
feature downstream of templates authored by hand in Skytap's own console. What is pinned
here is the part that is silent when wrong:

  * **A create that returns no id is a failure, not a success.** Skytap answering 200 with
    an empty body would otherwise leave a template in the account that nothing here knows
    about — the orphan `create_environment` already refuses to make.
  * **A description failure does not lose the template.** The description is a second PUT,
    and stranding a real template over a cosmetic field is the wrong trade.
  * **Deletes are idempotent on 404.** A teardown that fails because the thing is already
    gone leaves a row nobody can ever clean up.
  * **A published service with no external address is refused.** Handing a caller an
    ip:port of `None` produces an SSH attempt to nowhere and a message about the wrong
    thing entirely.
  * **`_vm()` carries interface ids.** Publishing is a POST *under* an interface, so a VM
    shape without them cannot be published to at all.

Uses httpx.MockTransport against the `skytap_service._client` seam, following
test_skytap_verify. No network, no app, no database.

Runs under pytest, or standalone:
    python tests/test_skytap_template_authoring.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-skytap-authoring")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import skytap_service as sk  # noqa: E402


class _Recorder:
    def __init__(self):
        self.requests = []


def _patch(handler):
    """Point the adapter at a canned handler. Patches `_cfg` rather than writing config
    rows, for the reason test_skytap_verify gives: the suite shares a real db and .env, so
    a test that writes config mutates the dev install."""
    rec = _Recorder()

    def _wrapped(request):
        rec.requests.append(request)
        return handler(request)

    saved = (sk._cfg, sk.SkytapClient)
    original_cls = sk.SkytapClient

    def _cfg(key):
        return {"skytap_username": "u", "skytap_api_token": "t",
                "skytap_base_url": "https://skytap.test"}.get(key, "")

    async def _sleep(_s):
        pass

    def _cls(creds, **kw):
        kw.pop("transport", None)
        kw.setdefault("sleep", _sleep)
        return original_cls(creds, transport=httpx.MockTransport(_wrapped), **kw)

    sk._cfg, sk.SkytapClient = _cfg, _cls
    return rec, saved


def _run(handler, coro_factory):
    rec, saved = _patch(handler)
    try:
        return asyncio.run(coro_factory()), rec
    finally:
        sk._cfg, sk.SkytapClient = saved


# ── create_template ──────────────────────────────────────────────────────────

def test_create_template_posts_the_configuration_id():
    def handler(request):
        assert request.method == "POST", request.method
        # v1: there is no POST on the v2 templates collection, and posting to it
        # answers 404 {"error":"Not Found"}.
        assert request.url.path == "/templates.json", request.url.path
        import json
        body = json.loads(request.content)
        assert body["configuration_id"] == "env-1", body
        assert body["name"] == "saas-base", body
        return httpx.Response(200, json={"id": 77, "name": "saas-base"})

    out, _ = _run(handler, lambda: sk.create_template("env-1", "saas-base"))
    # The id is normalised to a string: Skytap returns them numeric in some payloads and
    # string in others, and 42 != "42" is a lookup that finds nothing.
    assert out["id"] == "77", out


def test_create_template_without_an_id_is_a_failure():
    """A 200 with no id would otherwise leave an orphan nobody can attribute."""
    def handler(request):
        return httpx.Response(200, json={"name": "saas-base"})

    try:
        _run(handler, lambda: sk.create_template("env-1", "saas-base"))
    except sk.SkytapError as exc:
        assert "orphan" in str(exc), exc
    else:
        raise AssertionError("a create returning no template id must raise")


def test_a_failed_description_does_not_lose_the_template():
    """The description is a second PUT. Failing it must not strand a real template."""
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "77", "name": "saas-base"})
        return httpx.Response(500, text="nope")

    out, _ = _run(handler,
                  lambda: sk.create_template("env-1", "saas-base", "a description"))
    assert out["id"] == "77", out
    assert ("PUT", "/v2/templates/77") in calls, calls


def test_create_template_requires_an_environment_and_a_name():
    for env_id, name in (("", "x"), ("env-1", "")):
        try:
            _run(lambda r: httpx.Response(200, json={"id": "1"}),
                 lambda: sk.create_template(env_id, name))
        except sk.SkytapError:
            pass
        else:
            raise AssertionError(f"({env_id!r}, {name!r}) should have been refused")


# ── get_template ─────────────────────────────────────────────────────────────

def test_get_template_returns_vms_and_an_authoritative_count():
    def handler(request):
        return httpx.Response(200, json={
            "id": "9", "name": "base", "vm_count": 99,
            "vms": [{"id": "1", "name": "broker"}, {"id": "2", "name": "app"}]})

    out, rec = _run(handler, lambda: sk.get_template("9"))
    assert [v["name"] for v in out["vms"]] == ["broker", "app"], out
    # The nested read is authoritative where the collection read was a guess.
    assert out["vm_count"] == 2, out
    assert "keep_idle=true" in str(rec.requests[0].url)


# ── delete_template ──────────────────────────────────────────────────────────

def test_delete_template_is_idempotent_on_404():
    def handler(request):
        return httpx.Response(404, text="not found")

    # No raise: a teardown that fails on "already gone" leaves a row nobody can clean up.
    _run(handler, lambda: sk.delete_template("9"))


def test_delete_template_still_raises_on_a_real_failure():
    def handler(request):
        return httpx.Response(500, text="boom")

    try:
        _run(handler, lambda: sk.delete_template("9"))
    except sk.SkytapError:
        pass
    else:
        raise AssertionError("a 500 on delete must still raise")


# ── published services ───────────────────────────────────────────────────────

def test_publish_service_posts_under_the_interface():
    def handler(request):
        assert request.url.path == \
            "/v2/configurations/env-1/vms/vm-2/interfaces/nic-3/services", request.url.path
        return httpx.Response(200, json={"id": "s1", "internal_port": 22,
                                         "external_ip": "203.0.113.9",
                                         "external_port": 40022})

    out, _ = _run(handler, lambda: sk.publish_service("env-1", "vm-2", "nic-3", 22))
    assert out == {"id": "s1", "internal_port": 22, "external_ip": "203.0.113.9",
                   "external_port": 40022}, out


def test_publish_service_without_an_external_address_is_refused():
    """A service with no ip:port would produce an SSH attempt to nowhere, and an error
    message about the wrong thing entirely."""
    def handler(request):
        return httpx.Response(200, json={"id": "s1", "internal_port": 22})

    try:
        _run(handler, lambda: sk.publish_service("env-1", "vm-2", "nic-3", 22))
    except sk.SkytapError as exc:
        assert "external address" in str(exc), exc
    else:
        raise AssertionError("a published service with no address must raise")


def test_delete_published_service_is_idempotent_on_404():
    def handler(request):
        return httpx.Response(404)

    # It runs in a caller's `finally`, where raising would replace the real failure.
    _run(handler, lambda: sk.delete_published_service("e", "v", "n", "s"))


# ── the VM shape ─────────────────────────────────────────────────────────────

def test_vm_carries_interface_ids_and_network_types():
    """Publishing is a POST under an interface, so a VM shape with no interface ids cannot
    be published to. The network type is what the contract check reads."""
    vm = sk._vm({
        "id": "1", "name": "broker", "runstate": "running",
        "interfaces": [{
            "id": "nic-3", "ip": "10.0.0.5", "nic_type": "vmxnet3",
            "network_id": "net-1", "network": {"id": "net-1",
                                               "network_type": "automatic"},
            "services": [{"id": "s1", "internal_port": 22,
                          "external_ip": "203.0.113.9", "external_port": 40022}],
        }],
    })
    assert vm["private_ip"] == "10.0.0.5", vm
    assert vm["interfaces"][0]["id"] == "nic-3", vm
    assert vm["interfaces"][0]["network_type"] == "automatic", vm
    # The flattened list every existing caller renders is unchanged.
    assert vm["published_services"][0]["external_port"] == 40022, vm


def test_vm_with_no_interfaces_still_maps():
    vm = sk._vm({"id": "1", "name": "app"})
    assert vm["interfaces"] == [], vm
    assert vm["published_services"] == [], vm
    assert vm["private_ip"] == "", vm


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
