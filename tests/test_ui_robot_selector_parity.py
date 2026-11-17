"""UI robot: every selector key a flow asks for exists in the table it asks.

`runners/ui-robot/robot/selectors.py` ships every locator as TODO and `need()` raises on one,
so an unrecorded selector already fails loudly. What `need()` cannot catch is a KEY that is
absent from the table (`table[key]` is a bare KeyError, mid-run, against a real tenant), or an
entry no flow reads any more. Both are visible statically, so this reads the two files with
`ast` -- neither is imported for its parse, because flows.py needs pyotp and a browser runner.

Pinned:
  * a literal-table call `need(PS, "k")` needs "k" in PS; likewise PRA
  * a call through a variable table (`_login`'s `table`) is checked against BOTH tables
  * no table entry is unused by every flow
  * `need()` still refuses TODO and names the key

Runs under pytest, or standalone:
    python tests/test_ui_robot_selector_parity.py
"""
import ast
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROBOT = os.path.join(_ROOT, "runners", "ui-robot", "robot")
_TABLES = ("PS", "PRA")


def _parse(name):
    with open(os.path.join(_ROBOT, name), encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=name)


def _defined():
    """{'PS': {keys}, 'PRA': {keys}} from the dict literals in selectors.py."""
    out = {}
    for node in _parse("selectors.py").body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _TABLES and isinstance(node.value, ast.Dict)):
            out[node.targets[0].id] = {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return out


def _used():
    """[(table name or None for a variable table, key)] for every need() call in flows.py."""
    uses = []
    for node in ast.walk(_parse("flows.py")):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "need" and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            table = node.args[0]
            name = table.id if isinstance(table, ast.Name) and table.id in _TABLES else None
            uses.append((name, node.args[1].value))
    return uses


def test_both_tables_are_found_and_flows_use_them():
    defined, used = _defined(), _used()
    assert set(defined) == set(_TABLES), defined.keys()
    # Floors, so a changed shape cannot make the walk match nothing and still pass.
    assert all(len(v) >= 5 for v in defined.values()), {k: len(v) for k, v in defined.items()}
    assert len(used) >= 10, len(used)


def test_every_key_a_flow_asks_for_exists():
    defined = _defined()
    missing = []
    for table, key in _used():
        for t in ([table] if table else _TABLES):
            if key not in defined[t]:
                missing.append(f"{t}[{key!r}]")
    assert not missing, "flows ask for selectors no table defines: " + ", ".join(missing)


def test_no_table_entry_is_dead():
    defined, used = _defined(), _used()
    read = {t: set() for t in _TABLES}
    for table, key in used:
        for t in ([table] if table else _TABLES):
            read[t].add(key)
    dead = [f"{t}[{k!r}]" for t in _TABLES for k in sorted(defined[t] - read[t])]
    assert not dead, "selectors no flow reads: " + ", ".join(dead)


def test_need_refuses_todo_and_names_the_key():
    spec = importlib.util.spec_from_file_location(
        "ui_robot_selectors_t", os.path.join(_ROBOT, "selectors.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        mod.need({"login_user": mod.TODO}, "login_user")
    except RuntimeError as e:
        assert "login_user" in str(e)
    else:
        raise AssertionError("need() returned a TODO selector instead of refusing")
    assert mod.need({"k": "#real"}, "k") == "#real"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
