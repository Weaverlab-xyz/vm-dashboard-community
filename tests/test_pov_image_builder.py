"""The POV image builder: two standard images, built and picked on a POV instance.

What is worth pinning, because each costs a whole build (or a whole POV) to notice:

  * the recipes turn into the SAME request the demo Build Image tab sends, registered as
    the AMI itself — an export-and-register build registers nothing on an install with no
    S3 bucket, so a finished build would be unpickable;
  * the AWS Windows template connects over WinRM, reads its password from EC2 rather than
    setting one, and seals with EC2Launch's own sysprep — a bare Sysprep ships an image
    whose Administrator password nobody can ever recover;
  * the scripts a recipe names are shipped in the image (Dockerfile COPY) and exist;
  * the builder is a POV route on the POV gate and the EXISTING pov_templates scope, and
    /api/packer and /api/images stay behind cloud_pages;
  * the template editor's picker writes image_ref OR image_id, never a catalog id into
    the literal field;
  * a Standard Linux target, and only one reached through the catalog, gets the POV's
    own login;
  * on Azure the same two recipes build a MANAGED image in the POV's location and
    register it as itself (no gallery, no VHD export, no storage account), the Windows
    finisher writes the sealed marker before Sysprep, and the Linux image admits password
    login for the account an Azure POV actually creates (povadmin), not adminuser.

packer_service is stdlib-only and models/packer.py pydantic-only, so both are exercised
for real; pov_image_recipes is too, with pov_broker stubbed. The rest is source text.

Run: python tests/test_pov_image_builder.py
"""
import importlib.util
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load(*parts):
    """Load one module by path with its package registered as an empty namespace, so a
    relative sibling import resolves and web_dashboard/__init__.py never runs (the same
    harness as test_packer_gcp_source)."""
    pkg_parts = parts[:-1]
    for i in range(1, len(pkg_parts) + 1):
        name = ".".join(pkg_parts[:i])
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__path__ = [os.path.join(_ROOT, *pkg_parts[:i])]
            sys.modules[name] = stub
    full_name = ".".join(pkg_parts + (parts[-1][:-3],))
    spec = importlib.util.spec_from_file_location(full_name, os.path.join(_ROOT, *parts))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load("web_dashboard", "services", "packer_service.py")
pm = _load("web_dashboard", "models", "packer.py")
# The recipe module reads three image names from pov_broker, which imports half the app.
_broker = types.ModuleType("web_dashboard.services.pov_broker")
_broker.AGENT_IMAGE = "agent:latest"
_broker.GATEWAY_IMAGE = "gateway:latest"
_broker.ANSIBLE_VM_IMAGE = "ansible:latest"
sys.modules["web_dashboard.services.pov_broker"] = _broker
rec = _load("web_dashboard", "services", "pov_image_recipes.py")


# ── recipes → requests ───────────────────────────────────────────────────────

def test_both_standard_recipes_exist_on_aws_and_azure():
    for cloud in ("aws", "azure"):
        ids = {r["id"] for r in rec.describe(cloud)}
        assert ids == {"standard-linux", "standard-windows"}, cloud
    assert rec.describe("gcp") == [], "GCP has no recipe yet; the tab says so"


def test_linux_recipe_request():
    req = rec.build_request("aws", "standard-linux", region="us-east-2")
    assert isinstance(req, pm.AWSPackerBuildRequest)
    assert req.source_ami_family == "ubuntu-24.04"
    assert req.os_type == "Linux"
    assert req.register_mode == "ami_only", "a POV build must register the AMI, not export"
    assert req.region == "us-east-2"
    assert req.recipe == "standard-linux"
    assert req.bt_admin_user == rec.LINUX_ADMIN_USER == "adminuser"
    # bt-ready seals the image, so it must run LAST.
    assert req.provisioner_files[-1] == "beyondtrust/bt-ready-debian.sh"
    env = {e.name: e.value for e in req.provisioner_env_vars}
    assert env["BT_ADMIN_NOPASSWD_ALL"] == "1"
    assert env["POV_PREPULL_IMAGES"] == "agent:latest gateway:latest ansible:latest"
    assert req.bt_epml is None


