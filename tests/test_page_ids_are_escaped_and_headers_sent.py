"""An id from the URL can only ever be a string to the page, and every page sends a CSP.

``/jobs/<id>`` rendered ``x-data="jobDetail('{{ job_id }}')"``. Jinja escapes ``'`` to
``&#39;``, but the browser decodes the attribute before Alpine evaluates it, so
``/jobs/x'%29%3Balert(1)%3B('`` ran script -- and the session token sits in localStorage,
so one link was an account. ``/pov/<id>`` had the same shape. Both now render the id with
``tojson`` inside a single-quoted attribute, where it is a JS string literal whatever it
contains.

Every response also carries security headers (main.py ``_SECURITY_HEADERS``): a CSP that
forbids framing, plugins, foreign <base>/<form> targets and connections to other origins,
plus nosniff and a referrer policy, and HSTS on HTTPS.

Runs standalone:
    python tests/test_page_ids_are_escaped_and_headers_sent.py
"""
import html
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="page-escape-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "page-escape-test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard import main  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import config_service  # noqa: E402

Base.metadata.create_all(bind=engine)
config_service.is_setup_complete = lambda: True

# No "/" in any of them: Starlette decodes %2F before routing, so a slash never reaches
# {job_id} -- it would be a different path, not a harder payload.
PAYLOADS = ("x');alert(1);('", 'x"><img src=x onerror=alert(1)>', "x\\');alert(1);('")


def _x_data(page, fn):
    m = re.search(r"x-data='" + fn + r"\((.*?)\)'", page, re.S)
    assert m, f"no single-quoted x-data='{fn}(...)' on the page"
    return html.unescape(m.group(1))


def _is_one_js_string(expr):
    """True if expr is exactly one double-quoted JSON string literal and nothing else."""
    import json
    try:
        return isinstance(json.loads(expr), str)
    except ValueError:
        return False


def test_the_job_page_id_is_a_string_literal_whatever_it_contains():
    client = TestClient(main.app)
    for payload in PAYLOADS:
        from urllib.parse import quote
        r = client.get("/jobs/" + quote(payload, safe=""))
        assert r.status_code == 200, r.status_code
        expr = _x_data(r.text, "jobDetail")
        assert _is_one_js_string(expr), f"{payload!r} rendered as {expr!r}"
        assert "<img src=x" not in r.text


def test_the_pov_page_uses_the_same_safe_form():
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "detail.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "x-data='povDetail({{ env_id|tojson }})'" in src
    assert "povDetail('{{ env_id }}')" not in src


def test_no_template_quotes_a_path_id_into_an_alpine_expression():
    """The pattern itself: a {{ }} value inside a quoted JS string in x-data."""
    bad = []
    tdir = os.path.join(_ROOT, "web_dashboard", "templates")
    for base, _d, files in os.walk(tdir):
        for name in files:
            if name.endswith(".html"):
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if re.search(r"x-data=\"[^\"]*'\{\{", line):
                            bad.append(f"{name}:{n}")
    assert not bad, f"x-data quotes a template value as a JS string; use |tojson: {bad}"


def test_every_response_carries_the_security_headers():
    client = TestClient(main.app)
    for path in ("/login", "/api/health"):
        h = client.get(path).headers
        csp = h.get("content-security-policy", "")
        for directive in ("frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'",
                          "connect-src 'self'", "form-action 'self'"):
            assert directive in csp, f"{path}: CSP lacks {directive!r}: {csp!r}"
        assert h.get("x-frame-options") == "DENY"
        assert h.get("x-content-type-options") == "nosniff"
        assert "strict-transport-security" not in h, "HSTS sent over plain HTTP"


def test_hsts_is_sent_over_https():
    client = TestClient(main.app, base_url="https://testserver")
    assert "max-age=" in client.get("/login").headers.get("strict-transport-security", "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
