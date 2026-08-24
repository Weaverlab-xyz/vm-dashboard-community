"""Regression: the three BeyondTrust products stay three independent, correctly-wired
features.

Password Safe, PRA and EPM-L used to be one Settings panel, one pydantic model, and one
``beyondtrust_enabled`` flag read at ~50 sites. Splitting that produced four failure
modes, every one of them **silent** — no exception, no log line, just a feature that
stopped happening:

1. **A surviving ``beyondtrust_enabled`` read.** ``config_service.get_bool`` falls back
   through ``getattr(settings, key, default)``, and that field is gone, so a missed site
   now returns ``False`` on every install. Shell Jump provisioning or Password Safe
   onboarding simply stops.
2. **A flag with no ``config.py`` field.** Same fallback, same silence: a bare
   ``get_bool("pra_enabled")`` is permanently ``False`` if nothing declares the field.
3. **A key bound in one panel but declared on another's model.**
   ``patch_feature_config`` resolves the model from the URL path segment, so the value is
   dropped on save and reads back blank. ``test_setup_feature_roundtrip`` compares against
   the UNION of all models and cannot see this; a three-way split is the most likely way
   to introduce it.
4. **An ``x-init`` left behind when its ``<select>`` moved.** An unset string config key
   reads back as ``""``, not the model default, so the select renders unselected and then
   saves ``""``.

Pure-Python — no DB, no HTTP. Runs under pytest, or standalone:
    python tests/test_beyondtrust_split.py
"""
import ast
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SETTINGS = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_SETUP = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_CONFIG = os.path.join(_ROOT, "web_dashboard", "config.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_MIGRATION = os.path.join(_ROOT, "web_dashboard", "services", "feature_flag_migration.py")

PANELS = {"password_safe": "PasswordSafeFeatureConfig",
          "pra": "PRAFeatureConfig",
          "epml": "EPMLFeatureConfig"}
FLAGS = ("password_safe_enabled", "pra_enabled", "epml_enabled")

# The 96 non-`enabled` keys the single BeyondTrustFeatureConfig declared, frozen here so
# the split is provably lossless, PLUS keys deliberately added since (the eight
# `*_gcp_*` cloud-DB onboarding keys, which wire the GCP Cloud SQL plugins as the third
# Layer-2 cloud alongside AWS SSM and Azure Run Command). A key that vanishes from all three models is a field an
# operator can no longer set; one that appears on two breaks the union-check equivalence
# test_setup_feature_roundtrip documents in its own docstring.
LEGACY_KEYS = frozenset("""
pscli_api_url pscli_client_id pscli_client_secret pscli_api_account_name
passwordsafe_registration_enabled passwordsafe_workgroup
passwordsafe_vm_functional_account_aws passwordsafe_vm_functional_account_azure
passwordsafe_vm_functional_account_gcp passwordsafe_managed_account_name
passwordsafe_ssh_key_enforcement_mode passwordsafe_aws_registration_method
passwordsafe_ssm_account_suffix passwordsafe_ssm_change_password_on_register
passwordsafe_azure_registration_method passwordsafe_azure_change_password_on_register
passwordsafe_gcp_registration_method passwordsafe_gcp_change_password_on_register
bt_api_host bt_client_id bt_client_secret bt_jump_group_name bt_jumpoint_name
clouddb_ps_onboarding_enabled clouddb_ps_platform_postgres clouddb_ps_platform_mysql
clouddb_ps_platform_sqlserver clouddb_ps_pravault_platform clouddb_ps_workgroup
clouddb_ps_functional_account_mode clouddb_ps_functional_account_postgres
clouddb_ps_functional_account_mysql clouddb_ps_functional_account_sqlserver
clouddb_ps_pravault_functional_account clouddb_ps_self_rotation
clouddb_ps_import_workgroup clouddb_ps_import_default_cloud clouddb_ps_import_max_systems
clouddb_ps_import_platform_map clouddb_db_client_image_postgres
clouddb_db_client_image_mysql clouddb_db_client_image_sqlserver
clouddb_ps_ssm_iam_username clouddb_ps_ssm_access_key_id clouddb_ps_ssm_secret_access_key
clouddb_ps_ssm_account_suffix clouddb_ps_ssm_public_key_path
clouddb_ps_ssm_plugin_private_key clouddb_ps_ssm_plugin_passphrase
clouddb_ps_ssm_key_directory
pra_config_api_client_id pra_config_api_client_secret
passwordsafe_azure_db_registration_method clouddb_ps_platform_azure_postgres
clouddb_ps_platform_azure_mysql clouddb_ps_platform_azure_sqlserver
clouddb_ps_functional_account_azure_postgres clouddb_ps_functional_account_azure_mysql
clouddb_ps_functional_account_azure_sqlserver
clouddb_ps_azure_auth_mode clouddb_ps_azure_cert_path clouddb_ps_azure_ssl
clouddb_ps_azure_sp_client_id clouddb_ps_azure_sp_client_secret
clouddb_ps_azure_plugin_private_key clouddb_ps_azure_plugin_passphrase
passwordsafe_gcp_db_registration_method clouddb_ps_platform_gcp_postgres
clouddb_ps_platform_gcp_mysql clouddb_ps_functional_account_gcp_postgres
clouddb_ps_functional_account_gcp_mysql clouddb_ps_gcp_auth_mode
clouddb_ps_gcp_impersonate_target clouddb_ps_gcp_rotator_service_account
azure_bt_jump_group_name azure_jumpoint_name azure_vm_jumpoint_mode
gcp_bt_jump_group_name gcp_jumpoint_name gcp_vm_jumpoint_mode
epml_site_id epml_pat
k8s_ps_token_rotation_enabled k8s_ps_token_platform k8s_ps_pravault_token_platform
k8s_ps_functional_account_aws k8s_ps_functional_account_azure
k8s_ps_functional_account_gcp k8s_ps_functional_account_local
k8s_ps_pravault_functional_account k8s_ps_workgroup k8s_ps_token_mode
k8s_ps_token_ttl_seconds k8s_ps_token_change_on_register
k8s_ps_token_delete_legacy_secret k8s_ps_token_register_on_provision
k8s_ps_pravault_mirror_enabled k8s_ps_token_checkout_duration_min
k8s_ps_token_address_options k8s_ps_rotator_apply_rbac k8s_ps_rotator_gke_sa_email
k8s_ps_rotator_aks_sp_object_id k8s_ps_rotator_eks_username
k8s_ps_rotator_eks_principal_arn k8s_ps_rotator_eks_create_access_entry
k8s_ps_rotator_bootstrap_namespace k8s_ps_rotator_bootstrap_sa
pra_k8s_namespace pra_k8s_sa_name bt_vault_account_group_id
""".split())

# RETIRED, deliberately absent from LEGACY_KEYS above: the six `k8s_token_sync_*` keys
# (enabled / interval_minutes / request_duration_min / max_per_pass / max_failures /
# max_per_hour). They tuned a dashboard-side poll that copied the rotated ServiceAccount
# token into the PRA Vault account. Password Safe does that itself now — registration
# links the two managed accounts with SyncedAccounts — so there is no interval, no push
# cap and no circuit breaker left to tune. This is the one case where a key vanishing
# from all three models is correct rather than a lossy split, which is why it is written
# down here instead of just deleted.


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _class_fields(src, name):
    """Annotated field names on a pydantic class, read via AST (no imports needed)."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {s.target.id for s in node.body if isinstance(s, ast.AnnAssign)}
    raise AssertionError(f"class {name} not found — did the split get reverted?")


def _panel_blocks():
    """Slice settings.html into {panel key: its x-if block}, for the three BT panels."""
    html = _read(_SETTINGS)
    opens = [(m.start(), m.group(1)) for m in
             re.finditer(r"<template x-if=\"panel\?\.key === '([a-z_0-9]+)'", html)]
    blocks = {}
    for i, (pos, key) in enumerate(opens):
        if key not in PANELS:
            continue
        end = opens[i + 1][0] if i + 1 < len(opens) else len(html)
        blocks[key] = html[pos:end]
    return blocks


# ── 1. the sweep: no surviving read of the retired flag ───────────────────────

def test_no_beyondtrust_enabled_read_survives():
    """The reason `beyondtrust_enabled` was deleted from config.py LAST.

    Asserts on *reads*, not on the string: the migration module and a handful of comments
    legitimately name the retired key, and forbidding the word outright would fight
    documentation. What must not survive is anything that resolves it at runtime — once
    the settings field is gone, each of these silently evaluates to False forever.
    """
    reads = [
        r'get_bool\(\s*["\']beyondtrust_enabled["\']',
        r'settings\.beyondtrust_enabled',
        r'_feature_gate\(\s*["\']beyondtrust_enabled["\']',
        r'\{%\s*if\s+beyondtrust_enabled\s*%\}',
        r'features\.beyondtrust\b',
    ]
    offenders = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "web_dashboard")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith((".py", ".html", ".js")):
                continue
            path = os.path.join(dirpath, fn)
            if os.path.abspath(path) == os.path.abspath(_MIGRATION):
                continue    # the one module that must read the legacy key
            body = _read(path)
            for pat in reads:
                if re.search(pat, body):
                    offenders.append(f"{os.path.relpath(path, _ROOT)}: {pat}")
    assert not offenders, (
        "these still resolve the retired beyondtrust_enabled flag, which now reads False "
        "on every install because config.py no longer declares it — the gated feature "
        f"just stops happening, with no error:\n  " + "\n  ".join(offenders))


def test_the_sweep_would_catch_what_it_was_written_for():
    """The guard above is regex-based; prove each pattern actually matches its shape."""
    samples = [
        'if config_service.get_bool("beyondtrust_enabled"):',
        "if settings.beyondtrust_enabled:",
        'dependencies=[_feature_gate("beyondtrust_enabled")]',
        "{% if beyondtrust_enabled %}",
        "this.epml.enabled = !!features.beyondtrust;",
    ]
    pats = [r'get_bool\(\s*["\']beyondtrust_enabled["\']', r'settings\.beyondtrust_enabled',
            r'_feature_gate\(\s*["\']beyondtrust_enabled["\']',
            r'\{%\s*if\s+beyondtrust_enabled\s*%\}', r'features\.beyondtrust\b']
    for sample, pat in zip(samples, pats):
        assert re.search(pat, sample), f"pattern {pat} no longer matches {sample!r}"


# ── 2. every flag has a settings field ────────────────────────────────────────

def test_each_flag_has_a_matching_settings_field():
    """config_service.get_bool falls back to `getattr(settings, key, default)`, so a flag
    with no field of exactly that name is permanently False — the feature is off and
    nothing anywhere says why."""
    cfg = _read(_CONFIG)
    fields = _class_fields(cfg, "Settings")
    for flag in FLAGS:
        assert flag in fields, (
            f"config.py's Settings declares no `{flag}` field, so get_bool('{flag}') "
            f"falls through getattr(settings, ...) to False on every install")
    assert "beyondtrust_enabled" not in fields, (
        "the retired flag is back in config.py — with it present, a read site missed by "
        "the sweep above keeps working, and the sweep stops being proof of anything")


def test_get_bool_still_falls_back_through_settings():
    """The premise of the test above. If this changes, re-derive the trap."""
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services", "config_service.py"))
    assert "getattr(settings, key, default)" in svc, (
        "config_service.get_bool no longer falls back through settings; "
        "test_each_flag_has_a_matching_settings_field is pinning the wrong thing")


# ── 3. panel keys, models and flags line up ───────────────────────────────────

def test_the_three_panel_keys_derive_the_three_flags():
    setup = _read(_SETUP)
    assert 'return f"{feature}_enabled"' in setup, (
        "the key-derivation rule changed; re-check that password_safe/pra/epml still map "
        "to password_safe_enabled/pra_enabled/epml_enabled")
    models = re.search(r"_FEATURE_MODELS\s*=\s*\{(.*?)\n\}", setup, re.S).group(1)
    config_only = re.search(r"_CONFIG_ONLY_FEATURES\s*=\s*\{([^}]*)\}", setup).group(1)
    html = _read(_SETTINGS)
    array = html[html.index("integrations: ["):html.index("],", html.index("integrations: ["))]
    # Match real entries, not any mention: both files carry comments naming the retired
    # "beyondtrust" panel, and a bare substring check would trip over its own explanation.
    registered = set(re.findall(r'^\s*"([a-z_0-9]+)":', models, re.M))
    carded = set(re.findall(r"key:\s*'([a-z_0-9]+)'", array))
    for key in PANELS:
        assert key in registered, f"_FEATURE_MODELS has no {key} entry — its panel 404s"
        assert key in carded, f"no {key} entry in integrations[] — no card, no toggle"
        assert f'"{key}"' not in config_only, (
            f"{key} is config-only, so `enabled` is stripped on save and its toggle "
            f"never persists")
    assert "beyondtrust" not in registered and "beyondtrust" not in carded, \
        "the old combined beyondtrust panel is still registered somewhere"


def test_the_flag_map_reads_all_three():
    """Miss one and that toggle renders permanently OFF however the flag is really set —
    silent, because nothing errors. Same failure Notifications once had."""
    html = _read(_SETTINGS)
    block = html[html.index("const map = {"):html.index("};", html.index("const map = {"))]
    for key in PANELS:
        assert re.search(rf"\b{key}:\s*features\.", block), \
            f"loadFeatureFlags' map has no `{key}:` entry — its toggle is stuck off"


def test_the_routers_gate_on_the_right_flag():
    main = _read(_MAIN)
    assert '_feature_gate("epml_enabled")' in main, \
        "the EPM-L router lost its gate, or still gates on the retired flag"
    assert '_feature_gate("pra_enabled")' in main, \
        "the gateways router must gate on pra_enabled — gateways are a PRA concept"


# ── 4. the models partition the old one ───────────────────────────────────────

def test_the_three_models_partition_the_old_model():
    """Lossless and non-overlapping. Overlap is the subtler half: `enabled` being the only
    field name shared by two models is the stated premise of
    test_setup_feature_roundtrip's union check, so a real config key on two of these
    would quietly weaken that test as well as this one.
    """
    setup = _read(_SETUP)
    fields = {k: _class_fields(setup, cls) for k, cls in PANELS.items()}
    keys = list(fields)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            shared = fields[keys[i]] & fields[keys[j]]
            assert shared == {"enabled"}, (
                f"{keys[i]} and {keys[j]} share {sorted(shared - {'enabled'})} — a config "
                f"key may be declared on only one model, or one panel's save wins")
    union = set().union(*fields.values()) - {"enabled"}
    assert not (LEGACY_KEYS - union), (
        "these keys were declared on the old BeyondTrustFeatureConfig and now live on no "
        f"model, so an operator can no longer set them: {sorted(LEGACY_KEYS - union)}")
    assert not (union - LEGACY_KEYS), (
        "these are new since the split; if they are deliberate, add them to LEGACY_KEYS "
        f"with a note: {sorted(union - LEGACY_KEYS)}")


# ── 5. per-panel binding — the check the union sweep cannot make ──────────────

def test_every_bound_key_is_declared_on_its_own_panels_model():
    """The failure test_setup_feature_roundtrip is blind to.

    That test compares the bound keys against the union of *all* models, and says in its
    own docstring that this is only equivalent to a per-panel check because no real key is
    owned by two panels. It is still blind to a key that sits on the *wrong* panel: the
    union contains it, so the union check passes, while at runtime
    ``patch_feature_config`` resolves the model from the URL path segment, pydantic's
    default ``extra='ignore'`` strips the unknown field, and the value is dropped on save
    and read back blank. Splitting one panel into three is exactly how that happens.
    """
    setup = _read(_SETUP)
    blocks = _panel_blocks()
    assert set(blocks) == set(PANELS), \
        f"missing drawer markup for {sorted(set(PANELS) - set(blocks))}"
    for key, block in blocks.items():
        declared = _class_fields(setup, PANELS[key]) - {"enabled"}
        bound = set(re.findall(r"panelCfg\.([A-Za-z_][A-Za-z_0-9]*)", block))
        assert bound, f"the {key} panel binds nothing — did the block get emptied?"
        stray = bound - declared
        assert not stray, (
            f"the {key} panel renders {sorted(stray)}, which {PANELS[key]} does not "
            f"declare. PATCH /api/setup/feature/{key} validates through that model, so "
            f"the operator types a value, Save reports success, and nothing is written")


# Declared on a model but bound in no panel — the mirror of the check above. Empty on
# purpose: a key that an operator is expected to set belongs on the page. A deliberately
# env-only key goes here WITH a note saying why it is not operator-settable.
PANEL_EXEMPT_KEYS = frozenset()


def test_every_declared_key_is_bound_in_its_own_panel():
    """The other direction of the same drift, and the one that actually shipped.

    ``passwordsafe_azure_db_registration_method`` was declared on config.py's ``Settings``
    *and* on ``PasswordSafeFeatureConfig``, was read at two runtime sites
    (cloud_database_service, jumpoint_host_service), and had its own row in the
    docs/databases.md config table — but nothing in settings.html bound it, so the only
    ways to set it were the setup wizard, an env var, or a hand-rolled
    ``PATCH /api/setup/feature/password_safe``. It is the only switch that turns Azure
    database onboarding off while leaving the AWS SSM path on
    (``clouddb_ps_onboarding_enabled`` is a master toggle shared by both clouds), so the
    operator who wanted exactly that had no route through the UI at all.

    Silent, which is why it lasted: the model round-trips the key, the panel saves cleanly,
    and the control is simply absent from the page. The check above only catches the
    reverse — bound but undeclared — and the union sweep in test_setup_feature_roundtrip
    sees neither.
    """
    setup = _read(_SETUP)
    blocks = _panel_blocks()
    assert set(blocks) == set(PANELS), (
        f"missing drawer markup for {sorted(set(PANELS) - set(blocks))}")
    for key, block in blocks.items():
        declared = _class_fields(setup, PANELS[key]) - {"enabled"} - PANEL_EXEMPT_KEYS
        bound = set(re.findall(r"panelCfg\.([A-Za-z_][A-Za-z_0-9]*)", block))
        missing = sorted(declared - bound)
        assert not missing, (
            f"{PANELS[key]} declares {missing}, which the {key} panel never binds — the "
            f"key is settable only from the setup wizard, an env var or a config PATCH, "
            f"and an operator cannot find it in Settings at all. Add an x-model control to "
            f"the panel, or add the key to PANEL_EXEMPT_KEYS with a note saying why it is "
            f"deliberately not operator-settable")


def test_the_panel_slicer_really_sees_the_panels():
    """This file's own premise: if the x-if idiom changes, every per-panel check above
    silently degrades to checking an empty string."""
    blocks = _panel_blocks()
    assert len(blocks["password_safe"]) > 10000, "the Password Safe block looks truncated"
    assert "pscli_api_url" in blocks["password_safe"]
    assert "bt_api_host" in blocks["pra"]
    assert "epml_site_id" in blocks["epml"]
    # And no bleed: a key must appear in exactly one of the three blocks.
    assert "pscli_api_url" not in blocks["pra"] + blocks["epml"]


# ── 6. every string-key select keeps its x-init default ──────────────────────

def test_string_selects_keep_their_x_init_default():
    """An unset string config key reads back as "" from /api/setup/feature, not as the
    model default, so a select without its x-init renders unselected and then saves "".
    ``_read_feature`` special-cases ``bool`` and ``int`` fields back onto the model default
    for exactly this reason; the ``str`` branch is a bare ``config_service.get(field)``.

    The first five moved between panels during the split, where the x-init had to travel
    with the <select> rather than stay behind in the old block.
    ``passwordsafe_azure_db_registration_method`` needs it from the other end: the control
    was added long after the key shipped, so its config row is unset on every install that
    never set the env var."""
    html = _read(_SETTINGS)
    for key in ("passwordsafe_aws_registration_method",
                "passwordsafe_azure_registration_method",
                "passwordsafe_gcp_registration_method",
                "azure_vm_jumpoint_mode", "gcp_vm_jumpoint_mode",
                "passwordsafe_azure_db_registration_method"):
        m = re.search(rf'x-model="panelCfg\.{key}"(.{{0,200}}?)x-init="panelCfg\.{key} =',
                      html, re.S)
        assert m, (
            f"the <select> for {key} has no adjacent x-init default, so it renders "
            f"unselected on an unset key and saves an empty string")


# ── 7. the back-compat seed ───────────────────────────────────────────────────

def _seed_with(store):
    """Import feature_flag_migration against a stubbed config_service backed by `store`.

    Returns (written_pairs, return_value). Fresh module each call so no state leaks.
    """
    services = types.ModuleType("web_dashboard.services")
    # Point at the real package dir so `feature_flag_migration` still imports; only
    # config_service is stubbed, so no DB is touched.
    services.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]
    sys.modules["web_dashboard.services"] = services
    cfg = types.ModuleType("web_dashboard.services.config_service")
    written = {}
    cfg.get_opt = lambda key, workgroup=None: store.get(key)
    cfg.set_many = lambda pairs, workgroup=None: written.update(pairs)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg
    for mod in ("web_dashboard.services.feature_flag_migration",):
        sys.modules.pop(mod, None)
    import importlib
    m = importlib.import_module("web_dashboard.services.feature_flag_migration")
    return written, m.seed_beyondtrust_split()


def test_seed_no_ops_when_the_legacy_flag_was_never_set():
    """Nothing to carry, so leave all three unset and let config.py's defaults apply.
    Writing "0" here would turn the integration off for a fresh install."""
    written, n = _seed_with({})
    assert n == 0 and written == {}, f"seeded {written} from an empty store"


def test_seed_carries_an_enabled_legacy_flag():
    written, n = _seed_with({"beyondtrust_enabled": "1"})
    assert n == 3 and written == {f: "1" for f in FLAGS}, written


def test_seed_carries_a_deliberate_opt_out():
    """The case the whole seed exists for. config.py defaults all three to True, so
    without this an operator who switched BeyondTrust OFF gets three products switched
    back ON by an upgrade."""
    written, n = _seed_with({"beyondtrust_enabled": "0"})
    assert n == 3 and written == {f: "0" for f in FLAGS}, written


def test_seed_accepts_the_legacy_true_spelling():
    """Rows written before _write_feature normalised to "1"/"0" hold "True"/"False"."""
    written, _ = _seed_with({"beyondtrust_enabled": "True"})
    assert written == {f: "1" for f in FLAGS}, written


def test_seed_never_overwrites_an_explicit_choice():
    """A value set on the new build wins over a legacy one — including the case where the
    two disagree, which is what an operator changing a toggle post-upgrade looks like."""
    written, n = _seed_with({"beyondtrust_enabled": "1", "pra_enabled": "0"})
    assert n == 0 and written == {}, f"seed clobbered an explicit choice: {written}"


def test_seed_leaves_the_legacy_row_alone():
    """Copies, never moves. Deleting it would make a rollback to the previous image read
    no row at all and fall through to settings.beyondtrust_enabled = True — the silent
    re-enable the seed exists to prevent, just deferred.

    Asserted behaviourally rather than by grepping for "delete": the seed writes exactly
    the three new keys and nothing else, whatever the store holds.
    """
    for store in ({"beyondtrust_enabled": "1"}, {"beyondtrust_enabled": "0"}):
        written, _ = _seed_with(dict(store))
        assert set(written) == set(FLAGS), (
            f"the seed touched keys outside the three new flags: "
            f"{sorted(set(written) - set(FLAGS))} — the legacy row must be left as-is")


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
