"""The install profile is a GATE, and the two profiles are mutually exclusive.

A demo instance resolves its BeyondTrust tenant from the global singletons
(`bt_api_host` / `pscli_api_url` / `entitle_api_key`); a POV instance holds a registry of
many named tenants because several POVs run at once. An instance claiming both roles would
have two answers to "which tenant?" at every call site, and the wrong answer is silent
rather than loud — a demo deploy onboarding into a customer's Password Safe, or a POV
onboarding into the demo tenant. Nothing errors; both paths "work".

So the properties pinned here are the ones whose absence is invisible:

  * ONE resolver. `main._feature_gate` decides whether a router 404s and
    `feature_flags.flags()` decides whether the nav link renders. A mask applied to only
    one of them yields a nav link to a router that 404s, or the reverse.
  * The mask only ever SUBTRACTS. A profile may refuse a feature; it may never enable one
    that config left off.
  * An unknown profile falls back to `demo`, i.e. to today's behaviour — the profile is
    read on the request path, so a typo in one config row must not take the app down.
  * The `pov` profile skips the cloud WRITES, not just the screens.

Pure where it can be: the source-shape assertions parse files and import nothing. The
behavioural ones import feature_flags, which needs only config_service and settings.

Runs under pytest, or standalone:
    python tests/test_install_profile.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-install-profile")

_FLAGS = os.path.join(_ROOT, "web_dashboard", "services", "feature_flags.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_SETUP = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_COMPOSE = os.path.join(_ROOT, "docker-compose.pov.yml")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── source shape: one resolver, and both readers use it ──────────────────────

def test_flags_resolves_through_the_masking_reader():
    """flags() must not read config_service directly, or it bypasses the mask."""
    src = _read(_FLAGS)
    body = src.split("def flags()", 1)[1].split("\ndef ", 1)[0]
    assert "config_service.get_bool(" not in body, (
        "flags() reads config_service.get_bool directly — it must go through enabled() "
        "so a profile-masked feature cannot render a nav link to a router that 404s")
    assert 'enabled("' in body, "flags() no longer calls enabled()"


def test_the_router_gate_resolves_through_the_same_reader():
    src = _read(_MAIN)
    gate = src.split("def _feature_gate", 1)[1].split("\ndef ", 1)[0]
    assert "feature_flags.enabled(flag)" in gate, (
        "_feature_gate does not resolve through feature_flags.enabled — the router gate "
        "and the nav links would disagree on a POV instance")
    assert "config_service.get_bool(flag)" not in gate, \
        "_feature_gate still reads config_service directly"


def _html_page_routes():
    """Every ``@app.get(..., response_class=HTMLResponse ...)`` route in main.py, as
    (path, decorator_text, body_text)."""
    src = _read(_MAIN)
    out = []
    for chunk in src.split("@app.get(")[1:]:
        path = chunk.split('"')[1]
        head, _, rest = chunk.partition("async def")
        if "HTMLResponse" not in head:
            continue
        out.append((path, head, rest.split("\n@app.")[0]))
    return out


def test_no_page_route_reads_a_masked_flag_through_config_service():
    """A page guard that calls config_service.get_bool bypasses the profile mask.

    This is the /pov bug pointing the other way: /proxmox and its siblings guarded on the
    raw config row, so a POV instance with a stale ``proxmox_enabled`` row served a demo
    hypervisor page — on the instance that does customer work. The mask lives in
    feature_flags.enabled(), so a guard that does not go through it is not a guard.
    """
    from web_dashboard.services import feature_flags as ff
    offenders = []
    for path, _head, body in _html_page_routes():
        for flag in re.findall(r'config_service\.get_bool\(\s*"(\w+)"', body):
            if flag in ff._DEMO_ONLY or flag in ff._POV_ONLY:
                offenders.append(f"{path} reads {flag} directly")
    assert not offenders, (
        "profile-masked flags read through config_service on a page route: "
        f"{offenders}. Use dependencies=[_feature_gate(flag)] instead.")


def test_every_profile_masked_page_is_actually_gated():
    """A nav link is not a gate — it hides the door, it does not lock it.

    /costs, /desktops, /databases, /functions and /k8s each described themselves as
    "Nav-gated on <flag>" and had no page guard at all, so on a POV instance the link
    vanished and the page still rendered to anyone who typed the URL.

    /containers is the one deliberate exception: it surfaces AWS ECS, Azure ACI and GCP
    Cloud Run alongside Portainer, and each tab self-gates. The exemption is listed here
    rather than inferred, so adding a second one has to be a decision someone makes.
    """
    from web_dashboard.services import feature_flags as ff
    exempt = {"/containers"}
    owned = {
        "/vms": "vmware_enabled", "/proxmox": "proxmox_enabled",
        "/vsphere": "vsphere_enabled", "/hyperv": "hyperv_enabled",
        "/nutanix": "nutanix_enabled", "/xcpng": "xcpng_enabled",
        "/costs": "cost_explorer_enabled", "/desktops": "vdesktops_enabled",
        "/databases": "cloud_database_enabled", "/functions": "cloud_functions_enabled",
        "/k8s": "k8s_management_enabled", "/pov": "pov_environments_enabled",
    }
    for flag in owned.values():
        assert flag in ff._DEMO_ONLY or flag in ff._POV_ONLY, \
            f"{flag} is no longer profile-masked; this map is stale"

    heads = {path: head for path, head, _body in _html_page_routes()}
    ungated = []
    for path, flag in owned.items():
        if path in exempt:
            continue
        assert path in heads, f"{path} is no longer an HTML page route; update this map"
        if f'_feature_gate("{flag}")' not in heads[path]:
            ungated.append(f"{path} (expected _feature_gate({flag!r}))")
    assert not ungated, f"profile-masked pages with no page gate: {ungated}"


def test_every_profile_owned_page_group_is_actually_gated():
    """The same rule as above, for pages that are not feature flags.

    AWS/Azure/GCP/OCI and Images are gated on credential PRESENCE, not on a toggle, so
    there was no flag for _DEMO_ONLY to mask and all five kept rendering on a POV
    instance — pointing at pages that instance can never put data in, because its wizard
    deliberately writes no cloud credentials.

    Source-parsing, like its sibling, so a new cloud page cannot regress this one route
    at a time.
    """
    from web_dashboard.services import feature_flags as ff
    owned = {"/aws": "cloud_pages", "/azure": "cloud_pages", "/gcp": "cloud_pages",
             "/oci": "cloud_pages", "/images": "cloud_pages"}
    for name in set(owned.values()):
        assert name in ff._PROFILE_PAGES, f"{name} is no longer a profile page group"

    heads = {path: head for path, head, _body in _html_page_routes()}
    ungated = []
    for path, name in owned.items():
        assert path in heads, f"{path} is no longer an HTML page route; update this map"
        if f'_profile_page_gate("{name}")' not in heads[path]:
            ungated.append(f"{path} (expected _profile_page_gate({name!r}))")
    assert not ungated, f"profile-owned pages with no page gate: {ungated}"


def test_the_profile_page_gate_resolves_through_the_same_reader_as_the_nav():
    """The 28cfc67 rule, restated for page groups: one reader, or the nav link and the
    route disagree and you get a link to a 404 — or a page with no link that still
    serves."""
    src = _read(_MAIN)
    gate = src.split("def _profile_page_gate", 1)[1].split("\ndef ", 1)[0]
    assert "feature_flags.profile_page_allowed(name)" in gate, (
        "_profile_page_gate does not resolve through feature_flags.profile_page_allowed")

    from web_dashboard.services import feature_flags as ff
    assert "cloud_pages" in ff.flags(), (
        "flags() does not ship cloud_pages, so _nav_links.html cannot read it and every "
        "cloud link would fail closed on EVERY profile")


def test_a_profile_page_group_is_masked_on_the_other_profile():
    _ensure_schema()
    from web_dashboard.services import config_service
    ff = _with_profile("pov")
    try:
        assert ff.profile_page_allowed("cloud_pages") is False
        assert ff.flags()["cloud_pages"] is False,             "the nav reads this key; masking only the gate leaves five live links"
    finally:
        config_service.delete("install_profile")
        config_service.invalidate()

    ff = _with_profile("demo")
    try:
        assert ff.profile_page_allowed("cloud_pages") is True
        assert ff.flags()["cloud_pages"] is True
    finally:
        config_service.delete("install_profile")
        config_service.invalidate()


def test_an_unclaimed_page_group_is_allowed_everywhere():
    """Matching profile_masks: a name nobody claims is profile-neutral, not forbidden."""
    from web_dashboard.services import feature_flags as ff
    assert ff.profile_page_allowed("something_nobody_owns") is True


def test_the_nav_gates_the_cloud_links_on_the_same_name():
    """The other half of the pair. A gate with no nav guard leaves five links to pages
    that 404; a nav guard with no gate is what 28cfc67 was written about."""
    nav = _read(os.path.join(_ROOT, "web_dashboard", "templates", "_nav_links.html"))
    assert nav.count("{% if cloud_pages %}") == 2, (
        "expected two cloud_pages blocks in the nav — the four cloud consoles and Images")
    for href in ('href="/aws"', 'href="/azure"', 'href="/gcp"', 'href="/oci"',
                 'href="/images"'):
        assert href in nav
    # Storage is deliberately NOT gated: it is where the agent-brokered filesystem
    # backend gets configured, which is the only storage a POV instance can have.
    assert 'href="/storage"' in nav
    storage_line = [ln for ln in nav.splitlines() if 'href="/storage"' in ln][0]
    assert "cloud_pages" not in storage_line


def test_the_background_warmers_respect_the_mask():
    """Worse than an ungated page: these make outbound calls on a timer. Reading the raw
    config row meant a POV instance kept polling the DEMO tenant's Cost Management, and a
    Portainer the profile masks off."""
    src = _read(_MAIN)
    for warmer in ("_warm_cost_summary", "_warm_portainer_containers"):
        body = src.split(f"async def {warmer}", 1)[1].split("\nasync def ", 1)[0]
        assert "feature_flags.enabled(" in body, \
            f"{warmer} does not resolve its flag through the mask"
        assert "config_service.get_bool(" not in body, \
            f"{warmer} still reads config_service directly"


def test_the_context_processor_supplies_the_nav_flags():
    """One supplier, so a page's chrome cannot depend on the route remembering it.

    Before #664 the flags were spread per route and three routes forgot: /users, /groups
    and /workgroups rendered a nav with fifteen links missing. Nothing errored, because
    Jinja resolves an undefined name to falsey and every ``{% if <flag> %}`` failed
    closed. Making a route's own chrome its own responsibility is what caused that.
    """
    src = _read(_MAIN)
    proc = src.split("def _profile_context", 1)[1].split("\ntemplates = ", 1)[0]
    assert "_feature_flags()" in proc, \
        "_profile_context no longer supplies the feature flags"
    assert "context_processors=[_profile_context]" in src, \
        "_profile_context is not registered on the templates object"


def test_no_route_hands_a_flag_name_to_templateresponse():
    """Starlette applies context processors with ``context.update(...)`` AFTER the route's
    own context, so a value the processor returns OVERWRITES whatever the route passed.

    That makes an overriding route silently ineffective — the same invisible-failure shape
    as #664, pointing the other way. The rule is therefore: no route passes a flag name at
    all, by key or by spreading _feature_flags(). This test is the only thing that makes
    breaking it loud.

    (If a route ever genuinely needs a per-page override, the answer is a differently
    named key, or inverting precedence in a Jinja2Templates subclass — not a quiet re-add.)

    Parsed with ast rather than matched textually: a dict literal spanning six lines with
    a comment in the middle is exactly where a regex quietly stops finding things.
    """
    import ast
    from web_dashboard.services import feature_flags as ff
    flag_names = set(ff.flags().keys())

    tree = ast.parse(_read(_MAIN))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "TemplateResponse"):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if not isinstance(arg, ast.Dict):
                continue
            for key, value in zip(arg.keys, arg.values):
                if key is None:                      # a ** unpacking
                    src = ast.unparse(value)
                    if "_feature_flags" in src or src == "flags":
                        offenders.append(f"line {node.lineno}: **{src}")
                elif isinstance(key, ast.Constant) and key.value in flag_names:
                    offenders.append(f"line {node.lineno}: {key.value!r}")
    assert not offenders, (
        "route context passes flag names that the context processor overwrites, so the "
        f"route's own values are silently discarded: {offenders}")


def test_there_is_no_both_profile():
    """A `both` profile would have to pick a tenant source at every call site."""
    src = _read(_FLAGS)
    m = re.search(r"VALID_PROFILES\s*=\s*\(([^)]*)\)", src)
    assert m, "VALID_PROFILES not found"
    profiles = set(re.findall(r"'([^']+)'|\"([^\"]+)\"", m.group(1)))
    flat = {a or b for a, b in profiles}
    assert flat == {"demo", "pov"}, f"unexpected profiles: {sorted(flat)}"


def test_the_two_profile_lists_do_not_overlap():
    from web_dashboard.services import feature_flags as ff
    overlap = set(ff._DEMO_ONLY) & set(ff._POV_ONLY)
    assert not overlap, f"flags claimed by both profiles: {sorted(overlap)}"


def test_the_shared_integrations_are_owned_by_neither():
    """A POV instance needs PRA, Password Safe, agents and Ansible. Claiming any of them
    for `demo` would make a POV instance unable to wire anything."""
    from web_dashboard.services import feature_flags as ff
    owned = set(ff._DEMO_ONLY) | set(ff._POV_ONLY)
    for flag in ("pra_enabled", "password_safe_enabled", "remote_agents_enabled",
                 "ansible_enabled", "entitle_enabled", "entitle_registration_enabled",
                 "notifications_enabled", "resource_expiry_enabled"):
        assert flag not in owned, f"{flag} must be profile-neutral, it is needed by both"


# ── behaviour: the mask ──────────────────────────────────────────────────────

def _with_profile(profile):
    """Set the profile and return the feature_flags module."""
    from web_dashboard.services import config_service, feature_flags as ff
    config_service.set("install_profile", profile)
    config_service.invalidate()
    return ff


def _ensure_schema():
    from web_dashboard import database as d
    d.Base.metadata.create_all(bind=d.engine)


def test_a_demo_only_flag_is_masked_on_a_pov_instance():
    _ensure_schema()
    from web_dashboard.services import config_service
    config_service.set("cloud_database_enabled", "1")
    ff = _with_profile("pov")
    try:
        assert ff.profile_masks("cloud_database_enabled") is True
        assert ff.enabled("cloud_database_enabled") is False, \
            "config says on, the profile says no — the profile must win"
        assert ff.feature_map()["cloud_database"] is False
    finally:
        config_service.delete("cloud_database_enabled")
        config_service.delete("install_profile")
        config_service.invalidate()


def test_a_pov_flag_is_masked_on_a_demo_instance():
    _ensure_schema()
    from web_dashboard.services import config_service
    config_service.set("pov_environments_enabled", "1")
    ff = _with_profile("demo")
    try:
        assert ff.profile_masks("pov_environments_enabled") is True
        assert ff.enabled("pov_environments_enabled") is False
    finally:
        config_service.delete("pov_environments_enabled")
        config_service.delete("install_profile")
        config_service.invalidate()


def test_the_mask_only_subtracts():
    """Being in the right profile is necessary, never sufficient."""
    _ensure_schema()
    from web_dashboard.services import config_service
    config_service.set("pov_environments_enabled", "0")
    ff = _with_profile("pov")
    try:
        assert ff.profile_masks("pov_environments_enabled") is False
        assert ff.enabled("pov_environments_enabled") is False, \
            "the profile enabled a feature config had switched off"
    finally:
        config_service.delete("pov_environments_enabled")
        config_service.delete("install_profile")
        config_service.invalidate()


def test_an_unknown_profile_falls_back_to_demo():
    """Read on the request path, so a typo must degrade to today's behaviour."""
    _ensure_schema()
    from web_dashboard.services import config_service
    ff = _with_profile("banana")
    try:
        assert ff.install_profile() == "demo"
        assert ff.profile_masks("cloud_database_enabled") is False
    finally:
        config_service.delete("install_profile")
        config_service.invalidate()