def test_linux_epml_toggle_uses_the_storage_copy():
    req = rec.build_request("aws", "standard-linux", region="us-east-2",
                            toggles={"stage_epml": True, "prepull_control_node": False})
    assert (req.bt_epml, req.bt_epml_source) == ("deb", "storage")
    assert "POV_PREPULL_IMAGES" not in {e.name for e in req.provisioner_env_vars}


def test_windows_recipe_request():
    req = rec.build_request("aws", "standard-windows", region="eu-west-1",
                            image_name="acme-win")
    assert req.source_ami_family == "windows-server-2022"
    assert req.os_type == "Windows"
    assert req.image_name == "acme-win"
    assert req.provisioner_files == ["beyondtrust/bt-ready-windows.ps1",
                                     "pov/pov-ready-windows.ps1"]


def test_windows_stage_toggle_needs_an_asset_and_keeps_only_its_name():
    try:
        rec.build_request("aws", "standard-windows", region="us-east-2",
                          toggles={"stage_installer": True})
    except rec.RecipeError:
        pass
    else:
        raise AssertionError("staging with no installer chosen must be refused")
    req = rec.build_request("aws", "standard-windows", region="us-east-2",
                            toggles={"stage_installer": True}, stage_asset="epm-w.msi")
    # The NAME: a presigned URL minted at queue time would expire in the queue.
    assert req.bt_stage_asset == "epm-w.msi"
    assert "POV_STAGE_URL" not in {e.name for e in req.provisioner_env_vars}


def test_recipe_refusals():
    for kwargs, why in (
        (dict(cloud="aws", recipe_id="standard-linux", region=""), "no region"),
        (dict(cloud="aws", recipe_id="nope", region="us-east-2"), "unknown recipe"),
        (dict(cloud="gcp", recipe_id="standard-linux", region="us-east1"), "not AWS/Azure"),
        (dict(cloud="azure", recipe_id="standard-linux", region=""), "no Azure location"),
    ):
        try:
            rec.build_request(kwargs.pop("cloud"), kwargs.pop("recipe_id"), **kwargs)
        except rec.RecipeError:
            continue
        raise AssertionError(f"expected a refusal for {why}")
    try:
        rec.build_request("aws", "standard-linux", region="us-east-2",
                          toggles={"made_up": True})
    except rec.RecipeError:
        pass
    else:
        raise AssertionError("an unknown option must be refused, not ignored")


def test_model_pins_bundled_paths_and_region():
    base = dict(image_name="x", source_ami_family="ubuntu-24.04")
    for bad in ("../etc/passwd.sh", "pov/../../x.sh", "/abs/x.sh", "pov/x.exe", "x.sh"):
        try:
            pm.AWSPackerBuildRequest(**base, provisioner_files=[bad])
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be refused")
    for bad in ("us-east", "US-EAST-1", "us-east-1; rm"):
        try:
            pm.AWSPackerBuildRequest(**base, region=bad)
        except ValueError:
            continue
        raise AssertionError(f"region {bad!r} must be refused")
    ok = pm.AWSPackerBuildRequest(**base, region="us-gov-west-1")
    assert ok.region == "us-gov-west-1"
    # The demo tab sends none of the new fields; its defaults are the old behaviour.
    demo = pm.AWSPackerBuildRequest(**base)
    assert (demo.register_mode, demo.os_type, demo.provisioner_files, demo.region) == \
        ("export", "Linux", [], None)


def test_recipe_scripts_exist_and_are_shipped():
    for r in (r for cloud in rec.RECIPES.values() for r in cloud.values()):
        for rel in r["files"]:
            assert os.path.isfile(os.path.join(_ROOT, "provisioners", rel)), rel
            assert pm._BUNDLED_RE.match(rel), rel
    docker = _read("Dockerfile")
    assert re.search(r"^COPY provisioners/ \./provisioners/$", docker, re.M), \
        "without the COPY every standard build fails naming its missing script"
    assert "provisioners" not in _read(".dockerignore")
    with open(os.path.join(_ROOT, "provisioners", "pov", "pov-ready-linux.sh"), "rb") as fh:
        assert b"\r\n" not in fh.read(), "CRLF breaks the shebang"


# ── generated templates ──────────────────────────────────────────────────────

