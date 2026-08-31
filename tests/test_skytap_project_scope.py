"""Scoping Skytap reads and creates to a project.

`skytap_project_id` shipped as a field in Settings, a key in config.py, and two paragraphs
of documentation — including a troubleshooting remedy, "clear the Project ID to widen the
scope" — while being read by NO code at all. The listing calls were never scoped, so the
setting could not narrow anything and the remedy could not widen anything. This is the
change that makes the label true.

What is pinned here:

  * **No project means the flat, account-wide paths**, byte for byte what shipped before —
    the risk of the new sub-resource paths only ever applies to someone who sets one.
  * **A project scopes BOTH listings**, or "clear it to widen the scope" stays a lie.
  * **A project id Skytap cannot see becomes a remedy, not a 502.** Left raw, a stale
    Project ID turns today's harmless empty list into a hard failure on the POV page whose
    cause is named nowhere.
  * **A new environment inherits the configured project, and an explicit one still wins** —
    the difference between a default and an override nobody can escape.
  * **The unverified paths stay flagged.** They are assumed, not confirmed, and this repo's
    convention is to name such a path once in a comment rather than let it look settled.

Uses httpx.MockTransport against the `skytap_service` seam. No network, no app, no
database.

Runs under pytest, or standalone:
    python tests/test_skytap_project_scope.py
"""
import asyncio
import json as _json
import os
import pathlib
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-skytap-project")

try:
    import httpx
except ImportError:  # pragma: no cover
    print("SKIP: httpx not installed")
    sys.exit(0)

from web_dashboard.services import skytap_service as sk  # noqa: E402


class _Recorder:
    def __init__(self):
        self.requests = []


def _patch(handler, *, project=""):
    """Point the adapter at a canned handler with a chosen project id.

    Patches `_cfg` rather than writing config rows — the suite shares a real `vm_cli.db`
    and the developer's `.env`, so writing config here would mutate the dev install.
    """
    rec = _Recorder()

    def _wrapped(request):
        rec.requests.append(request)
        return handler(request)

    original_cfg, original_cls = sk._cfg, sk.SkytapClient

    def _cfg(key):
        if key == "skytap_project_id":
            return project
        return {"skytap_username": "u", "skytap_api_token": "t",
                "skytap_base_url": "https://skytap.test"}.get(key, "")

    async def _sleep(_s):
        pass

    def _cls(creds, **kw):
        kw.pop("transport", None)
        kw.setdefault("sleep", _sleep)
        return original_cls(creds, transport=httpx.MockTransport(_wrapped), **kw)

    sk._cfg, sk.SkytapClient = _cfg, _cls
    return rec, (original_cfg, original_cls)


def _restore(saved):
    sk._cfg, sk.SkytapClient = saved


def _run(coro_fn, handler, *, project=""):
    rec, saved = _patch(handler, project=project)
    try:
        return asyncio.run(coro_fn()), rec
    finally:
        _restore(saved)


def _ok(payload):
    return lambda r: httpx.Response(200, json=payload)


# ── the default: unchanged ───────────────────────────────────────────────────

def test_no_project_lists_the_whole_account():
    """The path that shipped. Someone who never sets a Project ID sees no change at all,
    which is what keeps the unverified sub-resource paths off their critical path."""
    _, rec = _run(sk.list_templates, _ok([{"id": "1", "name": "t"}]))
    assert rec.requests[0].url.path == "/v2/templates"

    _, rec = _run(sk.list_environments, _ok([{"id": "9", "name": "e"}]))
    assert rec.requests[0].url.path == "/v2/configurations"


def test_configured_project_id_is_blank_by_default():
    _, saved = _patch(_ok([]))
    try:
        assert sk.configured_project_id() == ""
    finally:
        _restore(saved)


# ── scoped ───────────────────────────────────────────────────────────────────

def test_a_configured_project_scopes_the_template_list():
    _, rec = _run(sk.list_templates, _ok([{"id": "1"}]), project="123456")
    assert rec.requests[0].url.path == "/v2/projects/123456/templates"


def test_a_configured_project_scopes_the_environment_list():
    """Both, or 'clear the Project ID to widen the scope' is only half true."""
    _, rec = _run(sk.list_environments, _ok([{"id": "9"}]), project="123456")
    assert rec.requests[0].url.path == "/v2/projects/123456/configurations"


def test_whitespace_around_a_project_id_does_not_reach_the_url():
    _, saved = _patch(_ok([]), project="  123456  ")
    try:
        assert sk._templates_path() == "/v2/projects/123456/templates"
    finally:
        _restore(saved)


