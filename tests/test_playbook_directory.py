"""The directory playbooks in examples/playbooks/directory/.

What these pin:

- every file is listed in the README's directory section;
- every task that hands a password to a module (a bind password, a new password, a Password
  Safe lookup) is no_log: true;
- every play asserts its required vars before it changes anything;
- the destructive plays refuse without confirm: true;
- the audits use only read-only modules;
- AD plays are WinRM plays on `hosts: all`, LDAP plays are `hosts: localhost` plays, so each
  runs on the transport the run form offers for it;
- credentials come from the injected dir_* vars, never from a hard-coded value.

Run: python tests/test_playbook_directory.py   (or under pytest)
"""
import glob
import os
import re

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR = os.path.join(_ROOT, "examples", "playbooks", "directory")
_FILES = sorted(glob.glob(os.path.join(_DIR, "*.yml")))
_SECRET = re.compile(r"password|bind_pw|passwd|secrets_safe_lookup")
_READ_ONLY = {"microsoft.ad.object_info", "community.general.ldap_search",
              "ansible.builtin.assert", "ansible.builtin.set_fact", "ansible.builtin.debug",
              "ansible.builtin.set_stats"}
_KEYWORDS = {"name", "register", "loop", "when", "no_log", "loop_control", "changed_when",
             "failed_when", "tags"}


def _plays(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tasks(path):
    for play in _plays(path):
        for task in play.get("tasks") or []:
            yield play, task


def _module(task):
    mods = [k for k in task if k not in _KEYWORDS]
    assert len(mods) == 1, f"ambiguous task {task.get('name')}: {mods}"
    return mods[0]


def test_the_set_is_complete():
    names = {os.path.basename(p) for p in _FILES}
    expected = {"ad-user.yml", "ad-group.yml", "ad-ou.yml", "ad-reset-password.yml",
                "ad-remove-computer.yml", "ldap-entry.yml", "ldap-attrs.yml",
                "ldap-group-membership.yml", "ldap-password.yml", "ldap-search.yml",
                "ad-audit-stale-accounts.yml", "ad-audit-privileged-groups.yml",
                "ad-audit-stale-computers.yml"}
    assert expected <= names, expected - names


def test_readme_lists_every_file():
    readme = open(os.path.join(_ROOT, "examples", "playbooks", "README.md"),
                  encoding="utf-8").read()
    for path in _FILES:
        assert f"`{os.path.basename(path)}`" in readme, os.path.basename(path)


def test_every_secret_bearing_task_is_no_log():
    for path in _FILES:
        for _play, task in _tasks(path):
            mod = _module(task)
            if mod in ("ansible.builtin.assert", "ansible.builtin.debug",
                       "ansible.builtin.set_stats"):
                continue
            if _SECRET.search(yaml.safe_dump(task[mod])):
                assert task.get("no_log") is True, \
                    f"{os.path.basename(path)}: '{task.get('name')}' handles a secret"


def test_every_play_asserts_before_it_changes_anything():
    for path in _FILES:
        if "audit-privileged" in path:
            continue        # takes no required vars
        for play in _plays(path):
            mods = [_module(t) for t in play["tasks"]]
            first_change = next((i for i, m in enumerate(mods) if m not in _READ_ONLY),
                                len(mods))
            assert "ansible.builtin.assert" in mods[:first_change + 1], \
                f"{os.path.basename(path)} changes something before asserting its vars"


def test_destructive_plays_need_confirm():
    for name in ("ad-remove-computer.yml", "ldap-entry.yml"):
        text = open(os.path.join(_DIR, name), encoding="utf-8").read()
        assert "confirm: false" in text and "confirm | bool" in text, name


def test_audits_change_nothing():
    for path in _FILES:
        if "-audit-" not in path and not path.endswith("ldap-search.yml"):
            continue
        for _play, task in _tasks(path):
            assert _module(task) in _READ_ONLY, \
                f"{os.path.basename(path)}: {_module(task)} is not read-only"


def test_transport_matches_the_play_shape():
    for path in _FILES:
        base = os.path.basename(path)
        for play in _plays(path):
            if base.startswith("ad-"):
                assert play["hosts"] == "all", base
                assert play["vars"]["ansible_connection"] == "winrm", base
            else:
                assert play["hosts"] == "localhost" and play.get("connection") == "local", base


def test_credentials_come_from_injected_vars():
    for path in _FILES:
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r"(bind_pw|domain_password):\s*(.+)", text):
            assert "dir_bind_password" in m.group(2), f"{os.path.basename(path)}: {m.group(0)}"


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
