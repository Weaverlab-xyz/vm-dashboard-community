"""Two ways a Rancher/Portainer Web Jump silently stops working, and neither is visible
in a diff or reachable by any test that imports the services.

Both nodes are EPHEMERAL by design — the pages say so — so a stop/recreate or a
relocation hands them a new public IP. A PRA Web Jump, though, carries the URL it was
created with. That leaves two independent gaps, and on 2026-08-17 they compounded into
an afternoon of chasing a firewall that was already correct:

1. **The registration early-returned on the stored id.** `register_*_ui_web_jump` is
   called on every deploy and re-syncs the gateway /32 first, deliberately — but then
   returned `{"reused": True}` the moment an id existed, never comparing the URL. A
   redeploy therefore could not converge: the item kept dialling the dead address and
   PRA reported "internal timeout starting session", which looks exactly like a blocked
   firewall and sends you to the wrong layer.

2. **The per-deploy checkbox never inherited the configured default.** The deploy-options
   endpoints returned pickers but not `*_ui_web_jump_enabled`, and both request models
   default `web_jump_enabled = False`. So a Deploy from the Containers page posted False
   even with the feature switched on in Settings, skipped registration entirely, and left
   whatever stale item existed untouched. Nothing errored; the deploy reported success.

Guarded at source level because that is the only level that sees them. The re-point path
is `async` behind PRA, Terraform and a live node, and defect 2 lives in the seam between
a Python payload and an Alpine object literal — where a missing key is not an error in
either language.

Run: python tests/test_web_jump_repoint.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


_K8S = _read("web_dashboard", "services", "k8s_service.py")
_PORTAINER = _read("web_dashboard", "services", "portainer_node_service.py")
_TPS = _read("web_dashboard", "services", "terraform_pra_service.py")
_API = _read("web_dashboard", "api", "containers.py")
_PAGE = _read("web_dashboard", "templates", "containers", "index.html")


def _register_body(src, func):
    """The body of one register_*_ui_web_jump, up to the next top-level def."""
    start = src.index(f"async def {func}(")
    rest = src[start + 10:]
    end = rest.find("\nasync def ")
    end2 = rest.find("\ndef ")
    if end2 != -1 and (end == -1 or end2 < end):
        end = end2
    return rest[:end] if end != -1 else rest


_REGISTRARS = (
    ("rancher", _register_body(_K8S, "register_rancher_ui_web_jump"), "server_url"),
    ("portainer", _register_body(_PORTAINER, "register_portainer_ui_web_jump"), "url"),
)


# ── defect 1: the redeploy must re-point, not reuse blindly ───────────────────

def test_the_url_helper_exists_and_is_pure():
    """`web_jump_url_from_state` is the whole basis of the comparison — and it must not
    need Terraform to run, or the registrars could not call it on every deploy."""
    assert "def web_jump_url_from_state(" in _TPS
    body = _TPS[_TPS.index("def web_jump_url_from_state("):]
    body = body[:body.find("\ndef ", 1)] if "\ndef " in body[1:] else body
    for forbidden in ("subprocess", "to_thread", "_terraform", "await "):
        assert forbidden not in body, f"web_jump_url_from_state must stay pure ({forbidden})"


def test_both_registrars_compare_the_stored_url_before_reusing():
    for label, body, url_var in _REGISTRARS:
        assert "web_jump_url_from_state(" in body, (
            f"{label}: registration never reads the URL its Web Jump was created with, so a "
            f"node that moved is never noticed")
        # The comparison must be against the CURRENT node URL the function resolved.
        assert re.search(rf"prior_url\s*==\s*{re.escape(url_var)}\b", body), (
            f"{label}: the stored URL is read but not compared against {url_var}")


def test_both_registrars_only_reuse_when_the_url_still_matches():
    """The `reused: True` return must be GUARDED. An unconditional early-return is the
    defect: it is the only path a redeploy takes, so nothing else can converge."""
    for label, body, _ in _REGISTRARS:
        reuse = body.index('"reused": True')
        guard = body[:reuse]
        assert "prior_url" in guard, (
            f"{label}: reuse is not guarded by the URL comparison — a redeploy cannot "
            f"re-point a Web Jump left on a dead address")
        assert "not prior_url" in guard, (
            f"{label}: an unreadable state must fall through to REUSE, never to a "
            f"destroy/recreate on every call")


def test_both_registrars_remove_before_recreating():
    """provision_web_jump starts from empty state — there is no in-place update — so a
    re-point is destroy + create, and the destroy has to come first or PRA is asked for
    two jump items with the same name."""
    for label, body, _ in _REGISTRARS:
        remove = f"remove_{label}_ui_web_jump()"
        assert remove in body, f"{label}: re-point never destroys the old item ({remove})"
        assert body.index(remove) < body.index("provision_web_jump("), (
            f"{label}: {remove} must run BEFORE the replacement is provisioned")


def test_the_gateway_resync_still_happens_before_any_return():
    """Pre-existing behaviour this change must not regress: the gateway /32 refresh runs
    on EVERY call, ahead of the reuse return, because gateway egress IPs are ephemeral."""
    for label, body, _ in _REGISTRARS:
        first_return = body.index('"reused": True')
        head = body[:first_return]
        assert "refresh_" in head and "firewall" in head, (
            f"{label}: the firewall refresh moved after the reuse return — an ephemeral "
            f"gateway IP would stop being re-synced")


# ── defect 2: the per-deploy checkbox inherits the configured default ─────────

_FEATURES = (
    ("rancher", "rancher_ui_web_jump_enabled", "deployForm", "o"),
    ("portainer", "portainer_ui_web_jump_enabled", "portainerDeployForm", "opts"),
)


def test_both_deploy_option_endpoints_publish_the_configured_default():
    for label, cfg_key, _, _ in _FEATURES:
        assert f'"web_jump_enabled": config_service.get_bool("{cfg_key}"' in _API, (
            f"{label}: the deploy-options endpoint does not publish {cfg_key}, so the form "
            f"has nothing to seed the checkbox from and silently posts False")


def test_both_forms_seed_the_checkbox_from_the_payload():
    for label, _, form, payload in _FEATURES:
        assert re.search(
            rf"{payload}\.web_jump_enabled\s*===\s*'boolean'\)\s*this\.{form}\.web_jump_enabled\s*="
            rf"\s*{payload}\.web_jump_enabled", _PAGE), (
            f"{label}: {form}.web_jump_enabled is never seeded from the endpoint, so it "
            f"keeps the hard-coded false and the deploy opts out of Web Jump registration")


def test_the_key_is_spelled_the_same_on_both_sides():
    """The seam this defect lives in: a Python dict key and a JS property read. A silent
    rename on either side reads as `undefined` in JS and re-breaks it with no error."""
    assert _API.count('"web_jump_enabled": config_service.get_bool(') == 2
    assert len(re.findall(r"\.web_jump_enabled\s*===\s*'boolean'", _PAGE)) == 2


def test_the_request_models_still_default_false():
    """Deliberate, and load-bearing: the API must not turn Web Jump provisioning on for a
    caller that never asked. Seeding is the FORM's job — this test exists so a future fix
    to the default doesn't get applied at the wrong layer."""
    models = _read("web_dashboard", "models", "containers.py")
    assert models.count("web_jump_enabled: bool = False") == 2


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