def test_a_neutral_flag_is_never_masked():
    _ensure_schema()
    from web_dashboard.services import config_service
    try:
        for profile in ("demo", "pov"):
            ff = _with_profile(profile)
            for flag in ("pra_enabled", "password_safe_enabled", "remote_agents_enabled"):
                assert ff.profile_masks(flag) is False, \
                    f"{flag} masked under {profile}; both profiles need it"
    finally:
        config_service.delete("install_profile")
        config_service.invalidate()


# ── the wizard writes, and what it declines to write ────────────────────────

def test_the_pov_profile_skips_the_cloud_writes():
    """Not just the screens. The wizard persists every non-secret cloud field whether or
    not it was filled in, so clicking past AWS would otherwise still store
    aws_region="us-east-2" — and "is this cloud usable?" is inferred from credential
    presence, not from a flag, so those defaults are not harmless noise."""
    src = _read(_SETUP)
    body = src.split("def _apply_config", 1)[1].split("\ndef ", 1)[0]
    assert 'if profile == "demo":' in body, (
        "_apply_config no longer guards the cloud writes on the profile")
    assert 'pairs["install_profile"] = payload.profile.install_profile' in body, \
        "_apply_config does not persist the chosen profile"


def test_a_reconfigure_that_omits_the_profile_does_not_reset_it():
    """The hazard: Settings -> "Reconfigure wizard" on a POV instance. If the payload
    defaulted the profile to `demo`, saving would flip the instance and unmask every demo
    feature -- silently, and while the operator was editing something unrelated. Two
    independent guards, because either alone is a single point of failure."""
    src = _read(_SETUP)
    assert "profile: ProfileSetup | None = None" in src, (
        "SetupPayload.profile must default to None, not ProfileSetup() — absent has to "
        "mean 'leave the profile alone', never 'demo'")
    body = src.split("def _apply_config", 1)[1].split("\ndef ", 1)[0]
    assert "if payload.profile is not None:" in body, \
        "_apply_config writes install_profile unconditionally"
    assert "feature_flags.install_profile()" in body, (
        "the cloud-write guard must fall back to the STORED profile when the payload "
        "omits one, or a reconfigure writes cloud defaults into a POV instance")


