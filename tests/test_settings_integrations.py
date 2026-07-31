"""The Settings → Integrations toggles must actually be wired end to end.

A toggle needs **three** things agreeing, in three different files, and getting any one
wrong fails silently:

1. an entry in the ``integrations[]`` array in ``settings.html`` — the row renders;
2. a matching key in ``_FEATURE_MODELS`` (``api/setup.py``) — ``PATCH
   /api/setup/feature/{key}`` 404s without it, so the switch throws an error toast;
3. a matching key in the ``map`` inside ``loadIntegrations`` (``settings.html``) — this
   is the nasty one. Miss it and the row renders, the switch saves, and the toggle
   **still shows OFF on every page load** regardless of the real config value, because
   the initial state comes from that map. Nothing errors; it just looks like the setting
   will not stick.

Remote Agents shipped with (1) and (2) but the page had no toggle at all, so the whole
feature was unreachable from the UI — the operator's only route in was the reconfigure
wizard. These assertions stop the same wiring from being half-done again.

Pure: parses both files, no app import. Runs under pytest, or standalone:
    python tests/test_settings_integrations.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_SETUP = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _integration_keys() -> set:
    """The `key:` of every entry in the integrations[] array."""
    html = _read(_SETTINGS)
    start = html.index("integrations: [")
    end = html.index("\n    ],", start)
    return set(re.findall(r"\{\s*key:\s*'([a-z_0-9]+)'", html[start:end]))


def _config_only_keys() -> set:
    """Entries flagged `configOnly: true` — their on/off lives elsewhere, so they are
    exempt from the enabled-state map."""
    html = _read(_SETTINGS)
    start = html.index("integrations: [")
    end = html.index("\n    ],", start)
    block = html[start:end]
    return {m.group(1) for m in re.finditer(r"\{\s*key:\s*'([a-z_0-9]+)'[^}]*?configOnly:\s*true",
                                            block, re.S)}


def _load_map_keys() -> set:
    """The keys of the `map` built in loadIntegrations().

    Matched on the ``key: features.x`` pair rather than line-anchored, because several
    entries share a line — a line-anchored pattern silently sees only the first of them
    and reports the rest as missing.
    """
    html = _read(_SETTINGS)
    start = html.index("const map = {")
    end = html.index("};", start)
    return set(re.findall(r"([a-z_0-9]+):\s*features\.", html[start:end]))


def _feature_model_keys() -> set:
    tree = ast.parse(_read(_SETUP))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_FEATURE_MODELS" for t in node.targets):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("_FEATURE_MODELS not found")


# ── the three-way agreement ───────────────────────────────────────────────────

def test_every_toggle_has_a_feature_model():
    """Without one, PATCH /api/setup/feature/{key} 404s and the switch errors."""
    missing = _integration_keys() - _feature_model_keys()
    assert not missing, (
        f"settings.html offers toggles with no _FEATURE_MODELS entry: {sorted(missing)}")


def test_every_toggle_has_an_enabled_state_mapping():
    """THE silent one. Without a map entry the toggle renders permanently off, however
    the flag is really set, and saving appears not to stick."""
    missing = _integration_keys() - _config_only_keys() - _load_map_keys()
    assert not missing, (
        f"settings.html toggles with no entry in the loadIntegrations map — these will "
        f"always render OFF: {sorted(missing)}")


def test_the_map_has_no_entries_for_toggles_that_do_not_exist():
    """Dead map entries mean a toggle was removed and its wiring left behind."""
    stale = _load_map_keys() - _integration_keys()
    assert not stale, f"loadIntegrations maps keys with no integrations[] entry: {sorted(stale)}"


def test_the_api_supplies_every_field_the_map_reads():
    """The map reads `features.<x>` off GET /api/features. A name that is not in the
    response is `undefined` → falsy → the toggle renders off, silently."""
    html = _read(_SETTINGS)
    start = html.index("const map = {")
    end = html.index("};", start)
    read_fields = set(re.findall(r":\s*features\.([a-z_0-9]+)", html[start:end]))

    main = _read(_MAIN)
    body_start = main.index('return {\n        "vmware":')
    body_end = main.index("\n    }", body_start)
    supplied = set(re.findall(r'"([a-z_0-9]+)":', main[body_start:body_end]))

    missing = read_fields - supplied
    assert not missing, (
        f"settings.html reads features.{{{','.join(sorted(missing))}}} but /api/features "
        f"does not return them — those toggles render permanently off")


# ── remote agents specifically ────────────────────────────────────────────────

def test_remote_agents_is_reachable_from_settings():
    """The regression this file was written for: the feature existed, worked, and was
    invisible, because the only way to enable it was the reconfigure wizard."""
    assert "remote_agents" in _integration_keys(), \
        "no Remote Agents toggle in Settings → Integrations"
    assert "remote_agents" in _feature_model_keys()
    assert "remote_agents" in _load_map_keys()


def test_the_remote_agents_key_derives_the_existing_flag():
    """_feature_to_cfg_key appends '_enabled', so the key has to be exactly
    'remote_agents' or the toggle writes a config key nothing reads."""
    setup = _read(_SETUP)
    assert 'def _feature_to_cfg_key' in setup
    assert 'return f"{feature}_enabled"' in setup, \
        "the key-derivation rule changed; re-check that remote_agents still maps to " \
        "remote_agents_enabled"
    # And that flag is what the nav gate and the router gate both read.
    main = _read(_MAIN)
    assert '"remote_agents_enabled": config_service.get_bool("remote_agents_enabled"' in main
    assert '_feature_gate("remote_agents_enabled")' in main
    nav = _read(os.path.join(_ROOT, "web_dashboard", "templates", "_nav_links.html"))
    assert "remote_agents_enabled" in nav, "the nav link lost its gate"


def test_remote_agents_is_not_config_only():
    """configOnly would strip `enabled` on save, so the toggle would never persist."""
    assert "remote_agents" not in _config_only_keys()
    setup = _read(_SETUP)
    block = re.search(r"_CONFIG_ONLY_FEATURES\s*=\s*\{([^}]*)\}", setup).group(1)
    assert "remote_agents" not in block


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
