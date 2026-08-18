"""Every keyword passed to a job_service function must be a real parameter.

Python raises ``TypeError: got an unexpected keyword argument`` only when the call
actually runs, so a call site that names a parameter which does not exist sits there
looking perfectly reasonable until someone clicks the button. That is exactly what
happened to the Hyper-V power buttons: ``api/hyperv.py`` called

    job_service.create_job(db, job_type=..., description=..., workgroup=..., owner_id=...)

and ``create_job`` has never had a ``description`` or an ``owner_id`` — it has
``created_by``, which was also missing. Every Start/Stop request 500'd before the job
row was written, so the page showed "failed to start" and /jobs showed nothing at all,
which reads like a permissions or agent problem rather than a broken call. The same two
kwargs were in ``api/vsphere.py`` and ``api/xcpng.py``, where an agent-bound connection
returns earlier and never reaches the line — so the break only showed on the one page
that had no agent branch.

``create_job`` is the single funnel every job row passes through and it takes nine
optional parameters, which is what makes it easy to call plausibly and wrongly. Nothing
else catches it: there is no lint in CI, the routers have no endpoint tests, and a
mismatch is invisible to an import check because the module imports fine.

Parses with ``ast`` rather than importing, so it runs with no dependencies installed and
cannot be broken by an unrelated import error. Runs under pytest, or standalone:
    python tests/test_job_service_call_kwargs.py
"""
import ast
import os
import sys

_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_WEB = os.path.join(_ROOT, "web_dashboard")
_JOB_SERVICE = os.path.join(_WEB, "services", "job_service.py")


def _signatures(path: str) -> dict:
    """``{func_name: (ordered_params, required_params, takes_kwargs)}`` from a module."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    out = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        ordered = [a.arg for a in args.posonlyargs + args.args]
        # A default fills the LAST n positional params; keyword-only defaults are their
        # own list, so they are read separately.
        n_defaulted = len(args.defaults)
        required = ordered[:len(ordered) - n_defaulted] if n_defaulted else list(ordered)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            ordered.append(arg.arg)
            if default is None:
                required.append(arg.arg)
        out[node.name] = (ordered, required, args.kwarg is not None)
    return out


def _call_sites() -> list:
    """``(func_name, positional_count, kwarg_names, starred, "path:line")`` for every
    ``job_service.<func>(...)`` call under web_dashboard/."""
    found = []
    for dirpath, dirnames, filenames in os.walk(_WEB):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full = os.path.join(dirpath, filename)
            with open(full, "r", encoding="utf-8") as handle:
                try:
                    tree = ast.parse(handle.read(), filename=full)
                except SyntaxError:
                    continue
            rel = os.path.relpath(full, _ROOT).replace(os.sep, "/")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                        and func.value.id == "job_service"):
                    continue
                # A *args or **kwargs at the call site makes the arity unknowable here.
                starred = any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords)
                found.append((
                    func.attr,
                    len(node.args),
                    [k.arg for k in node.keywords if k.arg],
                    starred,
                    f"{rel}:{node.lineno}",
                ))
    return found


def test_every_job_service_kwarg_is_a_real_parameter():
    signatures = _signatures(_JOB_SERVICE)
    bad = []
    for name, _positional, kwargs, starred, where in _call_sites():
        if starred or name not in signatures:
            continue
        ordered, _required, takes_kwargs = signatures[name]
        if takes_kwargs:
            continue
        for kwarg in kwargs:
            if kwarg not in ordered:
                bad.append(f"{where}: job_service.{name}({kwarg}=...) — no such parameter")
    assert not bad, "keyword with no matching parameter:\n  " + "\n  ".join(bad)


def test_every_job_service_call_supplies_the_required_parameters():
    """The other half of the same mistake, and the reason the Hyper-V break was a hard
    500 rather than a job row with a blank creator: dropping ``created_by`` is as fatal
    as inventing ``description``, and one edit produced both."""
    signatures = _signatures(_JOB_SERVICE)
    bad = []
    for name, positional, kwargs, starred, where in _call_sites():
        if starred or name not in signatures:
            continue
        ordered, required, _takes_kwargs = signatures[name]
        supplied = set(ordered[:positional]) | set(kwargs)
        missing = [p for p in required if p not in supplied]
        if missing:
            bad.append(f"{where}: job_service.{name}(...) — missing {', '.join(missing)}")
    assert not bad, "required parameter never supplied:\n  " + "\n  ".join(bad)


def test_create_job_still_has_no_description_or_owner_id():
    """The specific regression, stated as a fact about the signature.

    If either name is ever added, the two sweeps above stop covering the call sites that
    used to be wrong, and this test is the one that says why it mattered.
    """
    ordered, required, _ = _signatures(_JOB_SERVICE)["create_job"]
    assert "description" not in ordered
    assert "owner_id" not in ordered
    assert "created_by" in required, "created_by is what those call sites should pass"


def test_the_scan_found_the_call_sites():
    """Guards against the ast walk silently matching nothing, which would make every
    assertion above vacuously true."""
    signatures = _signatures(_JOB_SERVICE)
    assert "create_job" in signatures and "set_failed" in signatures
    sites = _call_sites()
    assert len(sites) >= 100, f"only found {len(sites)} job_service call sites"
    creates = [s for s in sites if s[0] == "create_job"]
    assert len(creates) >= 20, f"only found {len(creates)} create_job call sites"
    assert any(where.startswith("web_dashboard/api/hyperv.py")
               for *_rest, where in creates), "the regressed call site is not covered"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
