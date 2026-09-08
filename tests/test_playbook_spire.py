"""Invariants for the SPIRE lab playbooks in examples/playbooks/spire/.

These plays drive the `spire-server` and `openssl` CLIs rather than any collection,
because the VM runner image ships none for SPIFFE. Shelling out means Ansible gives us
no idempotency for free, so these assertions pin what we hand-rolled:

  * every spire-server/openssl command declares `changed_when` (or `creates`);
  * read-only probes use `changed_when: false`;
  * the plays match the linux/ contract (`hosts: all`, `become: true`);
  * the administrative PKCS#12 and its passphrase never reach job output.

Two assertions here exist because the thing they pin already went wrong once.

`test_no_seeded_path_uses_characters_spire_rejects` — SPIRE restricts path segments to
letters, numbers, dots, dashes and underscores. The plugin repo's seed script carried a
path with `~` and `!`, sent stderr to /dev/null, and so seeded one entry fewer than it
claimed for months. The documented "11 entries, 8 discovered" was right only by
coincidence, and the account-name codec's `!HH` branch is unreachable in practice.

`test_seed_population_matches_the_documented_counts` — discovery returning 8 of 11 IS
the assertion the lab exists to make. The plugin shipped with discovery defaulting its
path filter to the mintable prefix, which silently narrowed the inventory to 2 accounts
while every run still reported success. If someone edits the seed set, the number in
the README and in the plugin's scenario has to move with it.

The beyondtrust-module invariants (delegate_to / no_log) live in
test_playbook_ps_lookup.py — they apply to every playbook, not just these.

Run: python tests/test_playbook_spire.py   (or under pytest)
"""
import glob
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPIRE_DIR = os.path.join(_ROOT, "examples", "playbooks", "spire")
_PLAYBOOKS = sorted(glob.glob(os.path.join(_SPIRE_DIR, "*.yml")))

_CMD_KEYS = ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell")

# Variables that hold the administrative credential or its passphrase. A task that
# ASSIGNS one of these (register/set_fact) must no_log; referencing one inside a
# `when:` is fine, because Ansible never prints a condition's value.
_SECRET_VARS = ("pfx_pass", "pfx_raw", "_ps_existing_pfx")

# SPIRE's own path grammar. Anything outside this in a segment is refused by the server.
_LEGAL_PATH = re.compile(r"^(/[A-Za-z0-9._-]+)+$")


def _rel(path):
    return os.path.relpath(path, _ROOT)


def _plays():
    for path in _PLAYBOOKS:
        for play in (yaml.safe_load(open(path, encoding="utf-8").read()) or []):
            yield path, play


def _tasks(play):
    return play.get("tasks") or []


def _command_of(task):
    """The command string for a command/shell task, else None."""
    for key in _CMD_KEYS:
        if key in task:
            val = task[key]
            if isinstance(val, dict):          # cmd:/argv: form
                return str(val.get("cmd") or val.get("argv") or "")
            return str(val)
    return None


def _seed_entries():
    """The workload entries seeded by spire-seed-entries.yml."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-seed-entries.yml"), encoding="utf-8").read())[0]
    for task in _tasks(play):
        entries = (task.get("vars") or {}).get("_entries")
        if entries:
            return entries
    raise AssertionError("spire-seed-entries.yml declares no _entries list")


def test_spire_playbooks_exist():
    assert _PLAYBOOKS, "no playbooks found in examples/playbooks/spire/"


def test_plays_match_the_linux_contract():
    """These configure hosts over SSH, like examples/playbooks/linux/."""
    for path, play in _plays():
        assert play.get("hosts") == "all", f"{_rel(path)}: hosts is not 'all'"
        assert play.get("become") is True, f"{_rel(path)}: become is not true"


def test_every_command_declares_changed_when():
    """Shelling out gives no change detection, so each command must say what a change
    means — or the play reports changed on every single run."""
    interesting = ("spire-server", "openssl", "systemctl", "ufw")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(i in cmd for i in interesting):
                continue
            if "changed_when" not in task and "creates" not in task:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "command without changed_when/creates:\n  " + "\n  ".join(offenders)


def test_probes_are_marked_unchanged():
    """State probes read, they don't mutate."""
    readonly = ("systemctl is-active", "spire-server healthcheck", "spire-server --version",
                "spire-server entry count", "spire-server bundle show", "openssl version",
                "openssl x509", "ufw status")
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            cmd = _command_of(task)
            if not cmd or not any(r in cmd for r in readonly):
                continue
            if task.get("changed_when") is not False:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "read-only probe not marked changed_when: false:\n  " + "\n  ".join(offenders)


