"""examples/functions/ stays valid and in step with the real workloads.

Matches the tests/test_k8s_samples.py + test_compose_samples.py convention: sample
payloads are documentation people copy, so a stale one is a support ticket.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_EXAMPLES = os.path.join(_REPO_ROOT, "examples", "functions")

from web_dashboard import functions  # noqa: E402,F401  (puts fnruntime on sys.path)
from web_dashboard.services import cloud_function_package as pkg  # noqa: E402


# Which route each sample payload exercises. Adapters route on the path, so a
# sample without one would only ever prove the 404 branch works.
_SAMPLE_ROUTE = {
    "db_grant": ("POST", "/give_access"),
    "portainer_access": ("POST", "/give_access"),
    "entitle_webhook_echo": ("POST", "/give_access"),
}


def _sample_files() -> list:
    return sorted(f for f in os.listdir(_EXAMPLES) if f.endswith(".request.json"))


def test_examples_directory_is_present_and_documented():
    assert os.path.isdir(_EXAMPLES), "examples/functions/ is missing"
    assert os.path.isfile(os.path.join(_EXAMPLES, "README.md"))
    assert os.path.isfile(os.path.join(_EXAMPLES, "custom_handler.py"))
    assert _sample_files(), "no *.request.json samples found"


def test_every_sample_is_valid_json_object():
    for name in _sample_files():
        with open(os.path.join(_EXAMPLES, name), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert isinstance(payload, dict), f"{name} is not a JSON object"


def test_every_sample_names_a_real_workload():
    available = set(pkg.available_workloads())
    for name in _sample_files():
        workload = name[: -len(".request.json")]
        assert workload in available, (
            f"{name} refers to workload {workload!r}, which no longer exists "
            f"(available: {', '.join(sorted(available))})")


def test_samples_actually_run_through_their_workload():
    """The strongest check available offline: feed each sample to its workload and
    require a well-formed Response. Catches a sample that drifted from the schema
    the handler expects."""
    import importlib

    from fnruntime.contract import Context, Request, Response

    for name in _sample_files():
        workload_name = name[: -len(".request.json")]
        module = importlib.import_module(f"fnworkloads.{workload_name}")
        with open(os.path.join(_EXAMPLES, name), "rb") as handle:
            body = handle.read()
        # echo_diag's sample points at hosts that do not resolve here; cap the wait
        # so the suite cannot hang on DNS.
        payload = json.loads(body)
        payload.pop("egress", None)
        payload["egress"] = False
        payload["timeout"] = 0.2
        # db_grant reads its target from the function's own configuration and
        # refuses to guess, so give it one. Dry run is the default, so this opens
        # no connection — it only proves the sample payload is the shape the
        # handler expects.
        if workload_name == "db_grant":
            os.environ.update({"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.invalid",
                               "FN_DB_NAME": "appdb"})
            os.environ.pop("FN_DB_DRY_RUN", None)
        if workload_name == "portainer_access":
            os.environ["FN_PORTAINER_URL"] = "https://portainer.invalid"
            os.environ.pop("FN_PORTAINER_API_KEY", None)
            os.environ.pop("FN_PORTAINER_DRY_RUN", None)   # dry run is the default
        # For an Entitle Remote Adapter THE VERB IS THE PATH, so a sample payload is
        # only meaningful paired with the route it belongs to. echo_diag has no
        # routing and accepts anything.
        method, path = _SAMPLE_ROUTE.get(workload_name, ("POST", "/"))
        request = Request(method=method, path=path, headers={}, query={},
                          body=json.dumps(payload).encode(), source="aws_function_url")
        response = module.handle(request, Context.from_env(workload=workload_name))
        assert isinstance(response, Response), f"{name}: handler returned {type(response)}"
        assert 200 <= response.status < 500, f"{name}: unexpected status {response.status}"
        response.rendered()          # must be serializable


def test_custom_handler_template_matches_the_workload_contract():
    """The template is what people copy — if it drifts from the contract, every
    workload written from it is broken."""
    path = os.path.join(_EXAMPLES, "custom_handler.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    for needed in ("NAME", "DESCRIPTION", "def handle(req: Request, ctx: Context) -> Response",
                   "from fnruntime.contract import"):
        assert needed in source, f"custom_handler.py is missing {needed!r}"
    # It must NOT live in fnworkloads/ — it is a template, not a deployable workload.
    assert "custom_handler" not in pkg.available_workloads()


def test_readme_documents_every_sample():
    with open(os.path.join(_EXAMPLES, "README.md"), "r", encoding="utf-8") as handle:
        readme = handle.read()
    for name in _sample_files():
        assert name in readme, f"{name} is not mentioned in examples/functions/README.md"


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