def test_the_wizard_loads_the_stored_profile_on_reconfigure():
    """Otherwise it renders 'demo' selected on a POV instance and submits that."""
    html = _read(os.path.join(_ROOT, "web_dashboard", "templates", "setup.html"))
    init = html.split("async init()", 1)[1].split("\n    },", 1)[0]
    assert "form.profile.install_profile = cfg.install_profile" in init, (
        "init() hydrates every other field but not the profile — the Purpose step would "
        "show the wrong instance kind and saving would change it")


def test_enabling_a_masked_integration_is_refused_rather_than_stored():
    """Storing it would read back as on while enabled() kept returning False — a toggle
    that saves cleanly, shows on, and does nothing."""
    src = _read(_SETUP)
    body = src.split("def patch_feature_config", 1)[1].split("\ndef ", 1)[0]
    assert "profile_masks(" in body, \
        "the feature PATCH endpoint does not check the profile mask"
    assert "409" in body, "a masked integration should be refused with a conflict"


def test_turning_a_masked_integration_off_is_still_allowed():
    src = _read(_SETUP)
    body = src.split("def patch_feature_config", 1)[1].split("\ndef ", 1)[0]
    assert 'filtered.get("enabled") and' in body, (
        "the refusal must be conditional on ENABLING — refusing a disable would strand a "
        "flag that is already on")


