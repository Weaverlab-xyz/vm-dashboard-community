"""The Config-Management page must offer every target kind the API accepts.

The page gated its whole run panel on "are there on-prem groups or cloud VMs?", written
out twice — once for the empty state, once for the panel. Kubernetes clusters and
databases were added as target kinds later (localhost plays, `/localhost-targets`), the
picker grew optgroups for them, and neither gate learned about them. Net effect on a
deployment whose only resource was a database: "No targets available", and the picker
that would have listed that database never rendered. A bulk hand-off from /inventory hid
its own selection the same way.

So the invariants worth pinning aren't the copy — they're the joins:

  * ONE gate expression, used by both branches. Two spellings of the same condition is
    exactly how the second one fell behind.
  * The gate counts every family the picker can list, plus bulk mode (which needs no
    picker target at all).
  * The picker's option prefixes match what the submit handler and the API parse.
  * A cloud="local" cluster/database — registered rather than provisioned, so it never
    appears in any cloud VM list — is a targetable cloud on the server.
  * The "Available Targets" legend lists the same families the picker does, or a
    database-only install renders an empty card next to a working picker.

Run: python tests/test_config_mgmt_targets.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def _page():
    return _read("web_dashboard", "templates", "config-mgmt", "index.html")


def _optional_getter(src, name):
    """Body of an Alpine `get <name>() { … }`, or "" when there is no such getter."""
    m = re.search(r"\n(\s*)get %s\(\) \{\n(.*?)\n\1\},\n" % re.escape(name), src, re.S)
    return m.group(2) if m else ""


def _getter(src, name):
    """Body of an Alpine `get <name>() { … }`, up to the closing brace at its indent."""
    body = _optional_getter(src, name)
    assert body, f"no `get {name}()` in the config-mgmt page"
    return body


# ── one gate, not two ─────────────────────────────────────────────────────────

def test_empty_state_and_run_panel_share_one_gate():
    """The original break: the two conditions were spelled out separately and only
    one of them would ever get updated."""
    src = _page()
    gates = re.findall(r'x-if="!loading &&([^"]*)"', src)
    assert len(gates) == 2, (
        f"expected exactly 2 !loading gates (empty state + run panel), found {len(gates)}: {gates}")
    negative = [g for g in gates if "!hasAnyTarget" in g]
    positive = [g for g in gates if g.strip() == "hasAnyTarget"]
    assert len(negative) == 1 and len(positive) == 1, (
        f"the empty state and run panel must both gate on hasAnyTarget, got {gates}")


def test_neither_gate_reimplements_the_condition():
    """An inlined `targets.length > 0 || hasCloudTargets` is the bug coming back."""
    src = _page()
    for gate in re.findall(r'x-if="!loading &&([^"]*)"', src):
        assert "targets.length" not in gate and "hasCloudTargets" not in gate, (
            f"gate reimplements the target check instead of using hasAnyTarget: {gate!r}")


# ── the gate counts everything the picker can offer ───────────────────────────

def test_gate_counts_every_target_family_and_bulk():
    src = _page()
    body = _getter(src, "hasAnyTarget")
    for ref in ("bulk", "targets.length", "hasCloudTargets", "hasLocalhostTargets"):
        assert ref in body, f"hasAnyTarget ignores {ref}: {body!r}"


def test_localhost_targets_are_k8s_clusters_and_databases():
    src = _page()
    body = _getter(src, "hasLocalhostTargets")
    for ref in ("k8sClusters", "cloudDatabases"):
        assert ref in body, f"hasLocalhostTargets ignores {ref}: {body!r}"


def test_every_picker_list_feeds_the_gate():
    """Whatever the picker can render an <option> from has to make the gate true, or
    the picker never renders at all."""
    src = _page()
    panel = src[src.index('<label class="block text-sm font-medium text-gray-700 mb-1">Target</label>'):]
    panel = panel[:panel.index("Ad-hoc on-prem host")]
    lists = set(re.findall(r'x-if="(\w+(?:\.\w+)*)\.length > 0"', panel))
    assert lists, "found no optgroup source lists in the target picker"

    # Follow the gate transitively rather than naming the helper getters, because a
    # hardcoded list here is the same bug this test exists to catch: adding a target family
    # would add a getter the list did not know about, and the guard would pass while the
    # picker stayed hidden. Whatever `hasAnyTarget` reaches through `this.<name>` is walked.
    reachable = _getter(src, "hasAnyTarget")
    seen, queue = set(), re.findall(r"this\.(\w+)", reachable)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        # Most names the walk reaches are plain state arrays, not getters — `_getter`
        # asserts, so use the optional form and simply stop descending there.
        body = _optional_getter(src, name)
        if body:
            reachable += body
            queue += re.findall(r"this\.(\w+)", body)
    for lst in lists:
        root = lst.split(".")[0]
        assert root in reachable, (
            f"picker lists {lst} but no getter behind hasAnyTarget counts it — "
            f"a deployment with only {root} would see 'No targets available'")


# ── picker prefixes match the submit handler and the API ──────────────────────

def test_picker_prefixes_round_trip_to_target_kind():
    src = _page()
    assert ":value=\"`k8s:${c.id}`\"" in src, "no k8s option value in the picker"
    assert ":value=\"`db:${d.id}`\"" in src, "no database option value in the picker"
    kind = _getter(src, "targetKind")
    assert "'k8s:'" in kind and "'db:'" in kind, (
        f"targetKind does not parse the prefixes the picker emits: {kind!r}")
    assert "return 'database'" in kind, "the db: prefix must map to the API's 'database' kind"
    # The submit handler sends the parsed kind + id, not the prefixed string.
    assert "target_kind: kind" in src and "target_id," in src


def test_api_accepts_the_kinds_the_page_sends():
    api = _read("web_dashboard", "api", "config_mgmt.py")
    assert 'target_kind: str = "vm"' in api, "RunRequest lost its target_kind default"
    assert re.search(r'target_kind in \("k8s", "database"\)', api), (
        "/run no longer routes k8s/database to the localhost path")


def test_page_reads_the_endpoint_that_serves_localhost_targets():
    src = _page()
    assert "'/api/config-mgmt/localhost-targets'" in src
    api = _read("web_dashboard", "api", "config_mgmt.py")
    assert '@router.get("/localhost-targets")' in api, (
        "the page's picker source route is gone — the picker would silently stay empty")


# ── a registered (cloud="local") resource is targetable ──────────────────────

def test_local_is_a_targetable_cloud_for_both_kinds():
    """A cluster registered from a kubeconfig, or a database registered against a
    Password Safe account, is never in a cloud VM list. If "local" isn't targetable
    the page can list it and the run still 400s."""
    src = _read("web_dashboard", "services", "ansible_cloud_run_service.py")
    for name in ("K8S_TARGET_CLOUDS", "DB_TARGET_CLOUDS"):
        clouds = re.search(r"^%s = \((.*?)\)" % name, src, re.M)
        assert clouds, f"{name} is gone"
        assert '"local"' in clouds.group(1), f'{name} dropped "local"'
    # …and the local cloud resolves to the local runner rather than raising.
    resolver = re.search(r"def resolve_runner\(cloud: str\) -> str:(.*?)\ndef ", src, re.S)
    assert resolver and 'if cloud == "local":' in resolver.group(1), (
        "resolve_runner no longer special-cases the local runner")


# ── legend parity ────────────────────────────────────────────────────────────

def test_available_targets_legend_covers_every_picker_family():
    src = _page()
    legend = src[src.index("Available Targets"):]
    legend = legend[:legend.index("Add on-prem targets in")]
    for lst in ("targets", "cloudTargets.aws", "cloudTargets.azure", "cloudTargets.gcp",
                "k8sClusters", "cloudDatabases"):
        assert lst in legend, (
            f"the Available Targets card never lists {lst} — a deployment with only "
            f"that family sees an empty card beside a working picker")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