def test_linux_template_runs_the_scripts_in_order():
    t = ps.generate_aws_template(
        source_ami="ami-123", instance_type="t3.medium", ssh_username="ubuntu",
        image_name="pov-standard-linux", has_provisioner=True,
        provisioner_env={"BT_ADMIN_USER": "adminuser"},
        scripts=["pov-ready-linux.sh", "provision.sh", "bt-ready-debian.sh"],
        extra_tags={"pov-recipe": "standard-linux"})
    assert 'scripts = ["pov-ready-linux.sh", "provision.sh", "bt-ready-debian.sh"]' in t
    assert 'script = "provision.sh"' not in t
    assert '"BT_ADMIN_USER=adminuser"' in t
    assert '"pov-recipe" = "standard-linux"' in t


def test_demo_linux_template_unchanged():
    t = ps.generate_aws_template(
        source_ami="ami-123", instance_type="t3.micro", ssh_username="ec2-user",
        image_name="demo", has_provisioner=True)
    assert 'script = "provision.sh"' in t
    assert 'Name    = "demo"' in t and 'BuiltBy = "vm-dashboard"' in t
    assert "subnet_id" not in t and "run_tags" not in t


_RUN_TAGS = {"povManagedBy": "vm-dashboard", "managed-by": "vm-dashboard",
             "povRole": "image-build"}


def test_pov_templates_build_in_the_image_subnet_with_every_resource_tagged():
    """The POV provisioning role launches only into a subnet it built and creates only
    resources tagged as its own: the default VPC and an untagged temporary key pair were
    a 403 on ec2:CreateKeyPair for both standard builds."""
    linux = ps.generate_aws_template(
        source_ami="ami-123", instance_type="t3.medium", ssh_username="ubuntu",
        image_name="pov-standard-linux", has_provisioner=False,
        subnet_id="subnet-0abc", run_tags=_RUN_TAGS)
    windows = ps.generate_aws_windows_template(
        source_ami="ami-win", instance_type="t3.large", image_name="pov-standard-windows",
        subnet_id="subnet-0abc", run_tags=_RUN_TAGS)
    for t in (linux, windows):
        assert 'subnet_id                   = "subnet-0abc"' in t
        assert "associate_public_ip_address = true" in t
        # The temporary security group admits only Packer's own address, never 0.0.0.0/0.
        assert "temporary_security_group_source_public_ip = true" in t
        for block in ("run_tags", "run_volume_tags", "snapshot_tags"):
            body = t[t.index(f"  {block} = {{"):]
            assert '"povManagedBy" = "vm-dashboard"' in body[:body.index("}")], block
        ami = t[t.index("  tags = {"):]
        assert '"povManagedBy" = "vm-dashboard"' in ami[:ami.index("}")]



def test_named_source_cidrs_replace_packers_address_lookup():
    for t in (ps.generate_aws_template(
                  source_ami="ami-1", instance_type="t3.medium", ssh_username="ec2-user",
                  image_name="pov-standard-linux", has_provisioner=False,
                  subnet_id="subnet-0abc", run_tags=_RUN_TAGS,
                  source_cidrs=["203.0.113.7/32", "198.51.100.0/28"]),
              ps.generate_aws_windows_template(
                  source_ami="ami-win", instance_type="t3.large",
                  image_name="pov-standard-windows", subnet_id="subnet-0abc",
                  run_tags=_RUN_TAGS, source_cidrs=["203.0.113.7/32"])):
        assert "temporary_security_group_source_cidrs = [\"203.0.113.7/32\"" in t
        assert "temporary_security_group_source_public_ip" not in t

def test_pov_build_service_places_the_build():
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = src[src.index("async def _run_aws_build"):src.index("def _azure_build_credentials")]
    assert "pov_cloud_aws.ensure_image_build_subnet(region)" in body
    assert body.count("**placement") == 2, "both the Linux and Windows templates"
    drv = _read("web_dashboard", "services", "pov_cloud_aws.py")
    fn = drv[drv.index("def image_build_tags"):drv.index("def _image_build_spec")]
    # Never the environment tag: that is what every listing and teardown groups on.
    assert "TAG_ENVIRONMENT" not in fn and "TAG_MANAGED_BY" in fn


def test_provision_policy_covers_the_packer_build():
    tf = _read("terraform", "pov_cloud_aws_policy", "main.tf")
    for action in ("ec2:CreateKeyPair", "ec2:CreateImage", "ec2:DeregisterImage",
                   "ec2:DeleteSnapshot"):
        assert f'"{action}"' in tf, action
    creating = tf[tf.index("creating_actions = ["):]
    creating = creating[:creating.index("]")]
    assert '"CreateKeyPair"' in creating and '"CreateImage"' in creating


