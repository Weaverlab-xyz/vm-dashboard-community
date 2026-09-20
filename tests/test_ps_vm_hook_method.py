"""The per-deploy onboarding-method override, and the defaults it must not disturb.

``ps_vm_hook.register`` picks how a VM is onboarded into Password Safe, and the choice
is per-cloud: AWS over Systems Manager, Azure over Run Command, GCP by writing a key
into instance metadata. Each of those drives the guest through an agent, so a guest
running none of them -- a VyOS network cell -- must be able to ask for the traditional
SSH flow instead.

The rot this prevents is in both directions:

  * **The override silently stops working.** A cell would then onboard on a cloud plugin
    that can never rotate it, and would look healthy right up until the first rotation
    attempt -- which is long after the demo.
  * **The override leaks into the default path.** Every existing caller passes nothing,
    so a blank override has to resolve byte-for-byte to what the cloud resolved before.
    This is the more dangerous direction: it would change how every VM on the instance
    onboards, in a way no test of the new feature would notice.

Stubs nothing but ``config_service`` lookups, which ``_cfg`` reaches lazily.

Runs under pytest, or standalone:
    python tests/test_ps_vm_hook_method.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-ps-vm-hook-method")

from web_dashboard.services import ps_vm_hook as H  # noqa: E402


def _no_config():
    """Every per-cloud method key unset, so ``_registration_method`` returns its default."""
    return lambda key: ""


# -- the defaults, unchanged ---------------------------------------------------

def test_the_cloud_defaults_are_what_they_were():
    real = H._cfg
    H._cfg = _no_config()
    try:
        assert H._registration_method("aws") == "ssm"
        assert H._registration_method("azure") == "azurevm"
        assert H._registration_method("gcp") == "gcpvm"
        assert H._registration_method("oci") == "ssh"
        assert H._registration_method("") == "ssh"
    finally:
        H._cfg = real


def test_a_blank_override_changes_nothing():
    """The property that matters: every existing caller passes nothing."""
    real = H._cfg
    H._cfg = _no_config()
    try:
        for tag in ("aws", "azure", "gcp", "oci", "cloud", ""):
            for blank in ("", None, "   "):
                assert H._resolve_method(tag, blank) == H._registration_method(tag), (
                    f"a blank override changed {tag!r} from "
                    f"{H._registration_method(tag)!r} to {H._resolve_method(tag, blank)!r}")
    finally:
        H._cfg = real


def test_the_configured_override_still_wins_over_the_default():
    """The pre-existing global escape hatch must keep working alongside the new one."""
    real = H._cfg
    H._cfg = lambda key: "ssh" if key == "passwordsafe_gcp_registration_method" else ""
    try:
        assert H._registration_method("gcp") == "ssh"
        assert H._resolve_method("gcp", "") == "ssh"
    finally:
        H._cfg = real


# -- the override --------------------------------------------------------------

def test_an_override_beats_the_cloud_default():
    real = H._cfg
    H._cfg = _no_config()
    try:
        assert H._resolve_method("aws", "ssh") == "ssh"
        assert H._resolve_method("azure", "ssh") == "ssh"
        assert H._resolve_method("gcp", "ssh") == "ssh"
    finally:
        H._cfg = real


def test_an_override_is_normalised():
    """Callers pass a literal; a stray case or space must not produce an unknown method
    that falls through every branch in register() and onboards nothing."""
    real = H._cfg
    H._cfg = _no_config()
    try:
        for raw in ("SSH", " ssh ", "Ssh"):
            assert H._resolve_method("gcp", raw) == "ssh", f"{raw!r} did not normalise"
    finally:
        H._cfg = real


def test_register_accepts_the_override_as_a_keyword():
    """A positional would be silently swallowed by the keyword-only marker; this pins
    that the parameter is actually there and spelled as callers spell it."""
    import inspect
    sig = inspect.signature(H.register)
    assert "method" in sig.parameters, "register() no longer takes a method override"
    p = sig.parameters["method"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY, "method must stay keyword-only"
    assert p.default == "", "a non-empty default would change every existing caller"


def test_the_docstring_says_why_the_override_exists():
    """The override looks like a convenience until you know that three plugins need a
    guest agent. Without the reason written down, the next reader deletes it."""
    doc = H.register.__doc__ or ""
    assert "method" in doc
    for token in ("agent", "netcell"):
        assert token in doc, f"register() docstring never mentions {token!r}"


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
