"""Config Management to a Windows cloud server over OpenSSH, on every runner.

What these pin:

- ``windows_target`` recognises a Windows build on each cloud from what its deploy job
  records (GCP ``os_type``, Azure ``req.os_type``, ``admin_password_custody`` on all
  three) and answers with the administrator the deploy used; a Linux build is ``{}``;
- the one definition of the connection: SSH with ``ansible_shell_type: powershell``,
  passed as extra vars so it outranks a play's own ``ansible_connection: winrm``;
- ``_dispatch_cloud_runner`` hands ``windows`` to the ECS, ACI and Cloud Run runners,
  each of which puts the flags on its ansible-playbook line BEFORE the secret vars file;
- a Linux run's command line is unchanged.

Run: python tests/test_windows_ansible_runners.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="win-ansible-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-windows-ansible-runners")

from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import ansible_local_run_service as alrs  # noqa: E402
from web_dashboard.services import ansible_vm_cmd  # noqa: E402

Base.metadata.create_all(bind=engine)   # the dispatcher reads runner config


def _src(name):
    with open(os.path.join(_ROOT, "web_dashboard", "services", name), encoding="utf-8") as f:
        return f.read()


def test_windows_target_per_cloud():
    gcp = {"os_type": "windows", "admin_username": "gcpadmin", "private_ip": "10.0.0.5"}
    azure = {"req": {"os_type": "Windows"}, "admin_username": "azureuser",
             "admin_password_custody": "secret_manager"}
    aws = {"admin_username": "Administrator", "ssh_user": "Administrator",
           "admin_password_custody": "passwordsafe_managed"}
    assert alrs.windows_target(gcp) == {"user": "gcpadmin"}
    assert alrs.windows_target(azure) == {"user": "azureuser"}
    assert alrs.windows_target(aws) == {"user": "Administrator"}
    for linux in ({}, None, {"os_type": "linux", "ssh_user": "ec2-user"},
                  {"req": {"os_type": "Linux"}}):
        assert alrs.windows_target(linux) == {}, linux


def test_the_connection_is_ssh_with_a_powershell_shell():
    assert ansible_vm_cmd.WINDOWS_SSH_VARS == {"ansible_connection": "ssh",
                                               "ansible_shell_type": "powershell"}
    assert ansible_vm_cmd.windows_ssh_args() == (
        "-e ansible_connection=ssh -e ansible_shell_type=powershell ")


def test_dispatch_passes_windows_to_every_runner():
    from web_dashboard.services import aws_service, azure_service, gcp_service
    seen = {}

    def fake(name):
        async def run(**kw):
            seen[name] = kw.get("windows")
            return 0, ""
        return run

    saved = (aws_service.run_ecs_ansible_task, azure_service.run_aci_ansible_task,
             gcp_service.run_cloud_run_ansible_task)
    aws_service.run_ecs_ansible_task = fake("ecs")
    azure_service.run_aci_ansible_task = fake("aci")
    gcp_service.run_cloud_run_ansible_task = fake("gcp")
    try:
        for runner in ("ecs", "aci", "gcp"):
            for flag in (True, False):
                asyncio.run(alrs._dispatch_cloud_runner(
                    runner=runner, target_ip="10.0.0.5", ansible_user="u",
                    playbook_b64="", ssh_key_b64="", job_id="j" * 8, windows=flag))
                assert seen[runner] is flag, (runner, flag)
    finally:
        (aws_service.run_ecs_ansible_task, azure_service.run_aci_ansible_task,
         gcp_service.run_cloud_run_ansible_task) = saved


def test_each_runner_puts_the_flags_before_the_secret_vars_file():
    for name, needle in (("aws_service.py", "+ _windows_args(windows) + _secret_ev +"),
                         ("azure_service.py",
                          '+ (_windows_ssh_args() if windows else "") + _secret_ev +'),
                         ("gcp_service.py", "+ _windows_ssh_args(windows) + _secret_ev +")):
        src = _src(name)
        assert needle in src, name
        assert "windows: bool = False" in src, name


def test_the_run_service_uses_the_recorded_administrator():
    src = _src("ansible_local_run_service.py")
    assert "or win.get(\"user\")" in src
    assert "windows=bool(win)," in src
    # the local runner path rides inline extra vars, operator values winning
    assert "{**ansible_vm_cmd.WINDOWS_SSH_VARS, **(extra_vars or {})}" in src


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok   {_name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {_name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
