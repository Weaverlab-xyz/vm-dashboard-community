"""Every TemplateResponse passes the request first.

Starlette 1.x takes ``TemplateResponse(request, name, context)``. The older order,
``TemplateResponse(name, {"request": request})``, raises ``TypeError: unhashable type:
'dict'`` at render, so the page answers 500. When FastAPI 0.143 / Starlette 1.7 came in
for their security fixes, 44 page routes in main.py still used it: login, setup, the
dashboard, every page. Only one test (the Swagger page) rendered a page and noticed, so
this pins the call shape statically, for every module, rather than relying on a page
happening to be rendered by some test.

The new order also works on the old Starlette (since 0.29), so it is safe either way.

Runs under pytest, or standalone:
    python tests/test_template_response_order.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "web_dashboard")


def _old_style_calls():
    found = []
    for dirpath, _, files in os.walk(_PKG):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            src = open(path, encoding="utf-8").read()
            if "TemplateResponse" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) == "TemplateResponse"
                        and node.args
                        and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))):
                    found.append(f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
    return found


def test_every_template_response_passes_the_request_first():
    old = _old_style_calls()
    assert not old, (
        "TemplateResponse called with the template name first; under Starlette 1.x that "
        "page returns 500. Use TemplateResponse(request, name, context):\n  "
        + "\n  ".join(old))


def test_the_check_would_catch_the_old_order():
    tree = ast.parse('templates.TemplateResponse("x.html", {"request": request})')
    call = tree.body[0].value
    assert isinstance(call.args[0], ast.Constant), "the guard's detection no longer matches"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
