"""Azure as a POV lab platform: the adapter ``lab_platforms`` dispatches to.

Thin, and thin in the same shape as ``pov_aws_service`` — which was the point of drawing
the line where it is. The work is in ``pov_cloud_env`` (shared) and ``pov_cloud_azure``
(ARM); this file names the cloud.

Where Azure differs from AWS, it differs in the capability table where the UI can read it,
not in private behaviour here:

    stored_credentials  - True, unlike AWS. Azure requires an admin account at VM
                          creation, so a POV built here HAS a platform login and the
                          Resource Broker install can use it.
    share link          - False, same as AWS. No publish sets on any cloud; a cloud POV's
                          customer-facing access is PRA.
    idle suspend        - False. `scheduled_suspend` is how a cloud POV stops costing
                          money overnight.
    projects            - False. The environment's scope is its own resource group.
    template authoring  - False. Templates are edited in the dashboard, not baked here.
"""
from __future__ import annotations

from . import pov_cloud_azure, pov_cloud_env

CLOUD = "azure"

VALID_RUNSTATES = pov_cloud_env.VALID_RUNSTATES


def configured() -> bool:
    return pov_cloud_azure.configured()


async def verify() -> tuple[bool, str]:
    return await pov_cloud_azure.verify()


async def list_templates() -> list:
    return await pov_cloud_env.list_templates(CLOUD)


async def get_template(template_id: str) -> dict:
    return await pov_cloud_env.get_template(CLOUD, template_id)


async def list_environments() -> list:
    return await pov_cloud_env.list_environments(CLOUD)


async def get_environment(env_id: str) -> dict:
    return await pov_cloud_env.get_environment(CLOUD, env_id)


def environment_id_for(name: str) -> str:
    """What this POV's environment id — and its resource group name — will be.

    Recorded before the first ARM call, so a build that dies partway still leaves a
    findable, reapable group. See `pov_env_service.run_env_provision`.
    """
    return pov_cloud_env.env_id_for(name)


async def create_environment(template_id: str, name: str = "", *,
                             bootstrap: str = "", **kwargs) -> dict:
    """Build the environment.

    ``**kwargs`` swallows ``project_id``, which the provision job passes to every adapter
    and which Azure has nothing to do with — its equivalent scoping is the resource group,
    which this creates rather than selects.
    """
    return await pov_cloud_env.create_environment(
        CLOUD, template_id, name, bootstrap=bootstrap)


async def create_broker_vm(env_id: str, template_id: str, bootstrap: str) -> dict:
    """Build or rebuild the broker VM, with its bootstrap already in custom_data.

    Not `inject_bootstrap`: cloud-init runs it on FIRST boot, so a payload handed to a
    running VM does nothing at all. `bootstrap_injection` is "cloud_init" so `pov_broker`
    takes this path instead.
    """
    return await pov_cloud_env.create_broker_vm(CLOUD, env_id, template_id, bootstrap)


async def set_runstate(env_id: str, runstate: str) -> dict:
    return await pov_cloud_env.set_runstate(CLOUD, env_id, runstate)


async def wait_for_runstate(env_id: str, target: str, **kwargs) -> dict:
    return await pov_cloud_env.wait_for_runstate(CLOUD, env_id, target, **kwargs)


async def stored_credentials(env_id: str, vm_id: str = "") -> list:
    return await pov_cloud_azure.stored_credentials(env_id, vm_id)


async def delete_environment(env_id: str) -> None:
    await pov_cloud_env.delete_environment(CLOUD, env_id)
