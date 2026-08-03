"""Unit tests for services/worker_policy.py — the numbers behind worker concurrency.

The headline tests here are :func:`test_no_cap_can_be_configured_to_zero` and
:func:`test_every_cap_has_a_ceiling_a_typo_cannot_defeat`. The first pins the property
that makes these knobs safe to expose in Settings: a cap of 0 reads like "pause the
queue" and would behave like a queue that silently never drains — nothing failed,
nothing logged, jobs just stop. The second pins the other direction: a slider or a fat
finger cannot ask one process to run 500 terraform applies against a 1-vCore Postgres.

:func:`test_the_three_copies_of_every_default_agree` is the boring one that will
actually catch a regression. Each default is written three times — here, in config.py,
and in api/setup.py's panel model — and a mismatch is invisible until a user's saved
value differs from the placeholder they were shown.

Pure Python: ``config_service`` and ``config.settings`` are stubbed, no DB, no deps.
Runs under pytest, or standalone:
    python tests/test_worker_policy.py
"""
import ast
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Simulated config store, keyed exactly as app_config is. Unset keys resolve to "" —
# what the real config_service.get returns for a key nobody wrote.
CONF = {}
WRITES = {}


def _install_stubs():
    """Register stub parent packages so worker_policy's relative imports resolve while it
    is loaded by file path (mirrors test_notify_policy._install_stubs). Loading by path is
    what keeps this runnable on a bare checkout — and what makes the module's
    stdlib-only-at-import-time rule load-bearing rather than aspirational."""
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = []
    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    cfg.set = lambda key, value, workgroup=None: WRITES.__setitem__(key, value)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = conf_mod


_install_stubs()
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "worker_policy.py")
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.worker_policy", _PATH)
pol = importlib.util.module_from_spec(_spec)
sys.modules["web_dashboard.services.worker_policy"] = pol
_spec.loader.exec_module(pol)

_CONFIG_PY = os.path.join(_ROOT, "web_dashboard", "config.py")
_SETUP_PY = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")

