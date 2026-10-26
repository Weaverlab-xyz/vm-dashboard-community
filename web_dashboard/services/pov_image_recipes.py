"""The standard images a POV is built from, as recipes over the Packer engine.

A POV template builder does not write Packer. They pick **Standard Linux** or **Standard
Windows**, a region and a few toggles, and :func:`build_request` turns that into the
same ``AWSPackerBuildRequest`` / ``AzurePackerBuildRequest`` the demo Build Image tab
sends — so the build itself is ``packer_build_service``, and a recipe is only data.

What a standard image carries, and what it deliberately does not:

* **Linux (Ubuntu 24.04)** — Docker and cloud-init, so the same image serves as a target
  AND as the control node (``docs/profiles/pov/public-cloud/control-node.md``); the
  ``adminuser`` account Password Safe manages and Entitle connects as, with SSH password
  login for that one account; the PRA Shell Jump sshd prerequisites. EPM-L can be staged
  (installed, never activated).
* **Windows (Server 2022)** — WinRM reachable from inside the POV network on 5985, which
  the Resource Broker install, the functional account and guest steps all ride; OpenSSH
  and RDP+NLA for PRA; EC2Launch re-armed so every instance generates its own
  Administrator password (on Azure: Sysprep, and the platform sets the per-POV
  ``povadmin`` password at creation).
* **Neither** carries anything per-tenant or per-POV: no enrolment code, gateway key,
  Resource Broker install key, Entitle token or password. Those stay post-boot, through
  the agent, exactly as on a stock image.

AWS and Azure. ``RECIPES`` is keyed by cloud, so GCP is one more entry and a request
builder.

**The Linux login differs by cloud, on purpose.** On AWS a Standard Linux target is given
the POV's own password for ``adminuser`` by cloud-init (pov_cloud_aws), and the image's
sshd Match block admits password login for that one account. On Azure every VM is
CREATED with ``povadmin`` and the per-POV platform password (pov_cloud_azure), and that
is the login ``stored_credentials`` hands Password Safe and Entitle; ``adminuser`` gets
no password there. So the Azure recipe points the Match block at ``povadmin`` instead —
with bt-ready's global ``PasswordAuthentication no`` read first, the platform login
would otherwise be refused over SSH on exactly the images built for it. ``adminuser``
is created on both, as the Password-Safe-managed bootstrap account.
"""
from typing import Optional, Union

from ..models.packer import AWSPackerBuildRequest, AzurePackerBuildRequest


class RecipeError(ValueError):
    pass


# The account bt-ready creates and Password Safe manages. It is also the login a
# standard-linux target is given at launch (pov_cloud_aws), so the two must agree.
LINUX_ADMIN_USER = "adminuser"

STANDARD_LINUX = "standard-linux"
STANDARD_WINDOWS = "standard-windows"

# The container images the control node runs (pov_broker), pre-pulled by the Linux
# recipe's toggle so a control node does not wait on Docker Hub at first boot. The
# bootstrap still pulls each one (they are :latest), so a baked layer only saves time.
_CONTROL_NODE_IMAGES_TOGGLE = "prepull_control_node"


def control_node_images() -> tuple:
    from . import pov_broker
    return (pov_broker.AGENT_IMAGE, pov_broker.GATEWAY_IMAGE, pov_broker.ANSIBLE_VM_IMAGE)