def test_scoped_reads_still_carry_keep_idle():
    _, rec = _run(sk.list_templates, _ok([{"id": "1"}]), project="123456")
    assert "keep_idle=true" in str(rec.requests[0].url)


# ── a bad project id ─────────────────────────────────────────────────────────

def test_a_project_skytap_cannot_see_says_to_clear_it():
    """Left raw this is a bare 502 on the POV page via `_platform_error`, with the real
    cause named nowhere — a regression on today's harmless empty list."""
    raw_body = "the-raw-skytap-404-body"

    def handler(_r):
        return httpx.Response(404, text=raw_body)

    try:
        _run(sk.list_templates, handler, project="999999")
    except sk.SkytapError as exc:
        msg = str(exc)
        assert "999999" in msg, "the message does not name the project"
        assert "Project ID" in msg and "Settings" in msg, "no remedy named"
        assert "blank" in msg.lower(), "it should say what clearing it does"
        assert raw_body not in msg, "the raw upstream body was passed through"
    else:  # pragma: no cover
        raise AssertionError("a 404 on a scoped read did not raise")


def test_a_404_without_a_project_is_left_alone():
    """Only the project case gets the rewrite. A 404 with no project configured means
    something else, and dressing it up as a Project ID problem would send the reader to a
    field they never set."""
    try:
        _run(sk.list_templates, lambda r: httpx.Response(404, text="nope"))
    except sk.SkytapError as exc:
        assert "Project ID" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 404 did not raise")


def test_other_failures_are_not_dressed_up_as_a_project_problem():
    try:
        _run(sk.list_templates, lambda r: httpx.Response(500, text="boom"),
             project="123456")
    except sk.SkytapError as exc:
        assert "Project ID" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 500 did not raise")


# ── creating into a project ──────────────────────────────────────────────────

def _capture_create():
    bodies = []

    def handler(request):
        if request.method == "POST":
            bodies.append(_json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"id": "42", "runstate": "stopped"})
        return httpx.Response(200, json={"id": "42", "runstate": "stopped"})

    return handler, bodies


def test_the_environment_create_posts_to_the_v1_collection():
    """Skytap has no POST on `/v2/configurations`.

    It answers `404 {"error":"Not Found"}` there, which reads exactly like "that template
    id does not exist" — the first live template build spent its time on the template
    field. Environments are created only by the v1 `POST /configurations.json`.
    """
    paths = []

    def handler(request):
        if request.method == "POST":
            paths.append(request.url.path)
        return httpx.Response(200, json={"id": "42", "runstate": "stopped"})

    _run(lambda: sk.create_environment("tmpl-1"), handler)
    assert paths == ["/configurations.json"], paths


def test_a_new_environment_inherits_the_configured_project():
    handler, bodies = _capture_create()
    _run(lambda: sk.create_environment("tmpl-1"), handler, project="123456")
    assert bodies and bodies[0].get("project_id") == "123456", bodies


def test_an_explicit_project_beats_the_configured_default():
    """A default you cannot escape is an override, and the field is documented as a
    default."""
    handler, bodies = _capture_create()
    _run(lambda: sk.create_environment("tmpl-1", project_id="777"), handler,
         project="123456")
    assert bodies[0].get("project_id") == "777", bodies


def test_no_project_anywhere_sends_no_project_id():
    """Skytap decides where it lands; sending an empty string would be asserting a scope
    nobody chose."""
    handler, bodies = _capture_create()
    _run(lambda: sk.create_environment("tmpl-1"), handler)
    assert "project_id" not in bodies[0], bodies


# ── the convention ───────────────────────────────────────────────────────────

def test_the_project_paths_are_flagged_unverified():
    """These paths are assumed, not confirmed. The house convention is to name such a path
    ONCE in a comment rather than let it read as settled — `_vm_ref` for publish sets is
    the existing example. Reading source to pin a convention is what
    `test_lab_platforms.test_the_registry_imports_no_adapter_at_module_scope` already
    does."""
    src = (pathlib.Path(_ROOT) / "web_dashboard" / "services"
           / "skytap_service.py").read_text(encoding="utf-8")
    head = src.split("def _templates_path", 1)[0]
    tail = head.rsplit("UNVERIFIED", 1)
    assert len(tail) == 2, "the project paths carry no UNVERIFIED marker"
    assert len(tail[1]) < 1200, \
        "the UNVERIFIED note is not the comment immediately above the path helpers"


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