def test_windows_template():
    t = ps.generate_aws_windows_template(
        source_ami="ami-win", instance_type="t3.large", image_name="pov-standard-windows",
        scripts=["bt-ready-windows.ps1", "pov-ready-windows.ps1"],
        provisioner_env={"POV_STAGE_NAME": "epm.msi"},
        provisioner_sensitive_var_names={"TOKEN": "penv_0"})
    assert 'communicator             = "winrm"' in t
    assert 'winrm_username           = "Administrator"' in t
    assert "winrm_password" not in t, "the password is EC2's, read back — never set"
    assert 'user_data_file           = "winrm_bootstrap.ps1"' in t
    assert 'provisioner "powershell"' in t and 'provisioner "windows-restart"' in t
    assert '"POV_STAGE_NAME=epm.msi"' in t and '"TOKEN=${var.penv_0}"' in t
    assert 'variable "penv_0"' in t
    # EC2Launch's sysprep, after the restart, after the sealed marker.
    assert "ec2launch.exe' sysprep" in t
    assert "Sysprep.exe" not in t
    assert t.index("windows-restart") < t.index("image-sealed") < t.index("ec2launch.exe")
    assert ps.AWS_WINRM_BOOTSTRAP.startswith("<powershell>")


def test_windows_boot_task_waits_for_the_seal():
    """The boot task turns Basic auth off and drops the build's HTTPS listener. Run on the
    BUILD instance across its windows-restart, it would cut Packer's own connection."""
    s = _read("provisioners", "pov", "pov-ready-windows.ps1")
    assert "if (-not (Test-Path 'C:\\BT\\image-sealed')) { return }" in s
    assert "LocalAccountTokenFilterPolicy" in s
    assert "-LocalPort 5985,5986" in s
    assert "packer-winrm-https" in s


# ── the build path ───────────────────────────────────────────────────────────

def test_build_service_registers_the_ami_and_fails_loudly():
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = src[src.index("async def _run_aws_build"):src.index("def _azure_build_credentials")]
    assert 'req.register_mode == "ami_only"' in body and "register_aws_ami(" in body
    assert 'if result.get("register_error")' in body, \
        "an unregistered AMI is not a finished POV build"
    assert "req.region or _cfg(\"aws_region\")" in body
    # The operator's script goes before the sealing script, never after.
    assert 'scripts.insert(len(scripts) - 1, "provision.sh")' in body
    fn = src[src.index("def _stage_bundled"):src.index("def register_aws_ami")]
    assert "root not in src.parents" in fn and "is_file()" in fn


def test_windows_family_is_known_and_checked():
    aws = _read("web_dashboard", "services", "aws_service.py")
    assert '"windows-server-2022"' in aws and '"os_type": "Windows"' in aws
    src = _read("web_dashboard", "services", "packer_build_service.py")
    assert "is a {fam.get('os_type')} " in src


def test_recipe_column_and_migration():
    assert "recipe = Column(String(40), nullable=True)" in _read("web_dashboard", "database.py")
    mig = _read("web_dashboard", "schema_migrations.py")
    assert 'Migration("0010_registered_image_recipe"' in mig
    assert 'add_column("registered_images", "recipe", "VARCHAR(40)")' in mig


# ── Azure ────────────────────────────────────────────────────────────────────

def test_azure_linux_recipe_request():
    req = rec.build_request("azure", "standard-linux", region="eastus")
    assert isinstance(req, pm.AzurePackerBuildRequest)
    assert (req.image_publisher, req.image_offer, req.image_sku) == \
        ("Canonical", "ubuntu-24_04-lts", "server"), "Ubuntu 24.04 Gen2"
    assert req.os_type == "Linux"
    assert req.register_mode == "image_only", "a POV build registers the image, not a VHD"
    assert req.region == "eastus"
    assert req.recipe == "standard-linux"
    assert req.trusted_launch is False
    assert req.provisioner_files == rec.RECIPES["aws"]["standard-linux"]["files"]
    assert req.provisioner_files[-1] == "beyondtrust/bt-ready-debian.sh"
    assert req.bt_admin_user == "adminuser"
    env = {e.name: e.value for e in req.provisioner_env_vars}
    # The Match block admits the account an Azure POV VM is created with.
    assert env["POV_ADMIN_USER"] == rec.AZURE_PLATFORM_ADMIN_USER == "povadmin"
    assert env["BT_ADMIN_NOPASSWD_ALL"] == "1"
    assert env["POV_PREPULL_IMAGES"] == "agent:latest gateway:latest ansible:latest"
    # The AWS recipe keeps adminuser there: its POV sets adminuser's password.
    aws_env = {e.name: e.value for e in
               rec.build_request("aws", "standard-linux", region="us-east-2").provisioner_env_vars}
    assert aws_env["POV_ADMIN_USER"] == "adminuser"


