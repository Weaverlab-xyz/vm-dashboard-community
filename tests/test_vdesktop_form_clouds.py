"""The desktop-pool form and the seat backends behind it, pinned to each other.

`tests/test_vdesktop_seats.py` already asserts that PROVISIONING_CLOUDS is derived from
_SEAT_BACKENDS, so the advertised set cannot drift from the implemented one. That guard
proved the backend registry consistent WITH ITSELF, and nothing tied it to the form — so
for a whole phase the AWS and GCP backends provisioned real VMs while the page offered
them as "AWS (records only)", `canSubmit()` returned true unconditionally for both, and
`submitCreate()` posted `{cloud, name, count}`. Every AWS or GCP pool created from the UI
was a guaranteed 400 from `validate_spec`, naming four fields the form never collected.

These tests read SOURCE TEXT and parse the Python with `ast`. They import nothing from
the app on purpose: `vdesktop_service` imports `..database`, so an importing test SKIPs
on a machine without the app's dependencies — which is where the edits get made. Same
reasoning as `tests/test_region_config.py::test_setup_models_match_region_fields_without_importing_fastapi`.

Run: python tests/test_vdesktop_form_clouds.py   (or under pytest)
"""
import ast
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(_ROOT, "web_dashboard", "templates", "desktops", "index.html")
SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "vdesktop_service.py")
ROUTER = os.path.join(_ROOT, "web_dashboard", "api", "desktops.py")


def _read(path):
    return io.open(path, encoding="utf-8").read()


TPL = _read(TEMPLATE)


def _seat_backends():
    """The clouds `_SEAT_BACKENDS` maps, straight off the dict literal."""
    for node in ast.walk(ast.parse(_read(SERVICE))):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_SEAT_BACKENDS" for t in node.targets):
            return {k.value for k in node.value.keys}
    raise AssertionError("_SEAT_BACKENDS is not a dict literal any more")


def _backend_classes():
    """`{cloud: {attr: literal}}` for the class-body constants of each seat backend."""
    out = {}
    for node in ast.walk(ast.parse(_read(SERVICE))):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Seats"):
            continue
        attrs = {}
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)):
                attrs[stmt.targets[0].id] = stmt.value.value
        if "cloud" in attrs:
            out[attrs["cloud"]] = attrs
    return out


def _required_fields():
    """`{cloud: (field, ...)}` from the _<CLOUD>_REQUIRED tuples."""
    out = {}
    for node in ast.walk(ast.parse(_read(SERVICE))):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and re.fullmatch(r"_([A-Z]+)_REQUIRED", node.targets[0].id)):
            cloud = node.targets[0].id.split("_")[1].lower()
            out[cloud] = tuple(e.value for e in node.value.elts)
    return out


