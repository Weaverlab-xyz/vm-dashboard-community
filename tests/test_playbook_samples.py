"""Structural validation for the sample playbooks in examples/playbooks/.

Guarantees each shipped playbook is valid YAML and a well-formed Ansible play
list (every play has hosts + tasks/roles), and that Windows samples declare a
WinRM connection. This is a cheap guard against a malformed sample shipping; it
is not a full `ansible-playbook --syntax-check` (no ansible dependency here).

Run: python tests/test_playbook_samples.py   (or under pytest)
"""
import glob
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLAYBOOKS = sorted(glob.glob(os.path.join(_ROOT, "examples", "playbooks", "**", "*.yml"), recursive=True))


def _check_play_list(path):
    with open(path) as f:
        doc = yaml.safe_load(f)
    rel = os.path.relpath(path, _ROOT)
    assert isinstance(doc, list) and doc, f"{rel}: not a non-empty play list"
    for play in doc:
        assert isinstance(play, dict), f"{rel}: a play is not a mapping"
        assert "hosts" in play, f"{rel}: a play has no 'hosts'"
        assert play.get("tasks") or play.get("roles"), f"{rel}: a play has no tasks/roles"
    # Windows samples must declare a WinRM connection (in vars).
    if os.sep + "windows" + os.sep in path:
        text = open(path).read()
        assert "ansible_connection: winrm" in text, f"{rel}: windows play missing winrm connection"


def test_samples_exist():
    assert _PLAYBOOKS, "no sample playbooks found in examples/playbooks/"


def test_all_playbooks_valid():
    for path in _PLAYBOOKS:
        _check_play_list(path)


if __name__ == "__main__":
    import sys
    if not _PLAYBOOKS:
        print("FAIL: no playbooks found")
        sys.exit(1)
    failures = 0
    for p in _PLAYBOOKS:
        try:
            _check_play_list(p)
            print(f"ok   {os.path.relpath(p, _ROOT)}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {os.path.relpath(p, _ROOT)}: {e}")
    sys.exit(1 if failures else 0)