def test_azure_platform_admin_matches_the_driver():
    drv = _read("web_dashboard", "services", "pov_cloud_azure.py")
    assert f'ADMIN_USERNAME = "{rec.AZURE_PLATFORM_ADMIN_USER}"' in drv, \
        "the Match block would admit an account no Azure POV VM has"
    assert "disable_password_authentication=False" in drv


def test_azure_windows_recipe_request():
    req = rec.build_request("azure", "standard-windows", region="westus3",
                            toggles={"stage_installer": True}, stage_asset="epm-w.msi")
    assert (req.image_publisher, req.image_offer, req.image_sku) == \
        ("MicrosoftWindowsServer", "WindowsServer", "2022-datacenter-g2")
    assert req.os_type == "Windows"
    assert req.vm_size == "Standard_D2s_v3", "B-series makes a Windows build crawl"
    assert req.provisioner_files == ["beyondtrust/bt-ready-windows.ps1",
                                     "pov/pov-ready-windows.ps1"]
    assert req.bt_stage_asset == "epm-w.msi"
    assert req.register_mode == "image_only" and req.region == "westus3"


def test_azure_model_pins_location_and_mode():
    base = dict(image_name="x")
    for bad in ("East US", "eastus; rm", "us-east-1", "-eastus"):
        try:
            pm.AzurePackerBuildRequest(**base, region=bad)
        except ValueError:
            continue
        raise AssertionError(f"location {bad!r} must be refused")
    assert pm.AzurePackerBuildRequest(**base, region="EastUS2").region == "eastus2"
    for bad in ("ami_only", "gallery"):
        try:
            pm.AzurePackerBuildRequest(**base, register_mode=bad)
        except ValueError:
            continue
        raise AssertionError(f"register_mode {bad!r} must be refused")
    try:
        pm.AzurePackerBuildRequest(**base, register_mode="image_only", trusted_launch=True)
    except ValueError:
        pass
    else:
        raise AssertionError("Trusted Launch cannot be captured as a managed image")
    try:
        pm.AzurePackerBuildRequest(**base, provisioner_files=["../x.sh"])
    except ValueError:
        pass
    else:
        raise AssertionError("a bundled path outside provisioners/ must be refused")
    # The demo tab sends none of the new fields; its defaults are the old behaviour.
    demo = pm.AzurePackerBuildRequest(**base)
    assert (demo.register_mode, demo.os_type, demo.provisioner_files, demo.region,
            demo.recipe) == ("export", "Linux", [], None, None)


def test_azure_linux_template_runs_the_scripts_in_order():
    t = ps.generate_azure_template(
        image_publisher="Canonical", image_offer="ubuntu-24_04-lts", image_sku="server",
        vm_size="Standard_B2s", image_name="pov-standard-linux", has_provisioner=True,
        provisioner_env={"POV_ADMIN_USER": "povadmin"},
        provisioner_sensitive_var_names={"TOKEN": "penv_0"},
        scripts=["pov-ready-linux.sh", "provision.sh", "bt-ready-debian.sh"],
        extra_tags={"pov-recipe": "standard-linux"})
    assert 'scripts         = ["pov-ready-linux.sh", "provision.sh", "bt-ready-debian.sh"]' in t
    assert 'script          = "provision.sh"' not in t
    assert '"POV_ADMIN_USER=povadmin"' in t and '"TOKEN=${var.penv_0}"' in t
    assert 'variable "penv_0"' in t
    assert '"pov-recipe" = "standard-linux"' in t and "azure_tags = {" in t
    assert 'managed_image_name' in t and "shared_image_gallery_destination" not in t
    # waagent generalizes after every script, bt-ready's seal included.
    assert t.index("bt-ready-debian.sh") < t.index("waagent -force -deprovision+user")