def test_admin_credential_never_reaches_job_output():
    """The PKCS#12 and its passphrase are the whole reason this playbook is delicate.
    There is no output-as-value channel in the runner — a job's output IS a captured
    log — so any task that assigns one of them must no_log."""
    path = os.path.join(_SPIRE_DIR, "spire-admin-identity.yml")
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    checked = 0
    for task in _tasks(play):
        assigned = {task.get("register")} | set(
            (task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}))
        secret = assigned & set(_SECRET_VARS)
        if not secret:
            continue
        checked += 1
        assert task.get("no_log") is True, (
            f"task {task.get('name')!r} assigns {sorted(secret)} without no_log")
    assert checked >= 3, (
        f"expected the mint play to assign the credential, the passphrase and the "
        f"existing-credential lookup under no_log; found {checked}")


def test_nothing_prints_the_admin_credential():
    """A debug that interpolates the credential would defeat every no_log above."""
    offenders = []
    for path, play in _plays():
        for task in _tasks(play):
            printed = yaml.safe_dump(task.get("ansible.builtin.debug")
                                     or task.get("debug") or {})
            for var in _SECRET_VARS:
                if re.search(rf"\b{re.escape(var)}\b", printed):
                    offenders.append(f"{_rel(path)}: {task.get('name')!r} prints {var}")
    assert not offenders, "credential reaches job output:\n  " + "\n  ".join(offenders)


def test_the_passphrase_is_generated_on_the_host():
    """Supplying it through the run form would put it in the job record and in the
    operator's shell history. It is generated on the VM and only ever moves to
    Password Safe."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-admin-identity.yml"), encoding="utf-8").read())[0]
    assert "pfx_pass" not in (play.get("vars") or {}), (
        "the PKCS#12 passphrase must not be a play var — it is generated on the host")
    for task in _tasks(play):
        cmd = _command_of(task) or ""
        if "openssl rand" in cmd:
            assert task.get("register") == "pfx_pass"
            return
    raise AssertionError("no task generates the passphrase with `openssl rand`")


def test_server_config_writes_admin_ids():
    """An entry-level `-admin` does NOT grant admin rights to a minted SVID; only
    admin_ids does. Omitting it surfaces as PERMISSION_DENIED on Verify Functional
    Account, a long way from the cause."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-server-install.yml"), encoding="utf-8").read())[0]
    for task in _tasks(play):
        body = (task.get("ansible.builtin.copy") or task.get("copy") or {})
        content = str(body.get("content") or "")
        if "server {" in content:
            assert "admin_ids" in content, "server.conf is written without admin_ids"
            assert "_admin_id" in content, "admin_ids is not the resolved admin SPIFFE ID"
            return
    raise AssertionError("no task writes server.conf")


def test_no_seeded_path_uses_characters_spire_rejects():
    """SPIRE restricts path segments to letters, numbers, dots, dashes, underscores.
    An illegal path is refused by the server, and the entry simply never exists."""
    for entry in _seed_entries():
        path = entry["path"]
        assert _LEGAL_PATH.match(path), (
            f"SPIRE will refuse the seeded path {path!r}: segments are limited to "
            f"letters, numbers, dots, dashes and underscores")


def test_seed_population_matches_the_documented_counts():
    """11 entries in, 8 discovered — one node/agent plus two privileged excluded.
    The README, docs/spiffe.md and the plugin's lab-scenario all state this number."""
    entries = _seed_entries()
    privileged = [e for e in entries
                  if "-admin" in (e.get("extra") or "") or "-downstream" in (e.get("extra") or "")]
    total = len(entries) + 1                       # + the parent node entry
    discoverable = len(entries) - len(privileged)  # the node entry is excluded too

    assert total == 11, f"expected 11 registration entries in total, found {total}"
    assert len(privileged) == 2, (
        f"expected one -admin and one -downstream entry, found {len(privileged)}")
    assert discoverable == 8, f"expected discovery to return 8 accounts, computed {discoverable}"


def test_seed_is_idempotent_on_an_existing_entry():
    """`entry create` fails on a duplicate. Re-running the seed must not fail the job,
    and must not report changed either."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-seed-entries.yml"), encoding="utf-8").read())[0]
    creates = 0
    for task in _tasks(play):
        cmd = _command_of(task) or ""
        if "entry create" not in cmd:
            continue
        creates += 1
        for key in ("changed_when", "failed_when"):
            assert "already exists" in yaml.safe_dump(task.get(key)), (
                f"{task.get('name')!r}: {key} does not tolerate an existing entry")
    assert creates >= 2, "expected the node entry and the workload entries to be created"


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"ok   {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e}")
    sys.exit(1 if _failures else 0)
