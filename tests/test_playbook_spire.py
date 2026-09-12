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


def test_the_version_guard_reads_stderr():
    """`spire-server --version` writes to STDERR. Guarding the download on stdout alone
    left the condition permanently true, so every run re-downloaded and re-unpacked the
    release — and a version bump would have overwritten in place while looking guarded.
    Caught by running the play twice against a real host, not by any static check."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-server-install.yml"), encoding="utf-8").read())[0]
    guard = None
    for task in _tasks(play):
        facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
        if "_installed" in facts:
            guard = str(facts["_installed"])
            break
    assert guard, "no task computes the installed-version fact"
    assert "stderr" in guard, (
        "the installed-version fact ignores stderr, which is where spire-server "
        "actually prints its version")

    gated = [t for t in _tasks(play)
             if "_installed" in yaml.safe_dump(t.get("when") or "")]
    assert len(gated) >= 2, (
        f"expected the download and the unpack to be gated on the installed version; "
        f"found {len(gated)} task(s)")


def test_the_conf_dir_is_not_fought_over_with_the_tarball():
    """conf/ ships 0755 in the release tarball, so unarchive resets it. Forcing 0750
    made the two flip-flop and the play report changed on every run forever."""
    play = yaml.safe_load(
        open(os.path.join(_SPIRE_DIR, "spire-server-install.yml"), encoding="utf-8").read())[0]
    for task in _tasks(play):
        if "directories" not in (task.get("name") or ""):
            continue
        modes = {i["path"].split("/")[-1]: i["mode"] for i in task["loop"]}
        assert modes.get("conf") == "0755", (
            f"conf/ is created {modes.get('conf')!r}; the tarball ships 0755 and will "
            f"reset anything stricter on every unpack")
        assert modes.get("{{ spire_data }}".split("/")[-1], "0750") == "0750"
        return
    raise AssertionError("no task creates the SPIRE directories")


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


# ── the Kubernetes track ─────────────────────────────────────────────────────
# Four traps, each recorded in docs/design/workload-k8s-short-lived-token.md. None of these
# plays has been run against a live pair, so these assertions are the only thing standing
# between a plausible-looking edit and a chain that cannot issue a single token.

def _play(name):
    return yaml.safe_load(open(os.path.join(_SPIRE_DIR, name), encoding="utf-8").read())[0]


def test_the_kubernetes_track_plays_exist():
    for name in ("spire-oidc-provider.yml", "spire-k8s-entry.yml", "spire-agent-install.yml"):
        assert os.path.exists(os.path.join(_SPIRE_DIR, name)), f"{name} is missing"


def test_the_workload_entry_stays_out_of_the_seed_play():
    """The 8-of-11 count is asserted above, stated in three documents, and has already
    caught a real bug. The Kubernetes workload entry therefore belongs in
    spire-k8s-entry.yml — putting it in the seed set would move the number and retire the
    assertion that caught that bug."""
    seeded = {e["path"] for e in _seed_entries()}
    entry_play = yaml.safe_dump(_play("spire-k8s-entry.yml"))
    assert "workload_path" in entry_play, \
        "spire-k8s-entry.yml no longer declares its own workload path"
    for path in seeded:
        assert "kube" not in path and "k8s" not in path, (
            f"seeded path {path!r} looks like the Kubernetes workload entry. It must live in "
            "spire-k8s-entry.yml — the seed play's entry count is load-bearing")


def test_the_node_entry_comes_from_the_join_token():
    """`token generate -spiffeID` creates the node entry itself, with the token's UUID as
    the selector. A hand-written `join_token:<name>` node entry — which is what
    spire-seed-entries.yml has, for a lab with no agent at all — matches no agent ever."""
    play = _play("spire-k8s-entry.yml")
    generates = [t for t in _tasks(play) if "token generate" in (_command_of(t) or "")]
    assert len(generates) == 1, "expected exactly one `token generate`"
    assert "-spiffeID" in _command_of(generates[0]), (
        "`token generate` must pass -spiffeID, or it creates no node entry and the "
        "workload entry is parented to nothing")
    for task in _tasks(play):
        cmd = _command_of(task) or ""
        if "entry create" in cmd and "-node" in cmd:
            raise AssertionError(
                f"{task.get('name')!r} hand-writes a -node entry. The join token creates it; "
                "a made-up join_token:<name> selector matches no attesting agent")


def test_the_agent_can_actually_resolve_a_unix_selector():
    """WorkloadAttestor "unix" is what lets the agent learn a caller's UID. Without it every
    unix:uid selector matches nothing and the fetch fails with "no identity issued", which
    reads like a missing entry on the server rather than a missing plugin on the node."""
    play = _play("spire-agent-install.yml")
    conf = next((t for t in _tasks(play)
                 if "agent.conf" in str((t.get("ansible.builtin.copy") or {}).get("dest", ""))), None)
    assert conf, "spire-agent-install.yml writes no agent.conf"
    content = conf["ansible.builtin.copy"]["content"]
    assert 'WorkloadAttestor "unix"' in content, \
        'agent.conf has no WorkloadAttestor "unix" — unix:uid selectors cannot resolve'
    assert 'NodeAttestor "join_token"' in content, "agent.conf cannot attest with its token"
    assert conf.get("no_log") is True, "agent.conf holds the join token and must no_log"


def test_the_agent_socket_matches_the_exec_plugin_default():
    """spiffe/k8s-spiffe-workload-jwt-exec-auth defaults to
    unix:///tmp/spire-agent/public/api.sock. Matching it is what lets the kubeconfig carry
    no environment variable for the socket."""
    play = _play("spire-agent-install.yml")
    assert play["vars"]["agent_socket"] == "/tmp/spire-agent/public/api.sock", (
        "the agent socket no longer matches the exec plugin's default — either restore it "
        "or set SPIFFE_ENDPOINT_SOCKET in the kubeconfig k3s-spiffe-auth.yml writes")


def test_the_agent_unit_does_not_get_a_private_tmp():
    """The Workload API socket lives under /tmp. With PrivateTmp the agent gets its own,
    starts cleanly, passes its own health check, and no workload can ever reach it."""
    play = _play("spire-agent-install.yml")
    for task in _tasks(play):
        content = str((task.get("ansible.builtin.copy") or {}).get("content", ""))
        if "[Service]" not in content:
            continue
        assert "PrivateTmp=true" not in content.replace(" ", ""), (
            "the agent unit sets PrivateTmp. The socket under /tmp would be invisible to "
            "every workload on the host, and nothing would report an error")


def test_the_agent_proves_the_chain_as_the_workload_not_as_root():
    """The assertion that makes the other plays meaningful. Fetching as root is attested as
    unix:uid:0, matches no entry and fails; fetching as the workload proves the entry, the
    selector, the audience and the attestation together."""
    play = _play("spire-agent-install.yml")
    fetch = [t for t in _tasks(play) if "api fetch jwt" in (_command_of(t) or "")]
    assert len(fetch) == 1, "spire-agent-install.yml must fetch a JWT-SVID to prove the chain"
    task = fetch[0]
    assert task.get("become_user"), (
        "the fetch runs as root, so it is attested as unix:uid:0 and matches no entry. It "
        "has to run as the workload account")
    assert "-audience" in _command_of(task), "the fetch must name an audience"
    assert task.get("no_log") is True, "the fetched token authenticates to the API server"


def test_the_oidc_provider_refuses_an_unreachable_domain():
    """oidc_domain is the address the KUBERNETES API SERVER fetches JWKS from, and it lands
    in both the certificate SAN and the issuer string. localhost passes every check on the
    SPIRE host and then fails from the cluster, after a play that reported success."""
    play = _play("spire-oidc-provider.yml")
    guard = yaml.safe_dump([t for t in _tasks(play) if "assert" in yaml.safe_dump(t)])
    assert "localhost" in guard, "nothing stops oidc_domain being localhost"
    assert "oidc_domain" in guard, "oidc_domain is not asserted at all"


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