# ── the second instance's compose file agrees with the profile ──────────────

def test_the_pov_compose_file_sets_the_profile_and_its_features():
    import yaml
    with open(_COMPOSE, encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    for svc in ("app", "worker"):
        env = compose["services"][svc]["environment"]
        assert "INSTALL_PROFILE=pov" in env, f"{svc} does not set the profile"
        assert "POV_ENVIRONMENTS_ENABLED=true" in env, f"{svc} does not enable the feature"


def test_the_pov_compose_file_needs_the_features_the_wiring_depends_on():
    """A POV instance with Ansible or remote agents off fails at the Jumpoint install
    step with a confusing error rather than at startup."""
    import yaml
    with open(_COMPOSE, encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    env = compose["services"]["app"]["environment"]
    for required in ("PRA_ENABLED=true", "PASSWORD_SAFE_ENABLED=true",
                     "REMOTE_AGENTS_ENABLED=true", "ANSIBLE_ENABLED=true"):
        assert required in env, f"the POV stack is missing {required}"


def test_the_pov_stack_shares_nothing_with_the_demo_stack():
    """Same volume, port or JWT key file as docker-compose.yml would defeat the point."""
    import yaml
    with open(_COMPOSE, encoding="utf-8") as fh:
        pov = yaml.safe_load(fh)
    with open(os.path.join(_ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
        demo = yaml.safe_load(fh)

    assert not (set(pov["volumes"]) & set(demo["volumes"])), \
        "the POV stack shares a volume with the demo stack"
    assert not (set(pov["secrets"]) & set(demo["secrets"])), \
        "the POV stack shares a secret with the demo stack"

    pov_key = list(pov["secrets"].values())[0]["file"]
    demo_key = list(demo["secrets"].values())[0]["file"]
    assert pov_key != demo_key, (
        "both stacks read the same JWT key file; that key is the Fernet root for "
        "app_config, so they would share encrypted configuration")

    def ports(doc):
        out = set()
        for svc in doc["services"].values():
            for p in (svc.get("ports") or []):
                out.add(str(p).split(":")[0])
        return out

    clash = ports(pov) & ports(demo)
    assert not clash, f"both stacks bind host port(s) {sorted(clash)}"


def test_the_pov_stack_does_not_mount_the_docker_socket():
    """A POV instance runs Ansible on the remote agent inside the customer environment
    and has Kubernetes masked off, so the host daemon buys it nothing."""
    import yaml
    with open(_COMPOSE, encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    for name, svc in compose["services"].items():
        for vol in (svc.get("volumes") or []):
            src = vol.get("source") if isinstance(vol, dict) else str(vol)
            assert "docker.sock" not in str(src), \
                f"{name} mounts the docker socket"


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