def test_azure_demo_linux_template_unchanged():
    t = ps.generate_azure_template(
        image_publisher="Canonical", image_offer="0001-com-ubuntu-server-jammy",
        image_sku="22_04-lts", vm_size="Standard_B2s", image_name="demo",
        has_provisioner=True)
    assert 'script          = "provision.sh"' in t
    assert "azure_tags" not in t


def test_azure_windows_template():
    t = ps.generate_azure_windows_template(
        image_publisher="MicrosoftWindowsServer", image_offer="WindowsServer",
        image_sku="2022-datacenter-g2", vm_size="Standard_D2s_v3",
        image_name="pov-standard-windows", has_provisioner=False,
        scripts=["bt-ready-windows.ps1", "pov-ready-windows.ps1"],
        provisioner_env={"POV_STAGE_NAME": "epm.msi"},
        provisioner_sensitive_var_names={"TOKEN": "penv_0"},
        extra_tags={"pov-recipe": "standard-windows"})
    assert 'scripts = ["bt-ready-windows.ps1", "pov-ready-windows.ps1"]' in t
    # The env the AWS Windows build always had, and the Azure one used to drop.
    assert '"POV_STAGE_NAME=epm.msi"' in t and '"TOKEN=${var.penv_0}"' in t
    assert 'variable "penv_0"' in t
    assert 'communicator   = "winrm"' in t
    assert '"pov-recipe" = "standard-windows"' in t
    # Sysprep (not EC2Launch), after the restart, after the sealed marker.
    assert "ec2launch" not in t.lower()
    assert t.index("windows-restart") < t.index("image-sealed") < t.index("Sysprep.exe")


def test_azure_demo_windows_template_unchanged():
    bare = ps.generate_azure_windows_template(
        image_publisher="MicrosoftWindowsServer", image_offer="WindowsServer",
        image_sku="2022-datacenter", vm_size="Standard_D2s_v3", image_name="demo",
        has_provisioner=False)
    assert 'provisioner "windows-restart"' not in bare
    assert "environment_vars" not in bare and "azure_tags" not in bare
    with_script = ps.generate_azure_windows_template(
        image_publisher="MicrosoftWindowsServer", image_offer="WindowsServer",
        image_sku="2022-datacenter", vm_size="Standard_D2s_v3", image_name="demo",
        has_provisioner=True)
    assert 'script = "provision.ps1"' in with_script


def test_windows_boot_task_drops_the_azure_build_listener():
    s = _read("provisioners", "pov", "pov-ready-windows.ps1")
    # Packer's azure-arm WinRM cert is named after its temporary VM, not "packer".
    assert "$cert.Subject -like 'CN=pkrvm*'" in s
    assert "$cert.Subject -eq 'CN=packer'" in s


def test_azure_build_service_registers_the_image_and_fails_loudly():
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = src[src.index("async def _run_azure_build"):src.index("async def _run_gcp_build")]
    assert 'image_only = req.register_mode == "image_only"' in body
    assert 'req.region or _cfg("azure_location")' in body
    # The builder's location follows build_resource_group_name, so a POV-location group.
    assert "resource_group = _pov_image_resource_group(location)" in body
    assert "azure_service.ensure_resource_group(" in body
    assert "is_linux_gallery = not is_windows and not image_only" in body
    assert "_stage_bundled(build_dir, req.provisioner_files)" in body
    assert 'scripts.insert(len(scripts) - 1, "provision.sh")' in body
    assert 'scripts.append("provision.ps1")' in body
    assert "register_azure_image(" in body and 'if result.get("register_error")' in body
    # The recipe reaches the catalog on every Azure registration path.
    assert "os_type=req.os_type, recipe=req.recipe))" in body
    assert "os_type=req.os_type, recipe=req.recipe, location=location," in body
    # The Windows Server provisioner now gets the env.
    assert "if (has_script or scripts) and not is_win_client:" in body
    reg = src[src.index("def register_azure_image"):src.index("async def _register_gallery_image")]
    assert 'source_cloud="azure"' in reg and "recipe=recipe" in reg
    assert "source_region=location" in reg
    assert 'return f"pov-images-{location}"' in src
    azure = _read("web_dashboard", "services", "azure_service.py")
    assert "async def ensure_resource_group(" in azure


