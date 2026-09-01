"""GCP as a POV lab platform: the adapter ``lab_platforms`` dispatches to.

Third of three, and thin in the same shape as its siblings. The work is in
``pov_cloud_env`` (shared) and ``pov_cloud_gcp`` (Compute Engine).

The one capability that differs from both other clouds is ``projects``:

    projects            - True, alone among the clouds. A GCP project is a real boundary
                          — an environment is built in one, `PovEnvironment.project_id`
                          records WHICH, and the teardown reads that back rather than
                          re-deriving it from current config. `expiry_reaper` states the
                          rule this protects: a destroy aimed at the wrong project is the
                          worst version of this bug.
    stored_credentials  - False, like AWS. GCE holds no guest login to read back; the
                          platform login comes from the image and its Vault account.
    share link          - False, like both. No publish sets on any cloud.
    idle suspend        - False. `scheduled_suspend` is the substitute.
    template authoring  - False. Templates are edited in the dashboard.
"""
from __future__ import annotations

from . import pov_cloud_env, pov_cloud_gcp

CLOUD = "gcp"

VALID_RUNSTATES = pov_cloud_env.VALID_RUNSTATES


def configured() -> bool:
    return pov_cloud_gcp.configured()


def configured_project_id() -> str:
    """The project a new POV is built in. Required by the ``projects`` capability."""
    return pov_cloud_gcp.configured_project_id()


async def verify() -> tuple[bool, str]:
    return await pov_cloud_gcp.verify()


async def list_templates() -> list:
    return await pov_cloud_env.list_templates(CLOUD)


async def get_template(template_id: str) -> dict:
    return await pov_cloud_env.get_template(CLOUD, template_id)


async def list_environments() -> list:
    return await pov_cloud_env.list_environments(CLOUD)


async def get_environment(env_id: str) -> dict:
    return await pov_cloud_env.get_environment(CLOUD, env_id)


def environment_id_for(name: str) -> str:
    """What this POV's environment id — and its network's name prefix — will be.

    Recorded before the first API call, so a build that dies partway still leaves a
    findable, reapable set. See `pov_env_service.run_env_provision`.
    """
    return pov_cloud_env.env_id_for(name)


async def create_environment(template_id: str, name: str = "", *,
                             bootstrap: str = "", **kwargs) -> dict:
    """Build the environment.

    ``**kwargs`` swallows ``project_id``, which the provision job passes to every adapter.
    GCP is the one cloud that has a use for it — but it reads the value off the POV ROW
    rather than the argument, because the row is what a teardown weeks later can still
    consult.
    """
    return await pov_cloud_env.create_environment(
        CLOUD, template_id, name, bootstrap=bootstrap)


async def create_broker_vm(env_id: str, template_id: str, bootstrap: str) -> dict:
    """Build or rebuild the broker VM, with its bootstrap already in `user-data`.

    Not `inject_bootstrap`: cloud-init runs it on FIRST boot, so a payload handed to a
    running instance does nothing at all.
    """
    return await pov_cloud_env.create_broker_vm(CLOUD, env_id, template_id, bootstrap)


async def set_runstate(env_id: str, runstate: str) -> dict:
    return await pov_cloud_env.set_runstate(CLOUD, env_id, runstate)


async def wait_for_runstate(env_id: str, target: str, **kwargs) -> dict:
    return await pov_cloud_env.wait_for_runstate(CLOUD, env_id, target, **kwargs)


async def delete_environment(env_id: str) -> None:
    await pov_cloud_env.delete_environment(CLOUD, env_id)
