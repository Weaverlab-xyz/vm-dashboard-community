"""The database layer's source: web_dashboard/database.py, or the package that replaced it.

Tests that sweep the database layer as text (every model class by ast, an ``init_db``
body, a column declaration, a marker constant) read it through these functions so the
answer does not depend on which file a model or helper now lives in. ``database_files()``
is the one place that knows the layout: a single module, or a ``database/`` package whose
``__init__.py`` comes first.

Not a test file (no ``test_`` prefix): imported by tests, run by none.
"""
import ast
import os

_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "web_dashboard")
_MODULE = os.path.join(_WEB, "database.py")
_PACKAGE = os.path.join(_WEB, "database")


def database_files():
    """Every source file of the database layer, ``__init__.py`` first, then by path."""
    if os.path.isdir(_PACKAGE):
        found = []
        for dirpath, dirnames, filenames in os.walk(_PACKAGE):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            found.extend(os.path.join(dirpath, n) for n in sorted(filenames)
                         if n.endswith(".py"))
        found.sort(key=lambda p: (os.path.relpath(p, _PACKAGE) != "__init__.py", p))
        return found
    return [_MODULE]


def is_database_file(path):
    """True if ``path`` is part of the database layer (for sweeps that exempt it)."""
    norm = os.path.normpath(path)
    return norm in {os.path.normpath(p) for p in database_files()}


def database_source():
    """Every file of the layer, joined by blank lines, ``__init__.py`` first."""
    parts = []
    for path in database_files():
        with open(path, encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n\n".join(parts)


def database_trees():
    """``[(path, ast.Module), ...]`` for every file of the layer."""
    out = []
    for path in database_files():
        with open(path, encoding="utf-8") as fh:
            out.append((path, ast.parse(fh.read(), filename=path)))
    return out


def database_top_level_nodes():
    """The top-level ast nodes of every file of the layer, in file order."""
    return [node for _path, tree in database_trees() for node in tree.body]


def database_function(name):
    """The top-level ``def`` called ``name`` anywhere in the layer, or an AssertionError."""
    for node in database_top_level_nodes():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in the database layer")