# What a recipe installs is the same on every cloud; only the base image and the size
# of the build machine differ. Order matters in both lists: bt-ready-debian.sh seals the
# image (host keys, machine-id, cloud-init state) and so runs LAST on Linux; the
# operator's own script, if any, is slotted in just before it by packer_build_service.
_LINUX_FILES = ["pov/pov-ready-linux.sh", "beyondtrust/bt-ready-debian.sh"]
_WINDOWS_FILES = ["beyondtrust/bt-ready-windows.ps1", "pov/pov-ready-windows.ps1"]
_LINUX_TOGGLES = {
    "stage_epml": {
        "label": "Install the EPM-L package (activated later, per POV)",
        "default": False,
    },
    _CONTROL_NODE_IMAGES_TOGGLE: {
        "label": "Pre-pull the control-node containers",
        "default": True,
    },
}
_WINDOWS_TOGGLES = {
    "stage_installer": {
        "label": ("Stage an installer from storage into C:\\BT\\staged "
                  "(not run — e.g. EPM for Windows)"),
        "default": False,
        "takes": "storage_asset",
    },
}
_LINUX_SUMMARY = ("Ubuntu 24.04 with Docker and cloud-init. Works as a target "
                  "(Password Safe, PRA Shell Jump, Entitle) and as the control node.")
_WINDOWS_SUMMARY = ("Windows Server 2022 with WinRM open to the POV network, OpenSSH "
                    "and RDP. Hosts the Resource Broker and is a PRA RDP target.")

# The account an Azure POV VM is CREATED with (pov_cloud_azure.ADMIN_USERNAME), holding
# the per-POV platform password. Not imported: that module pulls in the Azure SDK, and
# this one is pure. tests/test_pov_image_builder.py pins the two together.
AZURE_PLATFORM_ADMIN_USER = "povadmin"

RECIPES = {
    "aws": {
        STANDARD_LINUX: {
            "label": "Standard Linux",
            "summary": _LINUX_SUMMARY,
            "os_type": "Linux",
            "os_family": "linux",
            "source_ami_family": "ubuntu-24.04",
            "instance_type": "t3.medium",
            "files": _LINUX_FILES,
            "toggles": _LINUX_TOGGLES,
        },
        STANDARD_WINDOWS: {
            "label": "Standard Windows",
            "summary": _WINDOWS_SUMMARY,
            "os_type": "Windows",
            "os_family": "windows",
            "source_ami_family": "windows-server-2022",
            # Windows builds crawl on 4 GB; the build instance only lives for the build.
            "instance_type": "t3.large",
            "files": _WINDOWS_FILES,
            "toggles": _WINDOWS_TOGGLES,
        },
    },
    # Marketplace images, both Gen2. ``instance_type`` is the Azure VM size of the build
    # machine (the key is shared so describe() and the tab need no per-cloud case).
    "azure": {
        STANDARD_LINUX: {
            "label": "Standard Linux",
            "summary": _LINUX_SUMMARY,
            "os_type": "Linux",
            "os_family": "linux",
            "image": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts",
                      "sku": "server"},
            "instance_type": "Standard_B2s",
            "files": _LINUX_FILES,
            "toggles": _LINUX_TOGGLES,
        },
        STANDARD_WINDOWS: {
            "label": "Standard Windows",
            "summary": _WINDOWS_SUMMARY,
            "os_type": "Windows",
            "os_family": "windows",
            "image": {"publisher": "MicrosoftWindowsServer", "offer": "WindowsServer",
                      "sku": "2022-datacenter-g2"},
            # B-series (<= 4 GB) makes a Windows build crawl; the packer_build_service
            # warning says the same to the demo tab.
            "instance_type": "Standard_D2s_v3",
            "files": _WINDOWS_FILES,
            "toggles": _WINDOWS_TOGGLES,
        },
    },
}


def recipes_for(cloud: str) -> dict:
    return RECIPES.get((cloud or "").strip().lower(), {})


def describe(cloud: str) -> list:
    """The recipes for a cloud, in the shape the Images tab renders. No secrets, no I/O."""
    return [
        {"id": rid, "label": r["label"], "summary": r["summary"],
         "os_type": r["os_type"], "os_family": r["os_family"],
         "instance_type": r["instance_type"],
         "toggles": [{"id": tid, **t} for tid, t in r["toggles"].items()]}
        for rid, r in recipes_for(cloud).items()
    ]


