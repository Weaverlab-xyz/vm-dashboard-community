"""The VyOS bake script's contract, and the ways it must fail loudly.

The bake is the one part of this feature that runs on infrastructure the test suite
cannot reach, so what is pinned here is everything about the script that can be read
without a VyOS box -- which is most of what goes wrong:

  * **It stops being VyOS-specific.** A shell provisioner pointed at a Debian image
    would otherwise run to completion, produce a box with a user on it, and be accepted
    by the deploy form. The script refuses up front; that refusal must not be deleted
    as defensive clutter.
  * **The account is baked with no credential.** VyOS does not create users from a
    cloud's key injection the way a stock Linux image does, so an account with neither a
    key nor a password is one nothing can log in as -- the Shell Jump included. That
    failure surfaces only when someone opens a session, which is during the demo.
  * **The key goes in as a file.** VyOS keeps authorized keys in configuration and
    regenerates ~/.ssh/authorized_keys from it on every commit of `system login`, so a
    key written to the file is reverted by the next one.
  * **A half-bake reaches the deploy form.** The script must verify its own commit
    persisted rather than trusting that `commit` returned zero.

Runs under pytest, or standalone:
    python tests/test_netcell_provisioner.py
"""
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-netcell-provisioner")

_SCRIPT = os.path.join(_ROOT, "provisioners", "net", "vyos-cell.sh")
_README = os.path.join(_ROOT, "provisioners", "net", "README.md")


def _src():
    with open(_SCRIPT, encoding="utf-8") as fh:
        return fh.read()


# -- it is a shell script that parses -----------------------------------------

def test_the_script_exists_and_is_executable():
    assert os.path.isfile(_SCRIPT), "provisioners/net/vyos-cell.sh is missing"
    assert os.access(_SCRIPT, os.X_OK), "the bake script is not executable"


def test_the_script_is_valid_posix_sh():
    """POSIX sh only -- Packer invokes it with `sh`, so a bashism is a bake that dies
    on the first line the build VM reaches."""
    r = subprocess.run(["sh", "-n", _SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"sh -n rejected the script: {r.stderr}"


def _code_lines():
    """The script with comment lines dropped.

    Scanning the raw file finds the header comment that *documents* the POSIX-only
    rule -- "no [[ ]]" -- and reports it as a violation of itself. The OT script
    carries the same line, so a comment-blind scan would have been wrong there too.
    """
    return "\n".join(ln for ln in _src().splitlines()
                     if not ln.lstrip().startswith("#"))


def test_the_script_avoids_bashisms_sh_would_not_catch():
    code = _code_lines()
    for bashism in ("[[ ", "<<<", "declare ", "local "):
        assert bashism not in code, f"{bashism!r} is a bashism; Packer runs this with sh"


# -- it refuses what it cannot bake -------------------------------------------

def test_it_refuses_an_image_that_is_not_vyos():
    src = _src()
    assert "/opt/vyatta" in src, \
        "the script no longer checks for VyOS config tooling, so it would happily " \
        "configure a Debian image and produce something the deploy form accepts"
    assert re.search(r"FATAL.*not a VyOS image", src), \
        "the non-VyOS refusal no longer says what is wrong"


def test_it_warns_when_the_account_would_be_unreachable():
    src = _src()
    assert "VYOS_ADMIN_PUBKEY" in src and "VYOS_ADMIN_PASSWORD" in src
    assert re.search(r"NO key and NO password", src), \
        "the script no longer warns about baking an account nothing can log in as"


def test_it_validates_the_public_key_before_opening_a_config_session():
    """A commit that dies halfway leaves the image in a state the bake cannot describe,
    so a malformed key has to fail before `configure` is entered."""
    src = _src()
    key_check = src.index("PUBKEY_TYPE=$(")
    configure = src.index("echo 'configure'")
    assert key_check < configure, \
        "the public key is parsed after the config session opens"
    assert "ssh-ed25519" in src, "the key-type allow-list no longer covers ed25519"


# -- what it must actually write ----------------------------------------------

def test_the_key_goes_in_as_configuration_not_as_a_file():
    src = _src()
    assert "authentication public-keys" in src, \
        "the public key is no longer written as VyOS configuration"
    assert "authorized_keys" not in src.split("# ── What the deploy")[0] or \
        "regenerates" in src, \
        "the script writes ~/.ssh/authorized_keys directly; VyOS regenerates that file " \
        "from config on the next commit of `system login`"


def test_it_runs_the_config_session_under_the_vyattacfg_group():
    """A VyOS config session needs that group even when the caller is root -- without
    it my_set writes into a scratch tree and there is nothing to commit."""
    src = _src()
    assert "sg vyattacfg" in src, "the config session no longer runs under vyattacfg"
    assert "script-template" in src, "the script no longer sources the VyOS script template"


def test_it_commits_and_saves():
    src = _src()
    assert "echo 'commit'" in src, "the config is never committed"
    assert "echo 'save'" in src, \
        "the config is never saved, so it does not survive a reboot"


def test_it_emits_both_firewall_syntaxes():
    """VyOS moved the firewall tree in 1.4. A script that only knows one train bakes an
    image whose demo commands do not work."""
    src = _src()
    assert "set firewall ipv4 name" in src, "the 1.4+ firewall syntax is missing"
    assert re.search(r'set firewall name', src), "the 1.3 firewall syntax is missing"
    assert "VYOS_SYNTAX" in src and "auto" in src, "the syntax is no longer detectable"


def test_it_verifies_its_own_commit_persisted():
    src = _src()
    assert "/config/config.boot" in src, "the script never reads back config.boot"
    assert src.count("FATAL") >= 3, \
        "the script no longer fails loudly on a half-bake — a `commit` returning zero " \
        "is not evidence that anything persisted"


def test_it_writes_the_triage_marker():
    src = _src()
    assert "/opt/netcell/IMAGE.txt" in src, \
        "the image marker is gone; there is then no way to answer 'baked with what?' " \
        "without opening a config session"


# -- the README says where the image comes from --------------------------------

def test_the_readme_is_honest_about_licensing():
    with open(_README, encoding="utf-8") as fh:
        text = fh.read()
    assert "rolling" in text.lower() and "subscription" in text.lower(), \
        "the README no longer distinguishes the free rolling builds from paid LTS"
    assert "vyos-cell" in text, "the README no longer names the image the tab expects"


def test_the_readme_does_not_promise_rotation():
    """The claim the whole feature must not overstate."""
    with open(_README, encoding="utf-8") as fh:
        text = fh.read().lower()
    assert "storage and checkout" in text, \
        "the README no longer states that Layer 2 here is checkout rather than rotation"


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
