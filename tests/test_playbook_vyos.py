"""The VyOS site-to-site VPN play in examples/playbooks/network/.

What these pin: it refuses without confirm: true; the pre-shared keys appear only in the
no_log apply task, never in the configuration it prints; it advertises only the DC
subnets and accepts only the cloud networks; the forward firewall drops by default and
opens AD ports only to the DCs; and the README lists it.

Run: python tests/test_playbook_vyos.py   (or under pytest)
"""
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "examples", "playbooks", "network", "vyos-ad-site-vpn.yml")


def _tasks():
    return yaml.safe_load(open(_PATH, encoding="utf-8"))[0]["tasks"]


def _task(name_part):
    return next(t for t in _tasks() if name_part in t["name"])


def test_refuses_without_confirm():
    play = yaml.safe_load(open(_PATH, encoding="utf-8"))[0]
    assert play["vars"]["confirm"] is False
    assert "confirm | bool" in _tasks()[0]["ansible.builtin.assert"]["that"]


def test_keys_only_in_the_no_log_apply_task():
    built = _task("Build the configuration")["ansible.builtin.set_fact"]["_config"]
    # "pre-shared-secret" is the auth MODE; the key itself is a `secret <value>` line.
    assert " secret " not in built and "_psks" not in built
    apply = _task("Apply and save")
    assert apply["no_log"] is True and "vyos.vyos.vyos_config" in apply
    assert "_psks" in yaml.safe_dump(apply)
    assert _task("Resolve each tunnel")["no_log"] is True


def test_routing_and_firewall_are_narrow():
    built = _task("Build the configuration")["ansible.builtin.set_fact"]["_config"]
    assert "route-map export AD-DC-OUT" in built and "route-map import AD-CLOUD-IN" in built
    assert "set firewall ipv4 name AD-CLOUD-TO-DC default-action drop" in built
    assert "destination group address-group AD-DCS" in built
    assert "inbound-interface name vti" in built


def test_readme_lists_it():
    readme = open(os.path.join(_ROOT, "examples", "playbooks", "README.md"),
                  encoding="utf-8").read()
    assert "`vyos-ad-site-vpn.yml`" in readme


if __name__ == "__main__":
    import sys
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