def test_api_queues_the_job_per_cloud():
    api = _read("web_dashboard", "api", "pov_images.py")
    assert '"aws": "packer_aws_build"' in api and '"azure": "packer_azure_build"' in api
    assert "job_type=job_type" in api
    assert 'job_type="packer_aws_build"' not in api
    assert "Job.job_type.in_(tuple(_BUILD_JOB_TYPES.values()))" in api
    worker = _read("web_dashboard", "services", "packer_build_service.py")
    assert '"packer_azure_build": AzurePackerBuildRequest' in worker


def test_azure_driver_boots_a_managed_image_id():
    """The catalog row holds the managed image's resource id; the driver must take it."""
    drv = _read("web_dashboard", "services", "pov_cloud_azure.py")
    fn = drv[drv.index("def _image_reference"):]
    fn = fn[:fn.index("\ndef ")]
    assert 'startswith("/")' in fn


# ── the router and its gates ─────────────────────────────────────────────────

def test_router_is_a_pov_route_on_the_existing_scope():
    main = _read("web_dashboard", "main.py")
    m = re.search(r"app\.include_router\(pov_images_api\.router,\s*"
                  r"dependencies=\[_feature_gate\(\"pov_environments_enabled\"\)\]\)", main)
    assert m, "the image builder must 404 on a demo instance with the rest of POV"
    for name in ("images", "packer"):
        assert re.search(rf"app\.include_router\({name}\.router,\s*dependencies="
                         rf"\[_profile_page_gate\(\"cloud_pages\"\)\]\)", main), \
            f"/api/{name} must stay behind cloud_pages"
    api = _read("web_dashboard", "api", "pov_images.py")
    assert 'require_permission("pov_templates", "read")' in api
    assert 'require_explicit_permission("pov_templates", "write")' in api
    assert 'require_explicit_permission("pov_templates", "delete")' in api
    assert '"pov_images"' not in _read("web_dashboard", "api", "auth.py"), \
        "a new scope silently revokes it from every explicit map"
    # Storage-backed options keep the storage permission and asset scoping.
    assert 'has_permission(user, "storage", "read")' in api
    assert "asset_scope_service.check(" in api
    # A catalog row a template still uses is not deletable.
    assert "PovCloudTemplateVM.image_ref == image_id" in api


def test_template_editor_picker():
    page = _read("web_dashboard", "templates", "pov", "templates.html")
    assert 'placeholder="ami-… or catalog id"' not in page, \
        "a catalog id typed there was saved as a literal and failed at RunInstances"
    fn = page[page.index("pickImage(vm) {"):page.index("imageLabel(img) {")]
    assert "vm.image_ref = ''" in fn and "vm.image_id = ''" in fn
    assert "vm.os_family = img.os_family" in fn
    assert "{'slug': 'images'" in page
    assert "images: this.showsCloudTemplates" in page
    # The picker's own state never reaches the API.
    save = page[page.index("async saveCloudTemplate()"):page.index("async deleteCloudTemplate")]
    assert "_pick" not in save


# ── the Standard Linux login ─────────────────────────────────────────────────

def test_standard_linux_targets_get_the_povs_login():
    env = _read("web_dashboard", "services", "pov_cloud_env.py")
    assert '"login": _login_kind(row) if role == "target" else ""' in env
    kind = env[env.index("def _login_kind"):]
    kind = kind[:kind.index("\ndef ")]
    assert "image_id" in kind, "a literal image id must never claim the login"
    aws = _read("web_dashboard", "services", "pov_cloud_aws.py")
    assert 'spec.get("login") == STANDARD_LINUX_LOGIN' in aws
    assert "_remember_linux_login_vm(env_id, vm[\"id\"])" in aws
    holds = aws[aws.index("def holds_login"):aws.index("def _password_data_sync")]
    assert 'family == "linux" and vm_id' in holds, "Linux is answered per VM, not per POV"
    assert "_forget_linux_login(env_id)" in aws
    rb = _read("web_dashboard", "services", "pov_resource_broker.py")
    assert "vm.platform_vm_id or \"\"" in rb


def test_linux_image_allows_password_login_for_the_admin_only():
    s = _read("provisioners", "pov", "pov-ready-linux.sh")
    assert "Match User ${ADMIN_USER}" in s
    assert "99-pov-ready.conf" in s
    assert "docker-compose-plugin" in s


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
