"""The network cell's preflight refusals, and that each one carries its remedy.

A cell that deploys and then cannot be reached costs a bake, a VM and the demo it was
built for. So every problem this feature can know about up front is a refusal before
anything launches -- the shape ``ot_service.in_plant_agent_problem`` established -- and
the message is rendered straight into a 400, which means it has to say what to DO and
not merely what is wrong.

The rot this prevents:

  * **A refusal degrades into a launch.** Each guard returning "" for a bad input is a
    silent regression: the deploy succeeds and the failure surfaces in front of an
    audience instead.
  * **A remedy decays into a symptom.** "Wrong platform" tells an SE nothing at 9am on
    the morning of a demo. The test pins that the remedy names the config key.
  * **The platform guard becomes strict.** It must stay best-effort: a Password Safe
    lookup that did not answer is not a reason to refuse to deploy.

Runs under pytest, or standalone:
    python tests/test_netcell_guards.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-netcell-guards")

from web_dashboard.services import netcell_service as N  # noqa: E402


# -- release train -------------------------------------------------------------

def test_the_documented_release_trains_are_accepted():
    for r in N.VYOS_RELEASES:
        assert N.release_problem(r) == "", f"{r!r} is in VYOS_RELEASES but refused"


def test_an_undocumented_release_train_is_refused():
    for bad in ("1.2", "2.0", "rolling", "", "   ", None):
        assert N.release_problem(bad), f"{bad!r} was accepted as a release train"


def test_the_release_refusal_names_both_syntaxes():
    """The whole point of asking is that the commands differ. A refusal that does not
    say how they differ sends the reader to a changelog."""
    msg = N.release_problem("1.2")
    assert "set firewall name" in msg and "set firewall ipv4 name" in msg, (
        "the release refusal does not show the two firewall syntaxes it is distinguishing")


# -- image ---------------------------------------------------------------------

def test_a_vyos_image_is_accepted_by_name_or_by_link():
    assert N.image_problem("vyos-cell") == ""
    assert N.image_problem("my-vyos-1-4-build") == ""
    assert N.image_problem("", "projects/p/global/images/vyos-cell-01") == ""


def test_a_non_vyos_image_is_refused():
    assert N.image_problem("ot-sim"), "the OT image was accepted as a network cell"
    assert N.image_problem("rocky9-pws-ready"), "a general-purpose image was accepted"


def test_no_image_at_all_is_refused_and_says_where_to_get_one():
    msg = N.image_problem("", "")
    assert msg
    assert "provisioners/net" in msg, "the no-image refusal does not say how to bake one"


def test_the_image_refusal_explains_the_consequence():
    """"Not a VyOS image" is a fact. What an SE needs is why it matters -- a Linux image
    deploys and onboards perfectly well and then has no `configure` mode."""
    msg = N.image_problem("debian-12")
    assert "configure" in msg, "the image refusal never says what would be missing"


# -- Password Safe platform ----------------------------------------------------

def test_each_cloud_native_plugin_is_refused():
    for platform in ("AWS Systems Manager Custom Plugin",
                     "Azure VM SSH Rotation",
                     "GCP VM SSH Rotation"):
        assert N.ps_platform_problem(platform), f"{platform!r} was accepted"


def test_a_renamed_plugin_is_still_refused():
    """Matched on token sets rather than an exact phrase, for the reason
    ps_vm_hook._platform_name_ok spells out: admins rename platforms."""
    assert N.ps_platform_problem("Azure Waagent VM SSH Rotation"), \
        "a renamed Azure plugin slipped through"
    assert N.ps_platform_problem("  gcp vm ssh rotation  "), \
        "case and padding defeated the platform guard"


def test_an_ssh_platform_is_accepted():
    for ok in ("Linux", "SSH", "Unix - SSH", "VyOS (custom)"):
        assert N.ps_platform_problem(ok) == "", f"{ok!r} was refused"


def test_a_platform_that_did_not_resolve_never_blocks():
    """Best-effort on purpose. A Password Safe outage must not become a refusal to
    deploy -- the same posture ps_vm_hook takes on a blank platform name."""
    for blank in ("", "   ", None):
        assert N.ps_platform_problem(blank) == "", \
            f"an unresolved platform ({blank!r}) blocked the deploy"


def test_the_platform_refusal_names_the_config_key_to_change():
    msg = N.ps_platform_problem("GCP VM SSH Rotation")
    assert "passwordsafe_vm_functional_account_gcp" in msg, \
        "the platform refusal does not name the setting that fixes it"
    assert "agent" in msg, \
        "the platform refusal never says why the plugin cannot work (no agent on VyOS)"


# -- the forced method ---------------------------------------------------------

def test_the_onboarding_method_is_ssh_and_is_not_configurable():
    """The other three methods cannot work on this guest at all, so offering them would
    be offering a way to break the cell."""
    assert N.NETCELL_PS_METHOD == "ssh"


def test_the_deploy_notes_warn_about_rotation_when_onboarding():
    notes = " ".join(N.deploy_notes(True)).lower()
    assert "rotation" in notes, "the notes never mention the rotation limitation"
    assert "ssh" in notes, "the notes never say which onboarding method was forced"


def test_the_deploy_notes_stay_quiet_about_password_safe_when_it_is_off():
    notes = " ".join(N.deploy_notes(False)).lower()
    assert "rotation" not in notes, \
        "the notes warn about rotation on a cell that is not being onboarded"


# -- the marker ----------------------------------------------------------------

def test_the_cell_marker_has_one_spelling():
    assert N.is_cell({"netcell": True})
    assert not N.is_cell({"ot_cell": True})
    assert not N.is_cell({})
    assert not N.is_cell(None)


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