def _spec_builders():
    """`{cloud: {spec_key: source of the value expression}}` for each `_<cloud>_spec`."""
    out = {}
    for node in ast.walk(ast.parse(_read(ROUTER))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        m = re.fullmatch(r"_([a-z]+)_spec", node.name)
        if not m:
            continue
        # Simple local bindings are resolved before the dict is read. A builder that
        # computes `region_id = payload.region or _cfg("aws_region")` and then writes
        # `"region": region_id` has a fallback; reading the dict alone would see a bare
        # name and call it missing. Substituting keeps this test about whether a
        # fallback EXISTS rather than about where the author put it.
        locals_ = {}
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                locals_[stmt.targets[0].id] = ast.unparse(stmt.value)
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
                out[m.group(1)] = {
                    k.value: locals_.get(ast.unparse(v), ast.unparse(v))
                    for k, v in zip(stmt.value.keys, stmt.value.values)
                    if isinstance(k, ast.Constant)
                }
    return out


def _cloud_options():
    """The `<option value="...">` set inside the form's Cloud <select>."""
    m = re.search(r'<select x-model="form\.cloud".*?</select>', TPL, re.S)
    assert m, "the cloud <select> is not recognisable any more"
    return set(re.findall(r'<option value="([a-z]+)"', m.group(0)))


def _body_for():
    """`{cloud: source}` for each entry of the template's `_bodyFor` map."""
    m = re.search(r"_bodyFor:\s*\{(.*?)\n    \},", TPL, re.S)
    assert m, "_bodyFor is not recognisable any more"
    body = m.group(1)
    out = {}
    for cloud in ("azure", "aws", "gcp"):
        e = re.search(r"\n      %s: f => \(\{(.*?)\}\)," % cloud, body, re.S)
        if e:
            out[cloud] = e.group(1)
    return out


# ─ The tests ─────────────────────────────────


def test_the_cloud_picker_offers_exactly_the_clouds_with_a_seat_backend():
    """The one that would have caught the whole thing, in both directions.

    A cloud in the picker with no backend hands an operator seat rows and no VMs. A
    cloud with a backend and no option is a feature nobody can reach."""
    assert _cloud_options() == _seat_backends()


def test_every_offered_cloud_has_its_own_field_block_and_body_entry():
    """An option with no `x-if` block collects nothing; with no `_bodyFor` entry it
    posts `{cloud, name, count}`. Either way the create is a guaranteed 400."""
    body = _body_for()
    for cloud in _cloud_options():
        assert "form.cloud === '%s'" % cloud in TPL, (
            "%s is offered in the picker with no field block" % cloud)
        assert cloud in body, "%s is offered with no _bodyFor entry" % cloud


def test_every_required_spec_field_is_on_the_form_or_defaulted_server_side():
    """The load-bearing one: it states the RULE rather than the instance.

    For each cloud, every name its `validate_spec` demands must either be sent by that
    cloud's `_bodyFor` entry or have a fallback in the spec builder's value expression.
    A required field with neither is exactly the 400 this file exists to prevent, and
    it is invisible in review because the two halves live in different files."""
    builders = _spec_builders()
    body = _body_for()
    required = _required_fields()
    # ssh_public_key is required by every backend but appears in none of the
    # _<CLOUD>_REQUIRED tuples - each validate_spec appends it by hand.
    fallbacks = ("_cfg(", "rc[", "region[", "_configured_ssh_key", "'", '"')
    for cloud in sorted(_cloud_options()):
        fields = tuple(required.get(cloud, ())) + ("ssh_public_key",)
        for field in fields:
            if field in body.get(cloud, ""):
                continue
            expr = builders.get(cloud, {}).get(field)
            assert expr is not None, (
                "%s requires %r and the spec builder never sets it" % (cloud, field))
            assert any(f in expr for f in fallbacks), (
                "%s requires %r; the form does not send it and %s has no server-side "
                "fallback: %s" % (cloud, field, "_%s_spec" % cloud, expr))


def test_the_submit_gate_refuses_a_cloud_it_does_not_know():
    """`canSubmit()` used to end `return true`, so Create was enabled for any cloud
    without a branch - including a future one somebody adds to the picker first."""
    m = re.search(r"\n    canSubmit\(\) \{(.*?)\n    \},", TPL, re.S)
    assert m, "canSubmit is not recognisable any more"
    returns = re.findall(r"return\s+(true|false)\s*;", m.group(1))
    assert returns and returns[-1] == "false", (
        "canSubmit falls through to %r; an unknown cloud must not be submittable"
        % (returns[-1] if returns else None))


def test_a_linux_only_cloud_is_not_offered_a_windows_option():
    """A backend that refuses Windows must not have a form that can ask for it. The
    Guest-OS control belongs only to the cloud whose backend can honour it."""
    for cloud, attrs in _backend_classes().items():
        if attrs.get("supports_windows"):
            continue
        block = re.search(
            r"<template x-if=\"form\.cloud === '%s'\">(.*?)\n        </template>" % cloud,
            TPL, re.S)
        assert block, "no field block for %s" % cloud
        assert "Windows" not in block.group(1) or "Linux-only" in block.group(1), (
            "%s is Linux-only but its field block offers Windows" % cloud)
        assert "os_type: 'Linux'" in _body_for().get(cloud, ""), (
            "%s must pin os_type to Linux; otherwise a stale Azure selection is "
            "carried into a backend that refuses it" % cloud)


def test_the_page_no_longer_advertises_records_only():
    """The strings the backend outgrew. Kept as a test because prose drifts silently:
    nothing else in CI reads this page."""
    visible = re.sub(r"\{#.*?#\}", "", TPL, flags=re.S)   # the Jinja comment may cite them
    for stale in ("records only", "records-only", "Phase 1", "Phase 2", "coming next"):
        assert stale not in visible, "the page still says %r" % stale


def test_the_page_says_gateway_not_jumpoint():
    """House terminology - `test_gateway_terminology` scans templates for this too, but
    a page-local assertion fails where the edit is made."""
    assert "Jumpoint" not in TPL


def _run_tests():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_tests())
