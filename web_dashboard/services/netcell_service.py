"""The network demo cell: a VyOS router/firewall, and the guards that keep it honest.

**This module has no orchestrator, and that is the design.** The OT cell needs an
``ot_cell_deploy`` parent because it provisions a Web Jump and one protocol tunnel per
brokered endpoint once the VM is up; the parent exists to do that wiring and to own its
teardown. A firewall is reached over SSH and nothing else, and the plain
``gce_deploy`` path already provisions the Shell Jump, the Password Safe onboarding,
the shared-gateway reference, the expiry stamp and the inventory row. A parent job here
would wire nothing, so it would be a second lifecycle to keep correct in exchange for
nothing -- and the first person to extend it would have to work out why it existed.

So a cell is an ordinary VM deploy carrying three things this module supplies:

  * a **marker** (``netcell`` on the job metadata) so the tab, the tile and the cells
    list can find it. ``netcell``, not ``net_cell``: one token greps cleanly, and the
    persona registry's own note about ``itops`` over ``it`` makes the same argument.
  * a **forced Password Safe onboarding method**. Every one of the three cloud-native
    plugins drives the guest through an agent -- SSM, waagent, google-guest-agent --
    and VyOS runs none of them. Onboarded on the GCP default the cell attaches to a
    platform that can never manage it and looks healthy until the first attempt, which
    is long after the demo. ``ps_vm_hook.register`` grew a per-deploy ``method``
    override for exactly this.
  * **preflight refusals**, in the shape ``ot_service.in_plant_agent_problem`` uses:
    a function returning a remedy string, turned into a 400 by the route, so nothing
    launches before the problem is known. A cell that deploys and then cannot be
    reached costs a bake, a VM and the demo.

Everything the cell teardown needs is inherited: the row is a plain ``gce_deploy``, so
``gcp_vm_service._run_destroy`` and the expiry reaper already unwind the Shell Jump,
the Password Safe registration and the gateway reference with no netcell-specific path.

Dependencies stay light on purpose -- ``config_service`` and ``ot_service``'s existing
PRA preflight -- so nothing here needs the API layer or the database to be importable.
"""
import logging

logger = logging.getLogger(__name__)


class NetCellError(Exception):
    """An invalid network-cell request. The message is rendered straight into a 400, so
    it carries the remedy rather than just the symptom -- the same contract
    ``ot_service.OTCellError`` states for the failed-job page."""


# The image the bake produces (provisioners/net/vyos-cell.sh) and the network tag the
# cell carries. Same string: the tag exists so a firewall rule can name the cells, and
# a second vocabulary for the same idea is a second thing to keep in step.
NETCELL_IMAGE_NAME = "vyos-cell"
NETCELL_NETWORK_TAG = "vyos-cell"

# The Password Safe onboarding method a cell is always deployed with. Not configurable:
# the other three methods cannot work on this guest at all, so offering them would be
# offering a way to break the cell.
NETCELL_PS_METHOD = "ssh"

# Release trains whose firewall syntax the demo documents. The operator asserts which
# one the image is, because the dashboard cannot read it off an image and a wrong
# answer looks like a typo rather than a version mismatch.
VYOS_RELEASES = ("1.4", "1.3")

# Platform-name tokens that identify the three cloud-native Password Safe plugins. Any
# of them on the functional account means the managed system would inherit a platform
# that manages the guest through an agent VyOS does not run. Matched as lowercased
# token sets, tolerant of admin renames, for the reason ps_vm_hook._platform_name_ok
# spells out: a rigid phrase breaks the moment somebody renames the platform.
_AGENT_PLATFORM_TOKENS = (
    ("systems", "manager"),
    ("azure", "ssh rotation"),
    ("gcp", "ssh rotation"),
)


def release_problem(release: str) -> str:
    """Refuse a release train the demo has no documented command set for.

    Empty string means fine. The point is not that other VyOS versions fail to boot --
    they may well work -- but that the doc's `set firewall ...` lines are written for
    one tree or the other, and an SE reading them aloud against the wrong train gets a
    syntax error in front of an audience.
    """
    r = (release or "").strip()
    if r in VYOS_RELEASES:
        return ""
    return (f"VyOS release {release!r} is not one this demo has commands for "
            f"({', '.join(VYOS_RELEASES)}). The firewall tree moved in 1.4 — 1.3 is "
            "`set firewall name`, 1.4 and later are `set firewall ipv4 name`. Pick the "
            "train your image was built from; provisioners/net/README.md says how the "
            "bake records it.")