def get(cloud: str, recipe_id: str) -> dict:
    recipe = recipes_for(cloud).get((recipe_id or "").strip())
    if recipe is None:
        known = ", ".join(sorted(recipes_for(cloud))) or "none"
        raise RecipeError(f"no recipe {recipe_id!r} for {cloud or 'this cloud'} "
                          f"(known: {known})")
    return recipe


def default_name(recipe_id: str) -> str:
    return f"pov-{recipe_id}"


def build_request(cloud: str, recipe_id: str, *, region: str, image_name: str = "",
                  toggles: Optional[dict] = None, extra_script: str = "",
                  stage_asset: str = ""
                  ) -> Union[AWSPackerBuildRequest, AzurePackerBuildRequest]:
    """The Packer request for one recipe. Pure: resolves no URL and calls no cloud.

    ``extra_script`` is an operator script's TEXT (read from storage by the caller), run
    before the recipe's sealing step. ``stage_asset`` is the storage asset the Windows
    ``stage_installer`` toggle downloads into the image without running it.
    """
    if cloud not in ("aws", "azure"):
        raise RecipeError(f"standard images are built on AWS and Azure only so far, "
                          f"not {cloud!r}")
    recipe = get(cloud, recipe_id)
    if not (region or "").strip():
        raise RecipeError("a region is required — an image exists only in the region it "
                          "was built in")
    chosen = {tid: bool(t.get("default")) for tid, t in recipe["toggles"].items()}
    for tid, val in (toggles or {}).items():
        if tid not in recipe["toggles"]:
            raise RecipeError(f"recipe {recipe_id} has no option {tid!r}")
        chosen[tid] = bool(val)

    env = []
    kwargs = {}
    if recipe["os_type"] == "Linux":
        kwargs["bt_admin_user"] = LINUX_ADMIN_USER
        # Entitle's k3s install and Config Management `become` need full sudo; the
        # scoped default covers only Entitle's ephemeral-account commands.
        env.append({"name": "BT_ADMIN_NOPASSWD_ALL", "value": "1"})
        # The account the sshd Match block admits for password login: the one this
        # cloud's POV sets a password on (see the module docstring).
        login = AZURE_PLATFORM_ADMIN_USER if cloud == "azure" else LINUX_ADMIN_USER
        env.append({"name": "POV_ADMIN_USER", "value": login})
        if chosen.get("stage_epml"):
            kwargs["bt_epml"] = "deb"
            kwargs["bt_epml_source"] = "storage"
        if chosen.get(_CONTROL_NODE_IMAGES_TOGGLE):
            env.append({"name": "POV_PREPULL_IMAGES", "value": " ".join(control_node_images())})
    else:
        if chosen.get("stage_installer"):
            if not (stage_asset or "").strip():
                raise RecipeError("pick the installer to stage from storage, or turn "
                                  "that option off")
            # The asset NAME only. Its presigned URL is minted when the build starts
            # (packer_build_service._bt_provisioner_env): a URL minted now would expire
            # while the job waits in the queue or for its change window.
            kwargs["bt_stage_asset"] = stage_asset.strip()

    common = dict(
        image_name=(image_name or "").strip() or default_name(recipe_id),
        os_type=recipe["os_type"],
        provisioner_files=list(recipe["files"]),
        provisioner_script=extra_script or "",
        provisioner_env_vars=env,
        region=region.strip(),
        recipe=recipe_id,
        **kwargs,
    )
    if cloud == "azure":
        image = recipe["image"]
        return AzurePackerBuildRequest(
            image_publisher=image["publisher"],
            image_offer=image["offer"],
            image_sku=image["sku"],
            vm_size=recipe["instance_type"],
            register_mode="image_only",
            **common,
        )
    return AWSPackerBuildRequest(
        source_ami_family=recipe["source_ami_family"],
        instance_type=recipe["instance_type"],
        register_mode="ami_only",
        **common,
    )
