"""Invariants for executing a Config-Management run on a remote agent.

Five separate things have to hold, and each has cost somebody a bad afternoon in one of the
adjacent features already:

  * **The inventory is authored by the AGENT.** A dashboard-supplied inventory or an
    ``ansible_*`` extra var can set ``ansible_connection: local``, which turns "configure
    that VM over SSH" into "run this playbook inside the runner container" — on a network the
    dashboard has no other route to. ``-e`` outranks every inventory variable in Ansible, so
    filtering only the inventory would be worth nothing.
  * **The two mirrored implementations agree.** ``bundle_ref`` and the run argv exist twice
    because the agent cannot import from ``web_dashboard``. A divergence in the first reads
    as "the sealed bundle did not authenticate" (an operator goes and looks at keys); in the
    second it silently changes which value wins a name conflict.
  * **The frame walker survives arbitrary chunk boundaries.** The predecessor bug re-encoded
    a decoded log stream and corrupted Docker's big-endian int32 frame headers for half of
    all payload sizes; a streaming reader adds partial frames on top of that.
  * **No HostConfig field comes from the job**, the same property the hypervisor sibling
    keeps — extended to the harder case where the job DOES choose something (the image).
  * **Policy fails closed**, and the Config-Management grant is separate from the discovery
    one: "may be port-scanned" is not "may have a playbook applied as root".

Standalone or under pytest:
    python tests/test_agent_ansible_run.py
"""
import importlib.util
import io as _io
import json
import os
import sys
import tarfile
import tempfile
import textwrap
from urllib.parse import unquote

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")


def _load(name, *parts):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent = _load("agent", "runners", "agent", "agent.py")
sealing = _load("agent_sealing", "web_dashboard", "services", "agent_sealing.py")
vmcmd = _load("ansible_vm_cmd", "web_dashboard", "services", "ansible_vm_cmd.py")