def image_problem(image_name: str, image_self_link: str = "") -> str:
    """Refuse an image that is not a network-cell bake.

    Name-matched, and that is a real limitation rather than a shortcut: nothing in a
    GCE image's metadata says what a provisioner script put inside it. The bake's own
    refusal is the strong check (vyos-cell.sh exits if /opt/vyatta is absent); this one
    exists to catch the much commoner mistake of picking a neighbouring image in a
    dropdown, which would otherwise deploy a Debian box the form calls a firewall.
    """
    name = (image_name or "").strip().lower()
    link = (image_self_link or "").strip().lower()
    if not name and not link:
        return ("No image selected. Bake one with provisioners/net/vyos-cell.sh and "
                f"name it {NETCELL_IMAGE_NAME!r} — see provisioners/net/README.md.")
    haystack = f"{name} {link}"
    if "vyos" in haystack:
        return ""
    return (f"{image_name or image_self_link!r} does not look like a VyOS image. A "
            f"network cell needs the {NETCELL_IMAGE_NAME!r} bake "
            "(provisioners/net/vyos-cell.sh); a general-purpose Linux image would "
            "deploy, onboard and then have no `configure` mode to demo.")


def ps_platform_problem(platform_name: str) -> str:
    """Refuse a Password Safe functional account bound to a cloud-native plugin.

    ``platform_name`` is the functional account's platform as Password Safe reports it.
    A blank name means the lookup did not answer, and a blank answer never blocks --
    the same best-effort posture ``ps_vm_hook._platform_name_ok`` takes, and for the
    same reason: a Password Safe outage should not become a refusal to deploy.

    The failure this avoids is the quiet one. All three plugins manage the guest through
    an agent -- SSM on AWS, waagent on Azure, google-guest-agent on GCP -- and VyOS runs
    none of them. The onboarding SUCCEEDS; the managed system simply inherits a platform
    that can never talk to the box, and nobody finds out until a rotation is attempted.
    """
    p = (platform_name or "").strip().lower()
    if not p:
        return ""
    for tokens in _AGENT_PLATFORM_TOKENS:
        if all(t in p for t in tokens):
            return (f"The configured Password Safe functional account is on platform "
                    f"{platform_name!r}, which manages a guest through an agent — SSM, "
                    "waagent or the Google guest agent. VyOS runs none of them, so the "
                    "cell would onboard successfully and then be unmanageable. Point "
                    "`passwordsafe_vm_functional_account_gcp` at a functional account on "
                    "an SSH platform, or deploy the cell with Password Safe onboarding "
                    "off.")
    return ""


def pra_preflight_problem() -> str:
    """The cell's PRA preflight, borrowed whole from the OT cell.

    Delegated rather than copied: a cell with no Jumpoint or Jump Group is a device
    nobody can reach, which is the same sentence on both features, and two copies of
    that check would drift the first time either cloud's config keys moved.
    """
    from . import ot_service
    return ot_service.pra_preflight_problem("gcp")


def is_cell(meta) -> bool:
    """True for a job row that is a network cell. One reader, so the marker's spelling
    lives in one place."""
    return bool((meta or {}).get("netcell"))


def cell_params(meta) -> dict:
    return dict(((meta or {}).get("netcell_params") or {}))


def deploy_notes(register_in_passwordsafe: bool) -> list:
    """What the form should say back, so a caveat is read before the demo rather than
    discovered during it.

    Rotation is the one that matters, and the note is deliberately about a MISSING
    PREREQUISITE rather than a missing capability. Password Safe rotates this account
    fine once it is bound to a platform whose change command runs in vbash and commits;
    what it cannot do is manage VyOS through a stock Linux platform, because VyOS
    regenerates authorized_keys from configuration and undoes a plain `passwd` at the
    next commit of `system login`. That platform is a Password Safe artifact, which
    CONTRIBUTING.md puts outside this repo -- so the operator supplies it, and until
    they do the demo is checkout rather than rotation.
    """
    notes = [
        "Reached over SSH through PRA. A firewall needs a Shell Jump and nothing else, "
        "so this cell provisions no Web Jump and no protocol tunnel.",
    ]
    if register_in_passwordsafe:
        notes.append(
            f"Password Safe onboarding is forced to the {NETCELL_PS_METHOD!r} method — "
            "the cloud-native plugins manage a guest through an agent VyOS does not run.")
        notes.append(
            "Rotation needs a VyOS platform whose change command commits in vbash; a "
            "stock Linux platform is reverted by the next commit of `system login`. "
            "Without one this is checkout, not rotation — see provisioners/net/README.md.")
    return notes
