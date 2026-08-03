"""The cloud deploy/destroy runners get their arguments from job metadata, not memory.

A `BackgroundTask` was handed live Python objects. The job runner is handed a row, so
anything an endpoint forgets to persist is simply absent when the work runs. That
failure is quiet in the worst way: `api/aws.py`'s deploy endpoint passed
`jump_group` / `jumpoint_name` / `pra_credential_ref` straight to the task and persisted
none of them, so a metadata-driven rebuild would have launched the instance and skipped
the BeyondTrust PRA registration — a job that reports success with a side effect
missing.

These tests derive both halves from the AST and compare them, so a new argument added to
a runner without a matching metadata key fails here rather than in production:

  * every key a service's `run()` requires is written by the endpoints that create that
    job type;
  * no resolved credential is persisted — references only, the rule
    `tests/test_ansible_local_meta.py` already enforces for Ansible runs.

Run: python tests/test_cloud_deploy_meta.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (service module, api module) pairs whose run() is metadata-driven.
_PAIRS = [
    ("aws_vm_service.py", "aws.py"),
    ("oci_vm_service.py", "oci.py"),
    ("azure_vm_service.py", "azure.py"),
    ("gcp_vm_service.py", "gcp.py"),
]


def _tree(*parts):
    path = os.path.join(_ROOT, *parts)
    return ast.parse(open(path, encoding="utf-8").read(), path)


def _required_keys_by_job_type(svc_tree):
    """{job_type: {keys that branch reads as meta["k"]}}.

    Scoped per branch, not per module: `run()` dispatches on job_type, so a key the
    ami_copy branch requires says nothing about what an ec2_destroy job must persist.
    Comparing the union would flag every job type for every other one's keys.

    `meta["k"]` is required — a missing key is a KeyError at run time. `meta.get("k")`
    carries its own default and needs no guarantee."""
    run = next(n for n in svc_tree.body if getattr(n, "name", None) == "run")
    out = {}
    for branch in [n for n in ast.walk(run) if isinstance(n, ast.If)]:
        test = branch.test
        types = set()
        if isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.Eq):
            if isinstance(test.comparators[0], ast.Constant):
                types.add(test.comparators[0].value)
        elif isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.In):
            for e in getattr(test.comparators[0], "elts", []):
                if isinstance(e, ast.Constant):
                    types.add(e.value)
        if not types:
            continue
        keys = set()
        for stmt in branch.body:
            for n in ast.walk(stmt):
                if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                        and n.value.id == "meta" and isinstance(n.slice, ast.Constant)):
                    keys.add(n.slice.value)
        for t in types:
            out.setdefault(t, set()).update(keys)
    return out


def _create_job_sites(api_tree):
    """[(job_type, status, {metadata keys})] for every create_job call in a module."""
    sites = []
    for n in ast.walk(api_tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_job":
            def kw(name, default=None):
                for k in n.keywords:
                    if k.arg == name and isinstance(k.value, ast.Constant):
                        return k.value.value
                return default
            md = next((k.value for k in n.keywords if k.arg == "metadata"), None)
            keys = ({k.value for k in md.keys if isinstance(k, ast.Constant)}
                    if isinstance(md, ast.Dict) else set())
            sites.append((kw("job_type"), kw("status", "pending"), keys))
    return sites


def test_every_required_metadata_key_is_persisted():
    """Only `pending` rows are checked: those are the ones the runner claims and
    rebuilds. `queued` children are driven by a parent that already holds their
    arguments, so they are not required to stand alone.

    Scope worth being explicit about: this covers `meta["k"]` only, and it would *not*
    have caught the PRA gap below. `run()` reads those three with `.get(...)` on
    purpose — job rows created before this change don't carry them, and a KeyError on
    an old row is worse than a deploy without a jump group. Optionality is a
    deliberate compatibility choice, so the guarantee they need is the named test
    below, not this one."""
    missing = []
    checked = 0
    for svc, api in _PAIRS:
        required = _required_keys_by_job_type(_tree("web_dashboard", "services", svc))
        for job_type, status, keys in _create_job_sites(_tree("web_dashboard", "api", api)):
            if status != "pending" or job_type not in required:
                continue
            checked += 1
            gap = required[job_type] - keys
            if gap:
                missing.append(f"{api}:{job_type} never persists {sorted(gap)}")
    assert checked, "no pending job sites matched a run() branch — the test is vacuous"
    assert not missing, "runner arguments that no endpoint writes: " + "; ".join(missing)


def test_the_pra_jump_group_fields_are_persisted():
    """The concrete gap that prompted this file. `_run_deploy` takes all three; the
    endpoint used to pass them from `req` and persist none, so rebuilding from metadata
    would deploy the instance and silently skip the PRA jump-group registration."""
    sites = [ks for jt, _, ks in _create_job_sites(_tree("web_dashboard", "api", "aws.py"))
             if jt == "ec2_deploy"]
    assert sites, "no ec2_deploy create_job call found"
    # The single-deploy site is the one the runner claims (the bulk children are queued).
    assert any({"jump_group", "jumpoint_name", "pra_credential_ref"} <= ks for ks in sites), (
        "no ec2_deploy site persists the PRA jump-group fields — a runner-driven deploy "
        "would skip BeyondTrust registration and still report success")


def test_no_resolved_credential_is_persisted():
    """References are fine and are the whole point (`*_secret_override`,
    `*_credential_ref` name something to resolve at run time). A resolved value is not:
    job metadata is readable by anyone who can read the job."""
    banned = ("password", "private_key", "client_secret", "api_key", "token")
    allowed_suffixes = ("_ref", "_override", "_name", "_title", "_var")
    # `register_in_passwordsafe` is a boolean flag, not a credential — the substring
    # match can't tell, so the prefix is named rather than the pattern weakened.
    allowed_prefixes = ("register_in_",)
    offenders = []
    for _, api in _PAIRS:
        for job_type, _, keys in _create_job_sites(_tree("web_dashboard", "api", api)):
            for k in keys:
                if (any(b in k for b in banned)
                        and not k.endswith(allowed_suffixes)
                        and not k.startswith(allowed_prefixes)):
                    offenders.append(f"{api}:{job_type} persists {k!r}")
    assert not offenders, "credential-shaped metadata keys: " + "; ".join(offenders)


def test_the_reference_exemption_is_not_a_loophole():
    """The suffix allowlist above must not let a bare secret through."""
    banned = ("password", "private_key", "client_secret", "api_key", "token")
    allowed_suffixes = ("_ref", "_override", "_name", "_title", "_var")
    # `register_in_passwordsafe` is a boolean flag, not a credential — the substring
    # match can't tell, so the prefix is named rather than the pattern weakened.
    allowed_prefixes = ("register_in_",)

    def rejected(key):
        return (any(b in key for b in banned)
                and not key.endswith(allowed_suffixes)
                and not key.startswith(allowed_prefixes))

    assert rejected("password"), "a bare password key must be rejected"
    assert rejected("client_secret"), "a bare client_secret must be rejected"
    assert rejected("private_key"), "a bare private_key must be rejected"
    assert rejected("guest_password"), "a prefixed bare secret must still be rejected"
    assert not rejected("pra_credential_ref"), "a reference must be allowed"
    assert not rejected("ssh_key_secret_override"), "an override name must be allowed"
    assert not rejected("register_in_passwordsafe"), "a boolean flag must be allowed"


def _handled_types():
    src = open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"),
               encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "HANDLED_TYPES" for t in node.targets
        ):
            return {e.value for e in node.value.elts}
    raise AssertionError("HANDLED_TYPES not found")


_HANDLED = _handled_types()


def test_both_services_are_actually_reachable():
    """Guards the tests above from passing vacuously if a rename orphans a service."""
    for svc, _ in _PAIRS:
        assert os.path.exists(os.path.join(_ROOT, "web_dashboard", "services", svc))
    for t in ("ec2_deploy", "ec2_bulk_deploy", "ec2_destroy", "ec2_create_image",
              "ami_copy", "oci_deploy", "oci_destroy"):
        assert t in _HANDLED, f"{t} is not claimable by the runner"


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
    sys.exit(1 if failures else 0)