def _policy(doc: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(textwrap.dedent(doc))
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


_FULL = """
targets:
  - cidr: 10.20.0.0/24
    ports: [443, 8006]
job_types: [agent_discover, agent_ansible]
ansible:
  enabled: true
  vm_image: chrweav/ansible-winrm:latest
  db_image: chrweav/ansible-cloud:latest
  targets:
    - cidr: 10.20.10.0/24
      ports: [22, 5985, 5986]
"""


def _frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


# ── the inventory is the agent's, and ansible_* is refused ────────────────────

def test_the_agent_refuses_an_ansible_connection_extra_var():
    """The whole reason the bundle carries typed fields instead of an inventory."""
    for name in ("ansible_connection", "ansible_python_interpreter",
                 "ansible_ssh_executable", "ANSIBLE_CONNECTION"):
        try:
            agent._check_extra_vars({name: "local"})
            raise AssertionError(f"{name} was accepted")
        except agent.PolicyRefusal as exc:
            assert "connection configuration" in str(exc).lower(), str(exc)


def test_ordinary_extra_vars_pass():
    agent._check_extra_vars({"role": "web", "db_login_host": "x", "count": 3})


def test_the_refusal_names_every_offending_var_at_once():
    try:
        agent._check_extra_vars({"ansible_connection": "local", "ansible_user": "root"})
        raise AssertionError("accepted")
    except agent.PolicyRefusal as exc:
        assert "ansible_connection" in str(exc) and "ansible_user" in str(exc)


def test_the_inventory_pins_the_resolved_ip_not_the_requested_name():
    """check_ansible validates the RESOLVED address to defeat DNS rebinding, but the connect
    happens later in another namespace — so the inventory has to carry the address, or that
    check means nothing."""
    inv = json.loads(agent._vm_inventory(ip="10.20.10.5", port=22, transport="ssh",
                                         login_user="root", winrm={}))
    host = inv["all"]["hosts"]["target"]
    assert host["ansible_host"] == "10.20.10.5"
    assert host["ansible_connection"] == "ssh"
    assert host["ansible_user"] == "root"


def test_a_winrm_inventory_derives_its_scheme_from_the_port():
    """One field, not two that must agree: 5986 is WinRM over TLS, 5985 is plain."""
    plain = json.loads(agent._vm_inventory(ip="1.2.3.4", port=5985, transport="winrm",
                                           login_user="a", winrm={}))
    tls = json.loads(agent._vm_inventory(ip="1.2.3.4", port=5986, transport="winrm",
                                         login_user="a", winrm={"scheme": "https"}))
    assert plain["all"]["hosts"]["target"]["ansible_winrm_scheme"] == "http"
    assert tls["all"]["hosts"]["target"]["ansible_winrm_scheme"] == "https"
    assert plain["all"]["hosts"]["target"]["ansible_connection"] == "winrm"


def test_the_inventory_is_serialized_not_assembled_as_text():
    """A name containing a quote or a newline must not be able to inject structure."""
    inv = agent._vm_inventory(ip='1.2.3.4"\n  evil: yes', port=22, transport="ssh",
                              login_user="", winrm={})
    parsed = json.loads(inv)                       # valid JSON, single host, no extra keys
    assert list(parsed["all"]["hosts"]) == ["target"]


# ── the two mirrors ───────────────────────────────────────────────────────────

def test_bundle_ref_is_byte_identical_on_both_sides():
    for kw in ({"run_kind": "vm", "transport": "ssh", "host": "10.20.10.5", "port": 22},
               {"run_kind": "vm", "transport": "winrm", "host": "HOST.Lab.Local.",
                "port": 5986},
               {"run_kind": "database", "transport": "local", "host": "db.lan",
                "port": "1433"}):
        assert agent.bundle_ref(**kw) == sealing.bundle_ref(**kw), kw


def test_seal_host_key_is_byte_identical_on_both_sides():
    for host in ("Vc.Lab.Local.", "  x  ", "", "10.0.0.1", "HOST"):
        assert agent.seal_host_key(host) == sealing.seal_host_key(host), host


def test_the_run_argv_is_byte_identical_on_both_sides():
    """The ORDER matters, not just the flags: `--extra-vars @file` after the inline one is
    what makes a resolved secret win a name conflict."""
    for has_key in (False, True):
        for has_vars in (False, True):
            mine = agent._ansible_argv(run_kind="vm", transport="ssh",
                                       has_key=has_key, has_vars=has_vars)
            theirs = vmcmd.build_vm_argv(
                job_dir=agent._JOB_DIR, inventory=f"{agent._JOB_DIR}/inventory.json",
                private_key=has_key, secret_vars_file=has_vars)
            assert mine == theirs, (has_key, has_vars, mine, theirs)


def test_a_database_run_is_a_localhost_play():
    argv = agent._ansible_argv(run_kind="database", transport="local",
                              has_key=False, has_vars=True)
    assert "-c" in argv and "local" in argv and "localhost," in argv
    assert "--private-key" not in argv


def test_the_files_land_outside_the_tmpfs():
    """The trap: the sibling mounts a tmpfs over /tmp at START, which shadows anything put
    there beforehand and deletes it with no diagnostic at all."""
    assert not agent._JOB_DIR.startswith("/tmp")
    assert "/tmp" in agent._ANSIBLE_HOSTCONFIG["Tmpfs"]


def test_the_job_dir_matches_the_dashboards():
    """Half of a contract with the dashboard: it names the paths inside the tar, the agent
    reads them back. A mismatch is a run that cannot find its own playbook."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "agent_ansible_bundle.py"), encoding="utf-8").read()
    assert f'JOB_DIR = "{agent._JOB_DIR}"' in src, (
        "the dashboard and the agent disagree about where the run's files live")


# ── framing ───────────────────────────────────────────────────────────────────

def test_frames_are_walked_only_when_complete():
    buf = bytearray(_frame(1, b"hello\n")[:5])
    assert agent._demux_frames(buf) == [], "a partial header was consumed"
    assert len(buf) == 5, "a partial header was dropped rather than kept"


def test_a_partial_payload_is_kept_for_the_next_read():
    whole = _frame(1, b"abcdefgh")
    buf = bytearray(whole[:12])                 # header + 4 of 8 payload bytes
    assert agent._demux_frames(buf) == []
    buf += whole[12:]
    assert agent._demux_frames(buf) == [(1, b"abcdefgh")]
    assert len(buf) == 0


def test_the_stream_survives_arbitrary_chunk_boundaries():
    """Byte-at-a-time is the worst case, and a length byte >= 0x80 is the regression the
    predecessor bug lived in."""
    big = b"x" * 0x180
    raw = _frame(1, b"one\n") + _frame(2, b"warn\n") + _frame(1, big)
    buf, got = bytearray(), []
    for i in range(len(raw)):
        buf += raw[i:i + 1]
        got += agent._demux_frames(buf)
    assert len(buf) == 0
    assert b"".join(p for _s, p in got) == b"one\nwarn\n" + big


def test_stdout_and_stderr_are_distinguishable():
    """One shared buffer splices a stderr warning into the middle of a stdout line."""
    buf = bytearray(_frame(1, b"out") + _frame(2, b"err"))
    assert agent._demux_frames(buf) == [(1, b"out"), (2, b"err")]


def test_demux_still_reads_a_whole_body_for_the_hypervisor_path():
    """_demux is the non-follow reader and must keep working — the hypervisor sibling's
    single JSON result goes through it."""
    raw = _frame(1, b'{"ok": true}')
    assert agent._demux(raw) == '{"ok": true}'


def test_demux_falls_back_to_text_when_there_is_no_frame_at_all():
    """An unframed error string is more useful than an empty one."""
    assert "boom" in agent._demux(b"boom")


# ── the container spec ────────────────────────────────────────────────────────

def test_no_hostconfig_field_is_derived_from_a_job():
    """The same property the hypervisor sibling keeps. `run_kind` selects the IMAGE, from
    policy, and nothing else — so the spec is byte-identical for both kinds and this needs
    no softening."""
    cfg = agent._ANSIBLE_HOSTCONFIG
    assert cfg["Binds"] == [], "a bind mount would be a path the job could influence"
    assert all(not m.get("Source") for m in cfg.get("Mounts") or []), (
        "a mount Source is a host path, which is the thing Binds: [] exists to prevent")
    assert cfg["Privileged"] is False
    assert cfg["CapDrop"] == ["ALL"]
    assert cfg["ReadonlyRootfs"] is True
    assert "no-new-privileges:true" in cfg["SecurityOpt"]
    assert cfg["AutoRemove"] is False, "the log stream and /wait both outlive the container"
    assert cfg["PidsLimit"] and cfg["Memory"]
    assert cfg["MemorySwap"] == cfg["Memory"], "swap must not defeat the memory limit"
    assert "NetworkMode" not in cfg, "the network comes from policy, per run"


def _capture_put(files, dest=None):
    """Run the real ``_archive_put`` with only the socket replaced, and hand back what it
    would have sent: (destination, tar member names, {name: TarInfo})."""
    sent = {}

    class _Conn:
        def __init__(self, path, timeout=60.0):
            pass

        def request(self, method, url, body=None, headers=None):
            sent["url"], sent["body"] = url, body

        def getresponse(self):
            class _R:
                status = 204

                def read(self):
                    return b""
            return _R()

        def close(self):
            pass

    orig = agent._UnixHTTP
    agent._UnixHTTP = _Conn
    try:
        agent._archive_put("cid", dest or agent._JOB_DIR, files)
    finally:
        agent._UnixHTTP = orig

    with tarfile.open(fileobj=_io.BytesIO(sent["body"])) as tar:
        members = {m.name: m for m in tar.getmembers()}
    return unquote(sent["url"].split("path=", 1)[1]), list(members), members


def test_the_job_dir_is_a_mount_or_the_daemon_refuses_every_run():
    """The one that killed the feature on every host at once.

    `PUT /containers/<id>/archive` is answered 400 "container rootfs is marked read-only"
    whenever HostConfig sets ReadonlyRootfs and the destination does not resolve into a
    mount. The daemon reads that off HostConfig, so extracting BEFORE the container starts
    — which is what the code does, and why this looked safe — makes no difference at all.
    """
    cfg = agent._ANSIBLE_HOSTCONFIG
    assert cfg["ReadonlyRootfs"] is True
    targets = [m.get("Target") for m in cfg.get("Mounts") or []]
    assert agent._JOB_DIR in targets, (
        f"{agent._JOB_DIR} is not a mount, so the Engine will refuse the archive PUT and "
        f"no Config-Management run can start: {targets}")


def test_the_job_dir_mount_is_a_volume_and_never_a_tmpfs():
    """A tmpfs here ACCEPTS the PUT and then loses the files: it is mounted empty when the
    container starts, over the top of what was just extracted. The run then fails as a
    missing playbook, pointing at the dashboard instead of at this line."""
    for m in agent._ANSIBLE_HOSTCONFIG.get("Mounts") or []:
        if m.get("Target") == agent._JOB_DIR:
            assert m.get("Type") == "volume", m


def test_the_job_dir_mount_names_no_host_path():
    """Anonymous, so the sibling's `Binds: []` property — nothing of this host is reachable
    from a run — survives the mount that had to be added."""
    for m in agent._ANSIBLE_HOSTCONFIG.get("Mounts") or []:
        assert not m.get("Source"), m


def test_the_extract_targets_the_job_dir_with_relative_members():
    """Two halves of one contract: ``path=`` must be the mount (extracting into ``/`` is
    refused even with the mount present, because ``/`` is not in it), and the members must
    then be named relative to it or they land in /opt/job/opt/job/."""
    dest, names, members = _capture_put({
        f"{agent._JOB_DIR}/playbook.yml": b"- hosts: all",
        f"{agent._JOB_DIR}/assets/site.tar": b"asset",
        f"{agent._JOB_DIR}/id_rsa": b"key",
    })
    assert dest == agent._JOB_DIR, dest
    assert "playbook.yml" in names and "assets/site.tar" in names, names
    assert not any(n.startswith("opt") for n in names), names
    assert members["assets"].isdir() and members["assets"].mode == 0o700
    assert members["id_rsa"].mode == 0o600, "the private key must never be group-readable"


def test_the_keys_may_still_be_written_as_absolute_paths():
    """The files dict stays keyed by where each file ENDS UP, which is what the argv and the
    dashboard both talk in. Stripping the prefix is this function's job, not the caller's."""
    dest, names, _ = _capture_put({"/opt/job/playbook.yml": b"x",
                                   "opt/job/inventory.json": b"{}"})
    assert sorted(names) == ["inventory.json", "playbook.yml"], names


def test_a_file_outside_the_job_dir_is_refused():
    """Everything is extracted relative to the mount, so a path outside it would silently
    land somewhere else entirely — and the one path assembled from anything the dashboard
    sends is the asset name."""
    for path in ("/etc/cron.d/x", "opt/job-other/x", "/opt/job"):
        try:
            _capture_put({path: b"x"})
            raise AssertionError(f"{path} was accepted")
        except agent.PolicyRefusal:
            pass


def test_the_log_driver_is_pinned_to_a_readable_one():
    """Without this, GET /logs answers 501 on any host whose default driver is journald —
    RHEL, Fedora, Rocky, Alma — and the whole feature is dead there, reported as "the runner
    produced no output"."""
    assert agent._ANSIBLE_HOSTCONFIG["LogConfig"]["Type"] == "json-file"


def test_ansible_writes_are_relocated_off_the_read_only_root():
    """Both runner images run as root with HOME=/root, so ansible's default
    ~/.ansible/tmp is on the read-only filesystem: [Errno 30] before any task runs."""
    env = agent._ANSIBLE_ENV
    for key in ("ANSIBLE_HOME", "ANSIBLE_LOCAL_TEMP", "ANSIBLE_SSH_CONTROL_PATH_DIR"):
        assert env[key].startswith("/tmp/"), f"{key} is not on the writable tmpfs"
    assert env["PYTHONUNBUFFERED"] == "1", "buffered stdout makes live output look hung"


def test_the_tmpfs_is_big_enough_to_be_plausible():
    """16 MB fits a playbook and a key but not ansible's assembled module payloads."""
    size = agent._ANSIBLE_HOSTCONFIG["Tmpfs"]["/tmp"]
    assert "size=128m" in size, size


# ── policy: fails closed, and separate from discovery ─────────────────────────

def test_config_management_is_off_unless_the_policy_enables_it():
    pol = _policy("targets:\n  - cidr: 10.20.0.0/24\n")
    assert pol.ansible_enabled is False
    for call in (lambda: pol.ansible_image("vm"),
                 lambda: pol.check_ansible("10.20.10.5", 22)):
        try:
            call()
            raise AssertionError("a policy with no ansible block allowed a run")
        except agent.PolicyRefusal:
            pass


def test_the_discovery_target_list_does_not_grant_config_management():
    """The separation is the point: widening a scan range must not authorise root on that
    subnet."""
    pol = _policy(_FULL)
    pol.check_ansible("10.20.10.5", 22)                 # in ansible.targets
    try:
        pol.check_ansible("10.20.0.5", 443)             # in targets, NOT ansible.targets
        raise AssertionError("the discovery list granted Config Management")
    except agent.PolicyRefusal as exc:
        assert "ansible.targets" in str(exc)


def test_a_port_outside_the_ansible_list_is_refused():
    pol = _policy(_FULL)
    try:
        pol.check_ansible("10.20.10.5", 3389)
        raise AssertionError("an unlisted port was allowed")
    except agent.PolicyRefusal as exc:
        assert "3389" in str(exc)


def test_loopback_and_metadata_stay_denied_for_config_management():
    pol = _policy("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: x
          targets:
            - cidr: 0.0.0.0/0
        """)
    for ip in ("127.0.0.1", "169.254.169.254"):
        try:
            pol.check_ansible(ip, 22)
            raise AssertionError(f"{ip} was allowed by a wide ansible.targets")
        except agent.PolicyRefusal as exc:
            assert "denied range" in str(exc)


def test_the_image_comes_from_policy_and_is_named_per_kind():
    pol = _policy(_FULL)
    assert pol.ansible_image("vm") == "chrweav/ansible-winrm:latest"
    assert pol.ansible_image("database") == "chrweav/ansible-cloud:latest"


def test_a_missing_image_for_a_kind_says_which_key_to_add():
    pol = _policy("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: chrweav/ansible-winrm:latest
          targets:
            - cidr: 10.20.10.0/24
        """)
    try:
        pol.ansible_image("database")
        raise AssertionError("a database run was allowed with no db_image")
    except agent.PolicyRefusal as exc:
        assert "ansible.db_image" in str(exc)


def test_an_unknown_run_kind_is_refused_by_name():
    pol = _policy(_FULL)
    try:
        pol.ansible_image("shell")
        raise AssertionError("an unknown run kind resolved to an image")
    except agent.PolicyRefusal as exc:
        assert "shell" in str(exc)


def test_the_two_target_lists_are_parsed_by_one_function():
    """Two parsers is how one of them would stop pinning resolved addresses."""
    src = open(_AGENT_PATH, encoding="utf-8").read()
    assert src.count("def _parse_targets") == 1
    assert '_parse_targets(doc.get("targets")' in src
    assert '_parse_targets(ansible.get("targets")' in src


def test_the_handler_is_registered_and_the_table_stays_closed():
    assert agent.HANDLERS["agent_ansible"] is agent.run_ansible
    assert set(agent.HANDLERS) == {"agent_discover", "agent_hypervisor", "agent_ansible",
                                   "agent_gateway"}


def test_the_version_reports_the_ansible_capable_build():
    """The dashboard refuses to QUEUE below this, so the two have to agree."""
    from_dashboard = open(os.path.join(_ROOT, "web_dashboard", "services",
                                       "agent_service.py"), encoding="utf-8").read()
    assert "MIN_ANSIBLE_VERSION = (2, 3)" in from_dashboard
    major, minor = agent.AGENT_VERSION.split(".")[:2]
    assert (int(major), int(minor)) >= (2, 3), agent.AGENT_VERSION


# ── quoting ───────────────────────────────────────────────────────────────────

def test_the_shell_quoter_handles_the_values_it_will_actually_see():
    import shlex
    for value in ("simple", "with space", "it's", "a\nb", "$(id)", "`id`", "",
                  "/opt/job/playbook.yml", "-o StrictHostKeyChecking=no"):
        assert agent._shell_quote(value) == shlex.quote(value), value


def test_the_command_execs_so_a_cancel_reaches_ansible():
    """Without exec, `sh` is PID 1 and does not forward SIGTERM while waiting — the reason
    `docker stop` on `sh -c` always burns the full grace period."""
    src = open(_AGENT_PATH, encoding="utf-8").read()
    assert 'command = "exec " + ' in src


def test_the_container_spec_passes_cmd_as_a_list_and_clears_the_entrypoint():
    """Two ways to get this wrong, both silent until a real run:

    The Engine API's ``Cmd`` is an array of strings — a bare string is taken as argv[0],
    not shell-parsed, so the container fails to start with the whole command as an
    executable name. And ``chrweav/ansible-winrm`` is built on an upstream image with its
    own ENTRYPOINT, which would be prepended to whatever Cmd says.
    """
    src = open(_AGENT_PATH, encoding="utf-8").read()
    assert '"Cmd": ["/bin/sh", "-c", command]' in src, (
        "Cmd must be a list — the Engine API does not shell-parse a string")
    assert '"Entrypoint": []' in src, (
        "the base image's ENTRYPOINT must be cleared or it is prepended to Cmd")


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
