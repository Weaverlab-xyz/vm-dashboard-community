"""Guard: the images the Azure sandbox mirrors into its ACR match the images the
dashboard actually wants, and the bash/PowerShell twins mirror the same list.

The Azure sandbox creates a Basic-SKU ACR, `az acr import`s a handful of public
runner images into it, and emits full ACR paths as config so the ACI runners pull
from the private mirror instead of hitting Docker Hub's anonymous pull limits.

Three ways that goes wrong, all silent:

* **Downgrade.** The mirrored image is not the image the app defaults to. This
  really happened: the loop mirrored upstream `willhallonline/ansible` while
  `Settings.ansible_aci_image` defaults to `chrweav/ansible-winrm` — same Ansible,
  but only the latter carries `pywinrm`, so every Windows/WinRM config-management
  target broke on a sandbox-configured instance. Nothing errors at setup time; the
  capability is just gone.
* **Unmirrored reference.** An emitted `$ACR/<image>` path whose image was never
  imported — the ACI pull 404s at deploy time.
* **Dead mirror.** An imported image nothing references — pays for the layers and
  suggests a half-landed wiring change.

Reads the scripts as text: no cloud account, no fastapi, no sqlalchemy.

Runs under pytest, or standalone:  python tests/test_sandbox_acr_mirror.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.config import Settings

_SH = os.path.join(_ROOT, "scripts", "sandbox", "Linux", "setup-azure.sh")
_PS1 = os.path.join(_ROOT, "scripts", "sandbox", "Windows", "Setup-AzureSandbox.ps1")
_TWINS = [_SH, _PS1]

# A Docker Hub reference: owner/name:tag.
_IMAGE = re.compile(r"\b([a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*:[\w.-]+)\b")

# The shell/PowerShell variable each twin holds the ACR login server in.
_ACR_VAR = re.compile(r"ACR_LOGIN_SERVER|AcrLoginServer")

# Which Settings field supplies the app default for each emitted image key. A
# per-cloud override key (k8s_runner_image_azure) defaults to "" and falls back to
# the shared field, so compare against the field that actually holds the image.
_KEY_DEFAULT_FIELD = {
    "ansible_aci_image": "ansible_aci_image",
    "k8s_runner_image_azure": "k8s_runner_image",
    "promote_runner_image": "promote_runner_image",
}

# Mirrored but referenced BARE, not as a full ACR path: the Jumpoint runner prepends
# `azure_acr_server` itself, and the VM-based tunnel Jumpoint shares that config key
# while docker-running without a registry login — so it must stay pullable from
# Docker Hub. See the emit comment in either twin.
_BARE_BY_DESIGN = {"beyondtrust/sra-jumpoint:latest"}


def _text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _mirrored(path):
    """The images inside the `az acr import` loop."""
    text = _text(path)
    if path.endswith(".ps1"):
        block = re.search(r"foreach \(\$img in @\((.*?)\)\) \{", text, re.S)
    else:
        block = re.search(r"for _img in\s(.*?); do", text, re.S)
    assert block, f"{os.path.basename(path)}: could not find the ACR import loop"
    return set(_IMAGE.findall(block.group(1)))


def _acr_referenced(path):
    """Images referenced with the ACR login server prefixed — i.e. expected in the
    mirror. Any line naming both the ACR variable and an image counts, which covers
    the `_cfg` emit lines and the PROMOTE_IMAGE/$PromoteImage assignment alike."""
    found = set()
    for line in _text(path).splitlines():
        if _ACR_VAR.search(line):
            found.update(_IMAGE.findall(line))
    return found


def _emitted_image_keys(path):
    """{config key: image} for every `<key>=$ACR/<owner/name:tag>` the twin emits."""
    pat = re.compile(r"(\w+)=\$(?:ACR_LOGIN_SERVER|AcrLoginServer)/([a-z0-9][^\s\"']*:[\w.-]+)")
    return dict(pat.findall(_text(path)))


def test_every_acr_reference_is_mirrored():
    """An emitted ACR path whose image was never imported 404s at ACI pull time."""
    failures = []
    for path in _TWINS:
        mirrored = _mirrored(path)
        for image in sorted(_acr_referenced(path) - mirrored):
            failures.append(f"{os.path.basename(path)}: references {image}, never imported")
    assert not failures, (
        "these images are referenced as $ACR/<image> but are missing from the "
        "`az acr import` loop — the ACI runner's pull will 404:\n  " + "\n  ".join(failures))


def test_no_dead_mirrors():
    """An imported image nothing references means a half-landed wiring change."""
    failures = []
    for path in _TWINS:
        referenced = _acr_referenced(path)
        for image in sorted(_mirrored(path) - referenced - _BARE_BY_DESIGN):
            failures.append(f"{os.path.basename(path)}: mirrors {image}, references nothing")
    assert not failures, (
        "these images are imported into the ACR but no emitted config points at "
        "them (add the config key, or drop the mirror):\n  " + "\n  ".join(failures))


def test_mirrored_images_match_the_app_defaults():
    """The sandbox must not silently swap a runner image for a less capable one.

    The mirror exists to dodge Docker Hub rate limits, not to change behaviour: an
    ACR path pointing at a different repo than the app's own default is a capability
    downgrade (pywinrm, in the case that prompted this test) with no error anywhere.
    """
    fields = Settings.model_fields
    failures = []
    for path in _TWINS:
        for key, image in sorted(_emitted_image_keys(path).items()):
            field = _KEY_DEFAULT_FIELD.get(key)
            if field is None:
                failures.append(
                    f"{os.path.basename(path)}: {key}={image} — unknown image key; add it to "
                    "_KEY_DEFAULT_FIELD with the Settings field holding its default")
                continue
            assert field in fields, f"_KEY_DEFAULT_FIELD names a missing field: {field}"
            expected = fields[field].default
            if image != expected:
                failures.append(
                    f"{os.path.basename(path)}: {key}=$ACR/{image} but Settings.{field} "
                    f"defaults to {expected}")
    assert not failures, (
        "the sandbox is pointing a runner at a DIFFERENT image than the app default — "
        "mirror the default image instead, or change the default:\n  " + "\n  ".join(failures))


def test_the_twins_mirror_and_emit_the_same_images():
    """.ps1/.sh twins are kept in parity by hand and are known to drift."""
    sh_mirror, ps1_mirror = _mirrored(_SH), _mirrored(_PS1)
    assert sh_mirror == ps1_mirror, (
        "ACR import loops differ:\n  only in setup-azure.sh: "
        f"{sorted(sh_mirror - ps1_mirror)}\n  only in Setup-AzureSandbox.ps1: "
        f"{sorted(ps1_mirror - sh_mirror)}")

    sh_keys, ps1_keys = _emitted_image_keys(_SH), _emitted_image_keys(_PS1)
    assert sh_keys == ps1_keys, (
        "emitted ACR image keys differ:\n  setup-azure.sh: "
        f"{sorted(sh_keys.items())}\n  Setup-AzureSandbox.ps1: {sorted(ps1_keys.items())}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