# key -> (accessor name, policy default constant, ceiling constant or None)
_CAPS = {
    "worker_heavy_concurrency": ("heavy_cap", "_DEFAULT_HEAVY", "HEAVY_CAP_CEILING"),
    "worker_medium_concurrency": ("medium_cap", "_DEFAULT_MEDIUM", "MEDIUM_CAP_CEILING"),
    "worker_light_concurrency": ("light_cap", "_DEFAULT_LIGHT", "LIGHT_CAP_CEILING"),
    "worker_max_concurrency": ("max_concurrency", "_DEFAULT_TOTAL", "TOTAL_CAP_CEILING"),
}
_OTHERS = {
    "worker_executor_threads": ("executor_threads", "_DEFAULT_EXECUTOR_THREADS",
                                "EXECUTOR_THREADS_CEILING"),
    "worker_drain_timeout_s": ("drain_timeout_s", "_DEFAULT_DRAIN_TIMEOUT_S",
                               "DRAIN_TIMEOUT_CEILING_S"),
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _set(**overrides):
    CONF.clear()
    CONF.update({k: str(v) for k, v in overrides.items()})


def _module_int_defaults(path, names):
    """Read ``name: int = <literal>`` / ``name = <literal>`` assignments out of a module's
    AST. Reading the source rather than importing keeps this test free of fastapi and
    pydantic, which the standalone CI run does not install for it."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = {}

    def _walk(body):
        for node in body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            if target in names and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, int) and target not in found:
                found[target] = node.value.value
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(node, attr, None)
                if inner:
                    _walk(inner)
    _walk(tree.body)
    return found


# ── the two properties that make these knobs safe to expose ──────────────────

def test_no_cap_can_be_configured_to_zero():
    """A cap of 0 would look like "pause the queue" and behave like a queue that never
    drains: nothing claimed, nothing failed, nothing logged. Pausing is not what these
    knobs are for, so the floor is 1 and a 0 (or a negative) is silently raised to it."""
    _set(**{k: 0 for k in _CAPS})
    for key, (fn, _, _) in _CAPS.items():
        assert getattr(pol, fn)() >= 1, f"{key} resolved below 1 from '0'"
    _set(**{k: -3 for k in _CAPS})
    for key, (fn, _, _) in _CAPS.items():
        assert getattr(pol, fn)() >= 1, f"{key} resolved below 1 from '-3'"


def test_every_cap_has_a_ceiling_a_typo_cannot_defeat():
    """The ceilings bound blast radius, not taste: heavy is capped because every streamed
    output line is a JobLog INSERT, and light because each poller holds a pooled
    connection and an executor thread for up to two hours."""
    _set(**{k: 999 for k in list(_CAPS) + list(_OTHERS)})
    for key, (fn, _, ceiling) in {**_CAPS, **_OTHERS}.items():
        assert getattr(pol, fn)() == getattr(pol, ceiling), \
            f"{key} exceeded {ceiling} when configured to 999"


def test_the_ceilings_are_above_the_defaults():
    """A ceiling at or below its own default would silently make the knob one-way."""
    for key, (fn, default, ceiling) in {**_CAPS, **_OTHERS}.items():
        assert getattr(pol, ceiling) >= getattr(pol, default), \
            f"{ceiling} is below {default} — {key} could never be raised"


# ── resolution order ─────────────────────────────────────────────────────────

def test_config_beats_settings_beats_the_literal_default():
    from web_dashboard.config import settings
    _set()
    assert pol.heavy_cap() == pol._DEFAULT_HEAVY          # neither source has it
    settings.worker_heavy_concurrency = 3
    try:
        assert pol.heavy_cap() == 3                       # settings (env) wins over literal
        _set(worker_heavy_concurrency=4)
        assert pol.heavy_cap() == 4                       # config (Settings page) wins over env
    finally:
        del settings.worker_heavy_concurrency
        _set()


def test_a_non_numeric_setting_falls_back_instead_of_raising():
    """A hand-edited app_config row, or a value typed into a field that isn't validated
    server-side, must not take the worker down on its next supervisor pass."""
    _set(worker_light_concurrency="lots")
    assert pol.light_cap() == pol._DEFAULT_LIGHT
    _set(worker_heavy_concurrency="")
    assert pol.heavy_cap() == pol._DEFAULT_HEAVY
    _set(worker_drain_timeout_s="soon")
    assert pol.drain_timeout_s() == pol._DEFAULT_DRAIN_TIMEOUT_S


def test_zero_is_meaningful_for_the_two_non_caps():
    """executor_threads=0 means "derive it from the caps" and drain=0 means "don't wait",
    so unlike the caps these two must NOT be floored at 1."""
    _set(worker_executor_threads=0, worker_drain_timeout_s=0)
    assert pol.executor_threads() == 0
    assert pol.drain_timeout_s() == 0


def test_caps_returns_all_four_in_one_read():
    _set(worker_heavy_concurrency=2, worker_medium_concurrency=1,
         worker_light_concurrency=3, worker_max_concurrency=3)
    assert pol.caps() == {"heavy": 2, "medium": 1, "light": 3, "total": 3}


# ── the defaults are written three times and must agree ──────────────────────

def test_the_three_copies_of_every_default_agree():
    """worker_policy._DEFAULT_*, config.py's Settings field, and api/setup.py's panel
    model each carry the same literal. A mismatch shows up as a Settings field whose
    placeholder differs from what the worker actually uses — invisible until someone
    saves the panel and the behaviour changes without them editing anything."""
    keys = list(_CAPS) + list(_OTHERS)
    in_config = _module_int_defaults(_CONFIG_PY, set(keys))
    in_setup = _module_int_defaults(_SETUP_PY, set(keys))
    for key, (_, default, _) in {**_CAPS, **_OTHERS}.items():
        want = getattr(pol, default)
        assert key in in_config, f"{key} is not a Settings field in config.py"
        assert in_config[key] == want, \
            f"{key}: config.py has {in_config[key]}, worker_policy.{default} is {want}"
        assert key in in_setup, \
            f"{key} is not on the panel model in api/setup.py — it can't be edited"
        assert in_setup[key] == want, \
            f"{key}: api/setup.py has {in_setup[key]}, worker_policy.{default} is {want}"


def test_the_tier_caps_can_cover_the_aggregate():
    """The tier caps bound composition and max_concurrency bounds the total, so the tiers
    are allowed to sum higher — but if they summed LOWER the aggregate would be dead
    config that could never be reached."""
    assert (pol._DEFAULT_HEAVY + pol._DEFAULT_MEDIUM + pol._DEFAULT_LIGHT) \
        >= pol._DEFAULT_TOTAL


def test_the_default_pool_can_serve_the_default_aggregate():
    """jobs_worker._limits clamps concurrency to roughly (pool - 4) / 2. If the shipped
    pool can't serve the shipped caps, every worker logs a clamp warning on boot and the
    defaults are a lie."""
    pool = _module_int_defaults(_CONFIG_PY, {"db_pool_size", "db_max_overflow"})
    capacity = pool["db_pool_size"] + pool["db_max_overflow"]
    assert capacity - 4 >= 2 * pol._DEFAULT_TOTAL, (
        f"pool of {capacity} cannot serve {pol._DEFAULT_TOTAL} concurrent jobs "
        f"(needs {2 * pol._DEFAULT_TOTAL + 4})")


# ── runtime status readout ───────────────────────────────────────────────────

def test_status_reports_never_run_before_any_worker_publishes():
    """A fresh install, or one still on the old image, must render as "never run" rather
    than as an empty card an operator reads as breakage."""
    _set()
    assert pol.get_runtime_status() == {"never_run": True}


def test_status_reports_corrupted_rather_than_crashing():
    """Mirrors expiry_reaper.get_last_sweep_result: an operator reading "corrupted"
    learns more than one staring at a blank panel, and the endpoint must not 500."""
    _set(worker_runtime_status="{not json")
    got = pol.get_runtime_status()
    assert got["corrupted"] is True
    assert got["raw_preview"].startswith("{not json")


def test_publishing_round_trips_through_config():
    WRITES.clear()
    pol.publish_runtime_status(
        configured={"heavy": 4}, effective={"heavy": 2},
        clamp_reason="pool", executor_threads_used=24, pool_capacity=10, in_flight=1)
    assert pol._RUNTIME_STATUS_KEY in WRITES
    blob = json.loads(WRITES[pol._RUNTIME_STATUS_KEY])
    assert blob["configured"] == {"heavy": 4}
    assert blob["effective"] == {"heavy": 2}
    assert blob["clamp_reason"] == "pool"
    assert blob["hostname"]          # so several replicas' readouts are distinguishable
    assert blob["published_at"].endswith("Z")
    CONF["worker_runtime_status"] = WRITES[pol._RUNTIME_STATUS_KEY]
    assert pol.get_runtime_status()["effective"] == {"heavy": 2}


def test_publishing_never_raises():
    """The worker's job is to run jobs. Losing a status readout must not take it down —
    same contract as expiry_reaper._persist_result."""
    cfg = sys.modules["web_dashboard.services.config_service"]
    original = cfg.set
    cfg.set = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db is down"))
    try:
        pol.publish_runtime_status(configured={}, effective={})   # must not raise
    finally:
        cfg.set = original


def test_the_status_key_is_not_a_configurable_cap():
    """Runtime state the worker writes must never become an editable field, for the same
    reason resource_expiry_last_sweep isn't one: saving the panel would overwrite it."""
    assert pol._RUNTIME_STATUS_KEY not in _CAPS
    assert pol._RUNTIME_STATUS_KEY not in _OTHERS
    in_setup = _module_int_defaults(_SETUP_PY, {pol._RUNTIME_STATUS_KEY})
    assert not in_setup, f"{pol._RUNTIME_STATUS_KEY} is a field on a panel model"


# ── purity ───────────────────────────────────────────────────────────────────

def test_the_module_imports_only_stdlib_at_module_level():
    """Loading by file path above is what proves this, but assert it directly so the
    reason is documented: a module-level `from . import config_service` would drag
    sqlalchemy into a test that has no database, and the standalone CI run would import
    the world to check arithmetic."""
    tree = ast.parse(open(_PATH, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, \
                f"relative import at module level: .{node.module or ''} (line {node.lineno})"
            assert node.module in ("datetime",), f"non-stdlib import: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in ("json", "logging", "socket"), \
                    f"non-stdlib import: {alias.name}"


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
