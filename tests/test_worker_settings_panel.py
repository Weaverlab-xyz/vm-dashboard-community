"""The Job Worker settings panel has to agree with three other places.

A concurrency cap is only useful if the number an operator types actually reaches the
worker, and there are four surfaces between the input and the supervisor: the template
binding, the Pydantic panel model (which is the whitelist — pydantic drops unknown extras,
so an unmodelled field is silently discarded on save), ``worker_policy`` (which reads it),
and ``config.py`` (the env-var default). Each pair can disagree quietly:

  * bound in the template but not on the model → the value is dropped on save and the field
    renders blank on every reload. This is not hypothetical: ``cost_budget_oci`` has been in
    exactly that state, bound at settings.html and read by cost_service but absent from
    CostExplorerFeatureConfig, because the existing guard for this
    (test_setup_feature_roundtrip.test_every_promote_runner_key_has_a_form_field) is scoped
    to one prefix;
  * on the model but read by nothing → a field that appears to work and does nothing;
  * runtime state made editable → saving the panel overwrites what the worker published.

Static: regex/AST over the template and the two modules, so it needs no fastapi, no
pydantic and no database. Deliberately, since that is what lets it run in the standalone
per-file CI pass.

Run: python tests/test_worker_settings_panel.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SETTINGS_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_SETUP_PY = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_POLICY_PY = os.path.join(_ROOT, "web_dashboard", "services", "worker_policy.py")
_WORKER_API = os.path.join(_ROOT, "web_dashboard", "api", "worker.py")
_MAIN_PY = os.path.join(_ROOT, "web_dashboard", "main.py")

_RUNTIME_STATUS_KEY = "worker_runtime_status"


def _read(path):
    return open(path, encoding="utf-8").read()


def _model_fields(class_name):
    """Annotated fields of one Pydantic model, from the AST — no pydantic import."""
    for node in ast.walk(ast.parse(_read(_SETUP_PY))):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id: ast.unparse(stmt.annotation)
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found in api/setup.py")


def _bound_in_template(prefix):
    """Every `panelCfg.<prefix>*` binding in settings.html."""
    return set(re.findall(rf"panelCfg\.({prefix}[a-z_0-9]+)", _read(_SETTINGS_HTML)))


def _registry(name):
    """A dict/set literal's string members from api/setup.py's AST."""
    for node in ast.walk(ast.parse(_read(_SETUP_PY))):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            v = node.value
            if isinstance(v, ast.Dict):
                return {k.value for k in v.keys}
            if isinstance(v, (ast.Set, ast.Tuple, ast.List)):
                return {e.value for e in v.elts}
            if isinstance(v, ast.Call):          # frozenset({...})
                return {e.value for e in v.args[0].elts}
    raise AssertionError(f"{name} not found in api/setup.py")


# ── the four surfaces agree ──────────────────────────────────────────────────

def test_every_model_field_is_bound_in_the_template():
    """Otherwise the field exists, is saved by nothing, and is invisible."""
    model = set(_model_fields("WorkerFeatureConfig")) - {"enabled"}
    bound = _bound_in_template("worker_")
    missing = sorted(model - bound)
    assert not missing, f"no form field in settings.html for: {missing}"


def test_every_bound_field_is_on_the_model():
    """The failure mode that has actually shipped here before. patch_feature_config
    validates through the model and pydantic ignores unknown extras, so a bound-but-
    unmodelled key is accepted by the UI, dropped on the way to the database, and reads
    back blank forever — with no error anywhere."""
    model = set(_model_fields("WorkerFeatureConfig")) - {"enabled"}
    bound = _bound_in_template("worker_")
    orphans = sorted(bound - model)
    assert not orphans, (
        f"bound in settings.html but not on WorkerFeatureConfig: {orphans} — pydantic "
        "drops unknown extras, so anything typed into these is silently discarded on save")


def test_every_model_field_is_read_by_the_policy():
    """A cap nothing reads is a control that does nothing. Mirrors
    test_notifications_settings_panel.test_panel_fields_match_what_the_policy_reads."""
    model = set(_model_fields("WorkerFeatureConfig")) - {"enabled"}
    policy = _read(_POLICY_PY)
    unread = sorted(f for f in model if f'"{f}"' not in policy)
    assert not unread, f"the panel offers keys worker_policy never reads: {unread}"


def test_every_field_is_a_plain_int():
    """_read_feature keys off `info.annotation is int`. An Optional[int] reads back as ""
    for an unset key and 422s the WHOLE panel on save, and a float would round-trip
    "3.0" into an int field — the regression test_setup_feature_roundtrip pins."""
    for name, annotation in _model_fields("WorkerFeatureConfig").items():
        if name == "enabled":
            assert annotation == "bool", f"enabled is annotated {annotation}"
            continue
        assert annotation == "int", (
            f"{name} is annotated {annotation!r}; every numeric panel field must be a "
            "plain int — Optional[int] 422s the panel and float breaks the round trip")


def test_the_defaults_match_config_py():
    """The panel's default is the placeholder an operator sees before they have saved
    anything. If it disagrees with config.py, the displayed value is not the value in use."""
    model_defaults = {}
    for node in ast.walk(ast.parse(_read(_SETUP_PY))):
        if isinstance(node, ast.ClassDef) and node.name == "WorkerFeatureConfig":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                        and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, int) \
                        and not isinstance(stmt.value.value, bool):
                    model_defaults[stmt.target.id] = stmt.value.value
    config_src = _read(os.path.join(_ROOT, "web_dashboard", "config.py"))
    for field, want in model_defaults.items():
        m = re.search(rf"^\s+{re.escape(field)}: int = (\d+)", config_src, re.M)
        assert m, f"{field} is not a Settings field in config.py"
        assert int(m.group(1)) == want, (
            f"{field}: panel default {want}, config.py default {m.group(1)}")


# ── registration ─────────────────────────────────────────────────────────────

def test_the_feature_is_registered_and_has_no_enable_toggle():
    """"worker" must be in _FEATURE_MODELS (or PATCH 404s) and in _CONFIG_ONLY_FEATURES —
    the worker has no off position, so an enable toggle could only mislead. Being
    config-only is also what exempts it from the loadFeatureFlags map that
    test_settings_integrations requires of every toggled feature."""
    assert "worker" in _registry("_FEATURE_MODELS"), (
        'no "worker" entry in _FEATURE_MODELS — PATCH /api/setup/feature/worker would 404')
    assert "worker" in _registry("_CONFIG_ONLY_FEATURES"), (
        '"worker" is not config-only, so the panel would render an enable toggle for a '
        "process that cannot be turned off")


def test_the_panel_is_registered_in_the_ui():
    """Mirrors test_notify_wiring.test_the_settings_panel_is_registered_in_the_ui: a model
    with no integrations[] entry is unreachable, and an entry with no panel block opens an
    empty slide-over."""
    html = _read(_SETTINGS_HTML)
    assert "key: 'worker'" in html, "no entry in the integrations list"
    assert "panel?.key === 'worker'" in html, "no slide-over panel block"
    assert "configOnly: true" in html.split("key: 'worker'")[1].split("},")[0], (
        "the integrations entry is not marked configOnly, so the UI shows a toggle that "
        "_write_feature will refuse to honour")


def test_the_status_readout_is_wired_end_to_end():
    """The readout is the only place a clamped cap becomes visible, so all four links
    matter: the endpoint, its registration, the lazy load, and the formatter."""
    api = _read(_WORKER_API)
    assert '@router.get("/status")' in api
    assert "require_admin" in api, "the status route is not admin-gated"
    assert 'prefix="/api/worker"' in api

    main = _read(_MAIN_PY)
    assert "worker as worker_api" in main, "api/worker.py is never imported"
    assert "include_router(worker_api.router)" in main, "the worker router is not mounted"

    html = _read(_SETTINGS_HTML)
    assert "loadWorkerStatus()" in html and "workerStatusSummary()" in html
    assert "/api/worker/status" in html, "the panel never calls the status endpoint"


def test_the_panel_links_to_a_doc_that_exists():
    """The panel can only carry a few sentences per field; the deployment surface — how to
    run the worker as its own Container App, the connection budget, the sizing — lives in the
    doc. A renamed file turns that into a 404 with nothing to catch it, since /docs/<page> is
    resolved from the filesystem at request time."""
    html = _read(_SETTINGS_HTML)
    panel = html.split("panel?.key === 'worker'")[1].split("</template>")[0]
    links = re.findall(r'href="/docs/([a-z0-9-]+)"', panel)
    assert links, "the Job Worker panel links to no documentation"
    for slug in links:
        assert os.path.isfile(os.path.join(_ROOT, "docs", f"{slug}.md")), \
            f"the panel links to /docs/{slug} but docs/{slug}.md does not exist"


def test_runtime_state_is_not_editable():
    """Runtime state the worker writes must never be a panel field: saving the panel would
    overwrite the worker's own readout. Same rule as resource_expiry_last_sweep and
    notify_last_scan_at."""
    model = set(_model_fields("WorkerFeatureConfig"))
    assert _RUNTIME_STATUS_KEY not in model, (
        f"{_RUNTIME_STATUS_KEY} is editable; saving the panel would clobber it")
    assert _RUNTIME_STATUS_KEY not in _bound_in_template("worker_"), (
        f"{_RUNTIME_STATUS_KEY} is bound as an input in settings.html")


def test_the_db_pool_settings_are_not_on_the_panel():
    """db_pool_size / db_max_overflow cannot be config-driven: create_engine runs at import
    in database.py, before any connection exists, so the pool that connects to the database
    cannot be sized from a value stored in it. Offering them would produce a field that
    appears to work and is read exactly never."""
    model = set(_model_fields("WorkerFeatureConfig"))
    for key in ("db_pool_size", "db_max_overflow", "db_pool_timeout_s", "db_pool_recycle_s"):
        assert key not in model, (
            f"{key} is on the panel, but the engine is built at import before config can "
            "be read from the database — the field could never take effect")
        assert not _bound_in_template(key), f"{key} is bound as an input in settings.html"


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
