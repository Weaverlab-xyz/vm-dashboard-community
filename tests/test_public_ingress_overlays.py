"""The tunnel overlays publish the agent vhost and nothing else, over loopback.

``docker-compose.ingress.yml`` + one of ``docker-compose.cloudflared.yml`` /
``docker-compose.ngrok.yml`` expose a NAT'd install through an outbound tunnel. The
security argument rests on three wiring facts that a later edit could quietly undo:

  * The gateway runs ``examples/cloud-gateway/Caddyfile`` — the file
    ``test_agent_vhost_surface.py`` already checks against the route table. A private
    copy of it would escape that test.
  * Gateway and connector share the app's network namespace, so the app's loopback
    ``TRUSTED_PROXY_HOSTS`` default is correct. Overriding it here would be a sign the
    topology changed and the comment explaining it is now wrong.
  * The connectors point at the gateway (port 80), never at the app's 8000, which
    would publish the whole app with no path filter.

Runs under pytest, or standalone: ``python tests/test_public_ingress_overlays.py``.
"""
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INGRESS = "docker-compose.ingress.yml"
_PROVIDERS = {"docker-compose.cloudflared.yml": "cloudflared",
              "docker-compose.ngrok.yml": "ngrok"}
_SIDECAR = 'service:app'


def _load(name):
    with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


def test_the_gateway_runs_the_route_checked_caddyfile():
    gw = _load(_INGRESS)["tunnel-gateway"]
    assert "./examples/cloud-gateway/Caddyfile:/etc/caddy/Caddyfile:ro" in gw["volumes"]
    assert gw["network_mode"] == _SIDECAR
    assert "ports" not in gw, "the gateway must not be published; the tunnel is the way in"


def test_the_app_keeps_the_loopback_proxy_trust_and_gets_the_tunnel_origin():
    env = _load(_INGRESS)["app"]["environment"]
    assert "PUBLIC_BASE_URL=https://${AGENT_HOSTNAME}" in env
    assert not any(e.startswith("TRUSTED_PROXY_HOSTS") for e in env), (
        "everything is loopback in this topology; the 127.0.0.1 default is correct")


def test_the_ui_is_not_published_unless_asked_for():
    env = _load(_INGRESS)["tunnel-gateway"]["environment"]
    assert env["UI_HOSTNAME"].endswith("ui.invalid}"), env["UI_HOSTNAME"]


def test_each_connector_is_a_loopback_sidecar_pinned_to_an_exact_version():
    for name, svc_name in _PROVIDERS.items():
        svc = _load(name)[svc_name]
        assert svc["network_mode"] == _SIDECAR, name
        assert "ports" not in svc, name
        assert re.search(r":\d+\.\d+\.\d+", svc["image"]), f"{name}: pin {svc['image']}"
        assert svc["depends_on"] == ["tunnel-gateway"], name


def test_ngrok_forwards_to_the_gateway_not_the_app():
    cmd = _load("docker-compose.ngrok.yml")["ngrok"]["command"]
    assert cmd[:2] == ["http", "80"], cmd
    assert "8000" not in " ".join(cmd)
    assert "--inspect=false" in cmd


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
    sys.exit(1 if failures else 0)
