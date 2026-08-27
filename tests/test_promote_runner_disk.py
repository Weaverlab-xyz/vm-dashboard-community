"""The promote runner must be given a disk big enough for the image it edits.

The bug this pins: an `image_promote_aws` job downloaded a 17.5 GiB VHD into the
Fargate task's /tmp and then ran `virt-customize` to inject an EC2 cloud-init.
The task definition asked for no `ephemeralStorage`, so Fargate handed it the
implicit 20 GiB default — shared with the runner image's own layers. libguestfs
had nowhere to build its supermin appliance and failed with

    libguestfs error: /usr/bin/supermin exited with error status 1

which reads like a corrupt disk image, not a full filesystem. The AWS leg was the
only one that had never run libguestfs before (Azure/GCP promotes inject their
guest agents on ACI / Cloud Run), so the 20 GiB ceiling went unnoticed.

Two halves, so neither can regress alone:
  * the ECS task definition must size the volume from the operator's knob, and
  * every virt-customize call in the runner must go through the wrapper that
    preflights free space, so a new injector can't reintroduce the opaque
    failure.

The ECS half is an AST check: nothing here can execute a task-definition
registration (that needs boto3 credentials and a live cluster). The validation
half runs for real.

Runs under pytest, or standalone:  python tests/test_promote_runner_disk.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_AWS_SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "aws_service.py")
_PROMOTE_SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "promote_runner_service.py")
_ENTRYPOINT = os.path.join(_ROOT, "runners", "promote", "entrypoint.py")


def _tree(path):
    return ast.parse(open(path, encoding="utf-8").read(), path)


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_ecs_task_definition_sizes_ephemeral_storage():
    """No ephemeralStorage key → Fargate's implicit 20 GiB, which cannot hold a
    real disk image plus the libguestfs appliance."""
    fn = _function(_tree(_AWS_SERVICE), "_run_promote_runner_ecs_sync")
    src = ast.unparse(fn)
    assert "ephemeralStorage" in src, \
        "the promote-runner task definition no longer requests ephemeralStorage — " \
        "Fargate will silently cap the task at 20 GiB"
    assert "sizeInGiB" in src, "ephemeralStorage must carry sizeInGiB"
    # Fed from the caller's knob, not hardcoded — an operator promoting a bigger
    # image than the default covers has to be able to raise it.
    assert "ephemeral_storage_gib" in {a.arg for a in fn.args.args}, \
        "_run_promote_runner_ecs_sync must take ephemeral_storage_gib from the caller"
    assert "int(ephemeral_storage_gib)" in src, \
        "sizeInGiB must come from the ephemeral_storage_gib argument"


def test_aws_target_passes_the_disk_size_through():
    """A knob the resolver reads but the launch call drops is dead config."""
    tree = _tree(_PROMOTE_SERVICE)
    resolver = ast.unparse(_function(tree, "_resolve_aws_runner_config"))
    assert "promote_runner_ecs_ephemeral_storage_gib" in resolver
    assert "'ephemeral_storage_gib'" in resolver or '"ephemeral_storage_gib"' in resolver

    launcher = ast.unparse(_function(tree, "run_for_aws_target"))
    assert "ephemeral_storage_gib=cfg['ephemeral_storage_gib']" in launcher, \
        "run_for_aws_target must hand the resolved disk size to run_promote_runner_ecs"


def test_every_virt_customize_call_preflights_disk_space():
    """virt-customize is the step that dies when the volume is full, and its error
    names supermin rather than the disk. Any bare subprocess call that runs it
    skips the free-space preflight and the annotated failure, so the next reader
    is back to guessing."""
    tree = _tree(_ENTRYPOINT)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src = ast.unparse(node)
        if "'virt-customize'" not in src and '"virt-customize"' not in src:
            continue
        if node.name == "run_virt_customize":
            continue
        if "run_virt_customize(" not in src:
            offenders.append(node.name)
    assert not offenders, \
        f"these build a virt-customize argv but don't run it through run_virt_customize: {offenders}"


def test_runner_refuses_to_start_an_edit_it_cannot_finish():
    """The preflight itself: too little free space must raise with an actionable
    message, before libguestfs gets a chance to blame supermin."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("promote_entrypoint", _ENTRYPOINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._LIBGUESTFS_MIN_FREE_BYTES >= 1024 ** 3, \
        "less than a GiB of headroom won't fit a supermin appliance"

    calls = []
    mod.free_bytes = lambda path: 100 * 1024 * 1024        # 100 MiB — nowhere near enough
    mod.subprocess.check_call = lambda *a, **k: calls.append(a)
    try:
        mod.run_virt_customize(["virt-customize", "-a", "/tmp/source.vhd"], "test injection")
    except RuntimeError as e:
        msg = str(e)
        assert "free" in msg and "disk" in msg.lower(), f"message isn't about disk space: {msg}"
    else:
        raise AssertionError("run_virt_customize ran virt-customize with 100 MiB free")
    assert not calls, "virt-customize was launched despite the failed preflight"


def _run_resolver_with(overrides):
    """Resolve the AWS runner config against a stubbed config store. Returns the
    dict, or raises PromoteRunnerError."""
    from web_dashboard.services import promote_runner_service as prs
    conf = {
        "promote_runner_ecs_subnet_id": "subnet-123",
        "promote_runner_ecs_execution_role_arn": "arn:aws:iam::1:role/exec",
        "promote_runner_ecs_task_role_arn": "arn:aws:iam::1:role/task",
    }
    conf.update(overrides)
    original = prs.config_service.get
    prs.config_service.get = lambda key: conf.get(key, "")
    try:
        return prs._resolve_aws_runner_config()
    finally:
        prs.config_service.get = original


def test_disk_size_default_and_range():
    """Fargate rejects anything outside 21-200 GiB with an API error the operator
    never sees in context, so catch it here with a message that names the knob."""
    try:
        from web_dashboard.services import promote_runner_service as prs
    except Exception as exc:  # pragma: no cover — cloud SDKs missing
        try:
            import pytest
            pytest.skip(f"promote_runner_service import unavailable: {exc}")
        except ModuleNotFoundError:
            print(f"SKIP: {exc}")
            return

    cfg = _run_resolver_with({})
    assert cfg["ephemeral_storage_gib"] >= 21, \
        f"the default resolved to {cfg['ephemeral_storage_gib']} GiB, which Fargate rejects"

    assert _run_resolver_with(
        {"promote_runner_ecs_ephemeral_storage_gib": "200"})["ephemeral_storage_gib"] == 200

    for bad in ("20", "201", "0", "abc"):
        try:
            _run_resolver_with({"promote_runner_ecs_ephemeral_storage_gib": bad})
        except prs.PromoteRunnerError as e:
            assert "promote_runner_ecs_ephemeral_storage_gib" in str(e)
        else:
            raise AssertionError(f"{bad!r} was accepted as an ephemeral storage size")


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
