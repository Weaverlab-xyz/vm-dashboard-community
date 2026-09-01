"""AWS as a POV lab platform: the adapter ``lab_platforms`` dispatches to.

Thin on purpose. The registry's contract is a module with a fixed set of functions, and
almost all of what those functions do is the same on every cloud — so the work lives in
``pov_cloud_env`` (shared) and ``pov_cloud_aws`` (boto3), and this file is the binding
that names the cloud. Adding Azure should be this file again with one word changed, plus
a driver; if it ever needs more than that, the difference belongs in the capability table
where the UI can see it, not in a second adapter's private behaviour.

What this platform cannot do, and where each one is declared:

    share link          - CAPABILITIES["aws"]["share_link"] = False. No publish sets, and
                          no analogue. A cloud POV's customer-facing access is PRA.
    stored credentials  - False. AWS holds no guest login to read back.
    idle suspend        - False. No cloud has one; `scheduled_suspend` is how a cloud POV
                          stops costing money overnight.
    projects            - False. The environment's scope is its own VPC and its tag.
    template authoring  - False. Templates are edited in the dashboard, not baked here.

Each of those is a capability the UI reads *before* offering a control, which is the whole
reason the table exists — a POV page that renders a Share button on AWS would be a 500
from inside a provision job instead of a sentence on a form.
"""
from __future__ import annotations

from . import pov_cloud_aws, pov_cloud_env

CLOUD = "aws"

# Re-exported so callers that already have the adapter module do not need to know a
# driver exists. `pov_env_service` reads this to refuse a runstate the platform cannot do.
VALID_RUNSTATES = pov_cloud_env.VALID_RUNSTATES


def configured() -> bool:
    return pov_cloud_aws.configured()


async def verify() -> tuple[bool, str]:
    return await pov_cloud_aws.verify()


async def list_templates() -> list:
    return await pov_cloud_env.list_templates(CLOUD)


async def get_template(template_id: str) -> dict:
    return await pov_cloud_env.get_template(CLOUD, template_id)


async def list_environments() -> list:
    return await pov_cloud_env.list_environments(CLOUD)


async def get_environment(env_id: str) -> dict:
    return await pov_cloud_env.get_environment(CLOUD, env_id)


async def create_environment(template_id: str, name: str = "", *,
                             bootstrap: str = "", **kwargs) -> dict:
    """Build the environment.

    ``**kwargs`` swallows ``project_id``, which the provision job passes to every adapter
    and which AWS has nothing to do with. Swallowed rather than rejected: the job is
    platform-agnostic by design, and making it special-case one field per platform is the
    if/elif chain the registry exists to avoid.
    """
    return await pov_cloud_env.create_environment(
        CLOUD, template_id, name, bootstrap=bootstrap)


def environment_id_for(name: str) -> str:
    """What this POV's environment id WILL be, before anything is created.

    Optional in the contract, and Skytap does not have it: an id the platform mints
    cannot be predicted. A cloud's can, because the "environment" is a tag this dashboard
    chooses, and `run_env_provision` records it BEFORE the first API call so a create that
    dies halfway still leaves a findable, reapable set of resources.
    """
    return pov_cloud_env.env_id_for(name)


async def set_runstate(env_id: str, runstate: str) -> dict:
    return await pov_cloud_env.set_runstate(CLOUD, env_id, runstate)


async def wait_for_runstate(env_id: str, target: str, **kwargs) -> dict:
    return await pov_cloud_env.wait_for_runstate(CLOUD, env_id, target, **kwargs)


async def delete_environment(env_id: str) -> None:
    await pov_cloud_env.delete_environment(CLOUD, env_id)
