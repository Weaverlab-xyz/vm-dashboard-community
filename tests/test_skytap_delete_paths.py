"""Deleting a Skytap environment: the right endpoint, and a 404 that is never taken on trust.

This exists because of a live failure with no symptom except the invoice. `delete_environment`
sent `DELETE /v2/configurations/{id}` — a path Skytap answers `404 {"error":"Not Found"}` for
every id, including one that is plainly running — and the adapter read that 404 as "somebody
already deleted it". Six POV environments outlived their POVs while the dashboard marked every
destroy `destroyed`, wrote "The infrastructure is gone" on the archive page, and deleted the VM
rows that were the only record of what had been in them.

Two separate mistakes, so two separate things pinned:

  * **The environment delete is v1.** `/configurations/{id}.json`, the same v1 resource the
    create posts to and `add_vms` puts to. v2's coverage of the top-level objects is per-verb:
    `GET`/`PUT` on `/v2/configurations/{id}` work, `DELETE` does not, and on
    `/v2/templates/{id}` only `GET` does. No test asserted the path, which is exactly how the
    wrong one shipped.
  * **A 404 on a delete is not proof the thing is gone.** The deletes stay idempotent — a
    teardown that fails on "it is already gone" leaves a row nobody can clean up — but the
    evidence has to be a direct READ of the object. A 404 on the delete plus a 200 on the read
    means the endpoint was wrong, and that must fail loudly: the destroy job records it as a
    problem an operator can re-run, where a silent success cannot be undone.

The same rule and the same path change apply to `delete_template`.

Uses httpx.MockTransport against the `skytap_service._client` seam, following
test_skytap_template_authoring. No network, no app, no database.

Runs under pytest, or standalone:
    python tests/test_skytap_delete_paths.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-skytap-delete")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import skytap_service as sk  # noqa: E402


def _patch(handler):
    """Point the adapter at a canned handler.

    Patches `_cfg` rather than writing config rows, for the reason test_skytap_verify gives:
    the suite shares a real db and .env, so a test that writes config mutates the dev install.
    """
    calls = []

    def _wrapped(request):
        calls.append((request.method, request.url.path))
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
    return calls, saved


def _run(handler, coro_factory):
    calls, saved = _patch(handler)
    try:
        return asyncio.run(coro_factory()), calls
    finally:
        sk._cfg, sk.SkytapClient = saved


# ── the environment delete ───────────────────────────────────────────────────

def test_delete_environment_uses_the_v1_path():
    """The whole bug in one assertion: v2 answers 404 here, for every id."""
    def handler(request):
        assert request.method == "DELETE", request.method
        assert request.url.path == "/configurations/223283274.json", request.url.path
        return httpx.Response(200, json={})

    _, calls = _run(handler, lambda: sk.delete_environment("223283274"))
    # One call. A delete that worked must not go on to read the object back.
    assert calls == [("DELETE", "/configurations/223283274.json")], calls


def test_a_204_is_a_successful_delete():
    """Skytap answers some deletes with an empty body; `_decode` returns None for it."""
    _run(lambda r: httpx.Response(204), lambda: sk.delete_environment("9"))


def test_a_404_that_a_read_confirms_is_silence():
    """Genuinely already gone: idempotent, because a teardown that cannot succeed twice
    leaves a row nobody can ever clean up."""
    def handler(request):
        return httpx.Response(404, json={"error": "Not Found"})

    _, calls = _run(handler, lambda: sk.delete_environment("9"))
    # The read is what supplied the evidence, and it is a GET of the object itself.
    assert calls == [("DELETE", "/configurations/9.json"),
                     ("GET", "/v2/configurations/9")], calls


def test_a_404_the_read_contradicts_raises():
    """THE REGRESSION TEST. A 404 on the delete and a live object on the read means the
    endpoint was wrong — and reporting that as a successful destroy is what stranded six
    running environments."""
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": "Not Found"})
        return httpx.Response(200, json={"id": "9", "name": "poc-weaver",
                                         "runstate": "running"})

    try:
        _run(handler, lambda: sk.delete_environment("9"))
    except sk.SkytapError as exc:
        # The remedy has to be inside the string: a failed job surfaces nothing else.
        assert "still there" in str(exc), exc
        assert "ENDPOINT" in str(exc), exc
    else:
        raise AssertionError(
            "a 404 on the delete with the environment still readable must raise")


def test_a_read_that_cannot_answer_does_not_get_guessed():
    """A 500 on the confirming read is not evidence of anything. Re-raised, so the destroy
    records a problem and can be re-run — the same refusal to guess `pov_reconcile._confirm_
    missing` makes about a listing."""
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": "Not Found"})
        return httpx.Response(500, text="boom")

    try:
        _run(handler, lambda: sk.delete_environment("9"))
    except sk.SkytapError:
        pass
    else:
        raise AssertionError("an unreadable confirmation must not be read as success")


def test_delete_environment_still_raises_on_a_real_failure():
    def handler(request):
        return httpx.Response(500, text="boom")

    try:
        _run(handler, lambda: sk.delete_environment("9"))
    except sk.SkytapError:
        pass
    else:
        raise AssertionError("a 500 on delete must still raise")


def test_delete_environment_requires_an_id():
    """Without this, an empty id would DELETE `/configurations/.json` — a call at the
    collection, with whatever that means on Skytap's side."""
    for bad in ("", "   ", None):
        try:
            _run(lambda r: httpx.Response(200, json={}),
                 lambda: sk.delete_environment(bad))
        except sk.SkytapError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been refused")


# ── the template delete ─────────────────────────────────────────────────────

def test_delete_template_uses_the_v1_path():
    """`/v2/templates/{id}` serves GET and nothing else on this account — the description
    PUT in `create_template` answers 404 there."""
    def handler(request):
        assert request.url.path == "/templates/77.json", request.url.path
        return httpx.Response(200, json={})

    _, calls = _run(handler, lambda: sk.delete_template("77"))
    assert calls == [("DELETE", "/templates/77.json")], calls


def test_delete_template_confirms_a_404_before_believing_it():
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(404, json={"error": "Not Found"})
        return httpx.Response(200, json={"id": "77", "name": "saas-base"})

    try:
        _run(handler, lambda: sk.delete_template("77"))
    except sk.SkytapError as exc:
        assert "still there" in str(exc), exc
    else:
        raise AssertionError("a template that survived its delete must raise")


def test_delete_template_is_still_idempotent_when_it_is_really_gone():
    _, calls = _run(lambda r: httpx.Response(404, json={"error": "Not Found"}),
                    lambda: sk.delete_template("77"))
    assert calls == [("DELETE", "/templates/77.json"),
                     ("GET", "/v2/templates/77")], calls


# ── the sibling call the same 404 broke ─────────────────────────────────────

def test_the_template_description_put_is_v1_too():
    """Logged live as `PUT /v2/templates/{id} -> 404`, which meant no template this
    dashboard ever baked got the description it was given. Non-fatal either way: the
    template exists and its id is what everything keys on."""
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "77", "name": "saas-base"})
        return httpx.Response(200, json={"id": "77", "name": "saas-base",
                                         "description": "a description"})

    out, calls = _run(handler,
                      lambda: sk.create_template("env-1", "saas-base", "a description"))
    assert ("PUT", "/templates/77.json") in calls, calls
    assert out["description"] == "a description", out


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
