"""The login page offers only the single sign-on it can actually complete.

The Sign in with Microsoft button used to render unconditionally, so a default install,
or one that had moved to generic OIDC, showed it anyway, and clicking it ended on a bare
501 from ``/api/auth/oauth/azure/login``. docs/oidc/entra-oauth.md already said the button
appears once Entra OAuth is saved; this pins the page to that.

The error messages are shared too: the OIDC callback reuses ``not_registered``,
``oauth_error``, ``token_error`` and ``no_email``, so an Okta user was told their
*Microsoft* account was not registered.

Renders the template with plain Jinja -- no app import -- so it never skips.

Run: python tests/test_login_sso_buttons.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")
_LOGIN = os.path.join(_TEMPLATES, "login.html")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")

MS = "/api/auth/oauth/azure/login"
OIDC = "/api/auth/oauth/oidc/login"


class _Anything(str):
    """Stands in for the theme/branding globals: any attribute, any item, renders empty."""
    def __getattr__(self, name):
        return _Anything()

    def __getitem__(self, key):
        return _Anything()

    def __call__(self, *a, **k):
        return _Anything()


def _render(oidc_enabled, entra_oauth_enabled):
    from jinja2 import Environment, FileSystemLoader, Undefined
    env = Environment(loader=FileSystemLoader(_TEMPLATES), undefined=Undefined)
    env.globals.update({k: _Anything() for k in ("theme", "brand", "branding", "static_url",
                                                 "url_for", "app_env")})

    class _Lenient(Undefined):
        def __getattr__(self, name):
            return _Anything()
    env.undefined = _Lenient
    return env.get_template("login.html").render(
        request=None, oidc_enabled=oidc_enabled, oidc_label="Okta",
        entra_oauth_enabled=entra_oauth_enabled)


def _form(html):
    """The password step's form, where the SSO buttons live."""
    return html.split("<form", 1)[1].split("</form>", 1)[0]


def test_no_sso_configured_shows_no_sso_buttons_and_no_divider():
    form = _form(_render(False, False))
    assert MS not in form, "Sign in with Microsoft shows with Entra OAuth unconfigured"
    assert OIDC not in form
    assert ">or<" not in form, "the 'or' divider shows with nothing after it"


def test_oidc_alone_shows_only_the_oidc_button():
    form = _form(_render(True, False))
    assert OIDC in form and "Sign in with Okta" in form
    assert MS not in form, "the legacy Microsoft button shows beside the OIDC default"
    assert ">or<" in form


def test_entra_alone_shows_only_the_microsoft_button():
    form = _form(_render(False, True))
    assert MS in form and OIDC not in form
    assert ">or<" in form


def test_both_configured_puts_the_oidc_default_first():
    form = _form(_render(True, True))
    assert form.index(OIDC) < form.index(MS)


def test_the_route_passes_the_entra_flag_from_the_same_check_the_redirect_makes():
    src = open(_MAIN, encoding="utf-8").read()
    route = src.split('@app.get("/login"', 1)[1].split("\n@app.", 1)[0]
    assert "entra_oauth_enabled" in route
    assert "auth._oauth_cfg()" in route, (
        "the button must follow the client id + tenant check in oauth_azure_login, "
        "or it can show for a login that 501s")


def test_sso_error_messages_name_no_provider():
    """Both callbacks redirect with these codes, so naming Microsoft is wrong half the time."""
    page = open(_LOGIN, encoding="utf-8").read()
    messages = page.split("const messages = {", 1)[1].split("};", 1)[0]
    messages = re.sub(r"(?m)^\s*//.*$", "", messages)
    assert "Microsoft" not in messages, "an SSO error message names Microsoft"
    for code in ("not_registered", "not_authorized", "account_disabled", "oauth_error",
                 "invalid_state", "no_code", "no_email", "token_error"):
        assert re.search(rf"\b{code}:", messages), f"no message for ?error={code}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
