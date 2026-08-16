"""Every keyword passed to a SQLAlchemy model constructor must be a real column.

SQLAlchemy raises ``TypeError: '<x>' is an invalid keyword argument`` only when the
constructor actually runs, so a mismatch can sit in a rarely-taken branch
indefinitely. That is exactly what happened with
``EntitleActivation(tenant_id=...)``: ``cloud_identity_service`` had always passed
it, no column existed, and every elevation would have raised the moment
``cloud_identity_gate_enabled`` was switched on — a code path no test exercised.

Parses with ``ast`` rather than importing, so it runs with no dependencies
installed and cannot be broken by an unrelated import error.
"""
import ast
import os
import sys

_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_WEB = os.path.join(_REPO_ROOT, "web_dashboard")
_DATABASE_PY = os.path.join(_WEB, "database.py")

# SQLAlchemy accepts these on any declarative model regardless of columns.
_ALWAYS_OK = {"_sa_instance_state"}


def _model_columns() -> dict:
    """``{ModelName: {column_attr, ...}}`` from database.py's class bodies."""
    with open(_DATABASE_PY, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_DATABASE_PY)
    models = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        columns = set()
        is_model = False
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                name = stmt.targets[0].id
                if name == "__tablename__":
                    is_model = True
                    continue
                value = stmt.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                        and value.func.id in ("Column", "relationship"):
                    columns.add(name)
        if is_model:
            models[node.name] = columns
    return models


def _constructor_kwargs() -> list:
    """``(ModelName, kwarg, "path:line")`` for every model constructed by keyword."""
    models = _model_columns()
    found = []
    for dirpath, dirnames, filenames in os.walk(_WEB):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "functions")]
        for filename in sorted(filenames):
            if not filename.endswith(".py") or filename == "database.py":
                continue
            full = os.path.join(dirpath, filename)
            with open(full, "r", encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read(), filename=full)
                except SyntaxError:
                    continue
            rel = os.path.relpath(full, _REPO_ROOT).replace(os.sep, "/")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in models:
                    continue
                for keyword in node.keywords:
                    if keyword.arg:
                        found.append((node.func.id, keyword.arg, f"{rel}:{node.lineno}"))
    return found


def test_every_model_constructor_kwarg_is_a_column():
    models = _model_columns()
    bad = []
    for model, kwarg, where in _constructor_kwargs():
        if kwarg in _ALWAYS_OK or kwarg in models[model]:
            continue
        bad.append(f"{where}: {model}({kwarg}=...) — no such column")
    assert not bad, "constructor kwarg with no matching column:\n  " + "\n  ".join(bad)


def test_entitle_activation_has_tenant_id():
    """The specific regression: cloud_identity_service passes it unconditionally."""
    assert "tenant_id" in _model_columns()["EntitleActivation"]


def test_the_scan_found_models_and_call_sites():
    """Guards against the ast walk silently matching nothing, which would make the
    assertions above vacuously true."""
    models = _model_columns()
    assert len(models) >= 10, f"only parsed {len(models)} models from database.py"
    assert "EntitleActivation" in models and "Job" in models
    assert len(_constructor_kwargs()) >= 20, "found almost no constructor call sites"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
