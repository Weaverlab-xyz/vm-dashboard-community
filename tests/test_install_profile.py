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
