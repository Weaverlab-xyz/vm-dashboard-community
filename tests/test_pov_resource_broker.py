"""The POV's Password Safe Resource Broker — the refusals, and the parsing.

Slice 5b. Its shape is decided by facts about BeyondTrust rather than about this repo, so
what is pinned here is mostly the consequences of those facts:

  * **A silent install needs ZONE as well as INSTALLKEY.** Without the zone the installer
    prompts, and an unattended prompt is not an error — it is a run that hangs until its
    timeout with an install log ending mid-dialog. Refused at preflight instead.
  * **RESTART is never set.** During a silent install it reboots the machine
    automatically, which drops the WinRM session mid-play and reports the failure of a
    step that had in fact succeeded.
  * **The stored credential is free text**, so it is parsed — and the parser refuses
    rather than guesses, because a wrong username comes back as a WinRM auth failure,
    which reads as a bad password.
  * **The installer key is never in the job row**, only the NAME of the var it binds to.
  * **The Config-Management grant is scoped to Windows guests**, not the POV's whole VM
    list: widening a port probe must never widen what may have a playbook applied as root.

Uses a real SQLite database. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_resource_broker.py
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-rb")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, ansible_local_service,  # noqa: E402
                                    pov_broker, pov_credentials, pov_env_service,
                                    pov_resource_broker as rb, storage_service)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _agent(db, *, version="2.4.0",
           reported=("agent_discover", "agent_gateway", "agent_ansible")):
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "k"
    row.agent_version = version
    row.reported_job_types = json.dumps(list(reported))
    row.last_seen_at = datetime.utcnow()
    db.commit()
    return row


def _env(db, **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE, **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name="rb", os_family="windows", ip="10.9.0.20"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


def _ready(db):
    """A POV with everything the install needs, so a test can take one thing away."""
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    vm = _vm(db, env)
    rb.configure(db, env, zone_name="POV-ZONE", asset_key="Bootstrapper.exe")
    rb.set_installer_key(env, "ik-secret-123")
    return env, agent, vm


# ── the credential parser ────────────────────────────────────────────────────

def test_the_common_separator_forms_parse():
    for text, expected in (
            ("administrator / Passw0rd!", ("administrator", "Passw0rd!")),
            ("administrator:P@ss:word", ("administrator", "P@ss:word")),
            ("ACME\\svc-rb | s3cret", ("ACME\\svc-rb", "s3cret")),
            ("username: administrator password: Passw0rd",
             ("administrator", "Passw0rd")),
    ):
        assert pov_credentials.parse(text) == expected, text


def test_a_bare_space_is_not_a_separator():
    """'administrator Passw0rd' is indistinguishable from a sentence, and a password with
    a space in it would split in the wrong place — the quiet wrong answer this refuses."""
    try:
        pov_credentials.parse("administrator Passw0rd")
        raise AssertionError("a bare space was accepted as a separator")
    except pov_credentials.CredentialParseError:
        pass


def test_a_sentence_is_refused_rather_than_mined():
    try:
        pov_credentials.parse("Log in as administrator with the password Passw0rd")
        raise AssertionError("a sentence was parsed")
    except pov_credentials.CredentialParseError:
        pass


def test_a_multi_line_note_is_refused():
    try:
        pov_credentials.parse("some notes\nadministrator / Passw0rd")
        raise AssertionError("a multi-line blob was parsed")
    except pov_credentials.CredentialParseError as exc:
        assert "more than one line" in str(exc)


def test_the_refusal_never_quotes_the_text():
    """It contains the password by definition."""
    secret = "administrator hunter2-do-not-log"
    try:
        pov_credentials.parse(secret)
        raise AssertionError("it parsed")
    except pov_credentials.CredentialParseError as exc:
        assert "hunter2" not in str(exc)


def test_two_indistinguishable_credentials_are_refused_not_ordered():
    """Neither `a` nor `c` is a superuser and both are local, so the guest-OS rule cannot
    separate them — and *this* caller cannot ask. `platform_login` seals one credential
    into a run bundle the agent uses over WinRM, so it never authenticates and never learns
    the outcome; position in a list is the only thing left, and that is still a guess. The
    template build, which does authenticate, uses `candidates` instead.

    **Do not turn this into ordering by position.** Ordering by RULE is what
    `test_the_guest_os_superuser_wins` covers; that is a different thing."""
    try:
        pov_credentials.pick([{"text": "a / b"}, {"text": "c / d"}], vm_label="rb01")
        raise AssertionError("it picked one")
    except pov_credentials.CredentialParseError as exc:
        assert "equal standing" in str(exc) and "rb01" in str(exc)
        assert "a / b" not in str(exc), "the refusal quoted a credential"
        # The usernames ARE named. A parsed username is not a credential, and withholding
        # it left the reader with a count and no idea what to do about it.
        assert "a, c" in str(exc), str(exc)


def test_the_guest_os_superuser_wins():
    """The reported failure: a broker VM whose box holds the template's administrator
    beside a second login somebody left there. Several per host is the NORM, so this has
    to resolve rather than refuse."""
    entries = [{"text": "btadmin / Hunter2"}, {"text": "administrator / Passw0rd"}]
    assert pov_credentials.pick(entries, vm_label="BtPocBroker01",
                                os_family="windows") == ("administrator", "Passw0rd")
    linux = [{"text": "ec2-user / a"}, {"text": "root / b"}, {"text": "ubuntu / c"}]
    assert pov_credentials.pick(linux, os_family="linux") == ("root", "b")


def test_local_beats_domain_qualified_but_only_as_a_tiebreak():
    """A domain login depends on a domain controller having booted, which in a lab whose
    boot order is not guaranteed fails at WinRM as an authentication error — i.e. reads as
    a bad password. So local wins a tie. It does NOT win outright: privilege is what the
    install needs, so a domain-qualified administrator still beats a local service
    account."""
    domain = "CORP" + chr(92) + "administrator"
    assert pov_credentials.pick(
        [{"text": domain + " / a"}, {"text": "administrator / b"}],
        os_family="windows") == ("administrator", "b")
    assert pov_credentials.pick(
        [{"text": "svc_backup / a"}, {"text": domain + " / b"}],
        os_family="windows") == (domain, "b")
    # `.\` is Windows' own "this machine" prefix, so it counts as local.
    dot = "." + chr(92) + "administrator"
    assert pov_credentials.pick(
        [{"text": domain + " / a"}, {"text": dot + " / b"}],
        os_family="windows") == (dot, "b")


def test_a_blank_os_family_is_not_read_as_linux():
    """Slice 3's rule. A blank family widens the superuser set to both names rather than
    picking one, so a guest holding exactly one of them still resolves — and a guest
    holding BOTH is refused, because which family it is remains the unanswered question."""
    assert pov_credentials.pick(
        [{"text": "root / a"}, {"text": "btadmin / b"}], os_family="") == ("root", "a")
    try:
        pov_credentials.pick([{"text": "root / a"}, {"text": "administrator / b"}],
                             vm_label="unknown01", os_family="")
        raise AssertionError("it guessed the guest's operating system")
    except pov_credentials.CredentialParseError as exc:
        assert "equal standing" in str(exc)


def test_an_override_outranks_the_rule_and_a_typo_is_refused():
    """The remedy the ambiguity refusal now names. An override that quietly fell back to
    the rule would turn a typo into a SUCCESSFUL run against the wrong account, which is
    worse than a failure — so a name the platform does not hold is a refusal."""
    entries = [{"text": "administrator / a"}, {"text": "btadmin / b"}]
    assert pov_credentials.pick(entries, os_family="windows",
                                prefer="btadmin") == ("btadmin", "b")
    # Matched on the bare account name too, so an operator need not know the box holds a
    # qualified form.
    domain = "CORP" + chr(92) + "administrator"
    assert pov_credentials.pick([{"text": domain + " / a"}, {"text": "svc / b"}],
                                prefer="administrator") == (domain, "a")
    try:
        pov_credentials.pick(entries, vm_label="rb01", prefer="adminstrator")
        raise AssertionError("a typo fell back to the rule")
    except pov_credentials.CredentialParseError as exc:
        assert "adminstrator" in str(exc) and "administrator, btadmin" in str(exc)


def test_candidates_is_ranked_rather_than_platform_ordered():
    """The SSH runner install can retry, so ordering is a courtesy rather than a
    correctness rule — but the first attempt is the one that usually wins, and three failed
    authentications in a job log is three lines an SE has to read past."""
    entries = [{"text": "zed / 1"}, {"text": "root / 2"}, {"text": "sa@corp.local / 3"}]
    assert [u for u, _ in pov_credentials.candidates(
        entries, os_family="linux")] == ["root", "zed", "sa@corp.local"]


def test_the_caller_can_say_what_the_remedy_is():
    """A template build has no login field, so the default "set it on the POV by hand"
    sends the reader hunting for a control that does not exist on that page. Both refusals
    carry the caller's sentence, not just the ambiguous one."""
    for entries in ([{"text": "a / b"}, {"text": "c / d"}], [{"text": "not a login"}]):
        try:
            pov_credentials.pick(entries, vm_label="rb01",
                                 remedy="Fix it on the platform.")
            raise AssertionError("it did not refuse")
        except pov_credentials.CredentialParseError as exc:
            assert "Fix it on the platform." in str(exc), exc
            assert "on the POV by hand" not in str(exc), exc


def test_an_unparseable_entry_beside_a_good_one_is_treated_as_a_note():
    got = pov_credentials.pick(
        [{"text": "this VM is the RB host"}, {"text": "administrator / Passw0rd"}],
        vm_label="rb01")
    assert got == ("administrator", "Passw0rd")


# ── candidates(): the caller that can try ────────────────────────────────────

def test_candidates_keeps_platform_order_where_the_rule_cannot_separate():
    """The template build authenticates in process, so two logins are two things to try
    rather than an ambiguity to refuse. The sort in `candidates` is STABLE, so the
    platform's order survives wherever the rule has nothing to say — here both entries are
    a local superuser and the guest's family was not given, so neither outranks the other.
    Nothing depends on that order being right, only on it being tried."""
    got = pov_credentials.candidates(
        [{"text": "administrator / Passw0rd"}, {"text": "root:Hunter2"}], vm_label="rb01")
    assert got == [("administrator", "Passw0rd"), ("root", "Hunter2")], got


def test_candidates_ignores_an_unparseable_entry_beside_a_good_one():
    """Same rule as `pick`: a note in the box is not a credential and not a failure."""
    got = pov_credentials.candidates(
        [{"text": "this VM is the RB host"}, {"text": "administrator / Passw0rd"}],
        vm_label="rb01")
    assert got == [("administrator", "Passw0rd")], got


def test_candidates_refuses_in_the_same_words_when_none_are_usable():
    """`pick` and `candidates` share this refusal deliberately — the reader's next move is
    the same either way, and `docs/pov-instance.md` quotes the sentence in troubleshooting."""
    for fn in (pov_credentials.pick, pov_credentials.candidates):
        try:
            fn([{"text": "not a login"}], vm_label="rb01", remedy="Fix the box.")
            raise AssertionError(f"{fn.__name__} did not refuse")
        except pov_credentials.CredentialParseError as exc:
            assert "no stored credential this dashboard can use" in str(exc), exc
            assert "rb01" in str(exc) and "Fix the box." in str(exc), exc


def test_candidates_is_bounded():
    """Past a handful the box holds notes that happen to parse, and every extra entry is a
    real authentication attempt against a real guest."""
    over = pov_credentials.MAX_CANDIDATES + 3
    got = pov_credentials.candidates(
        [{"text": f"user{i} / pass{i}"} for i in range(over)], vm_label="rb01")
    assert len(got) == pov_credentials.MAX_CANDIDATES, got
    assert got[0] == ("user0", "pass0"), got


# ── the installer arguments ──────────────────────────────────────────────────

def test_the_arguments_carry_both_installkey_and_zone():
    """A silent install without ZONE prompts, and an unattended prompt hangs the run."""
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    args = rb.installer_arguments(env)
    assert "/quiet" in args
    assert f"INSTALLKEY={{{{ {rb.KEY_VAR} }}}}" in args
    assert "ZONE=POV-ZONE" in args
    db.close()


def test_restart_is_never_set():
    """During a silent install it reboots automatically, dropping the WinRM session
    mid-play — so Ansible reports the failure of a step that succeeded."""
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    assert "RESTART" not in rb.installer_arguments(env)
    db.close()


def test_the_key_is_a_var_name_in_the_arguments_never_the_value():
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    assert "ik-secret-123" not in rb.installer_arguments(env)
    db.close()


def test_the_install_log_is_an_absolute_path():
    """It was `-l "install.log"`, resolved against whatever working directory
    `win_package` launched the process with -- so the one artefact that explains a
    failure was written somewhere nobody could name. A live install returned
    `rc: 1603` with empty stdout and empty stderr, and 1603 alone is only "fatal
    error during installation"."""
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    args = rb.installer_arguments(env)
    assert rb.RB_INSTALL_LOG in args
    assert rb.RB_INSTALL_LOG[1:3] == ":\\", rb.RB_INSTALL_LOG
    assert '-l "install.log"' not in args
    db.close()


def test_the_log_is_written_outside_the_connecting_users_profile():
    """The run arrives over WinRM as an administrator whose `%TEMP%` resolves to an
    8.3 path, and an MSI-backed installer hands its work to msiexec running as
    SYSTEM. A machine-wide directory is readable by both, and by whoever logs on
    later to look."""
    assert "\\Users\\" not in rb.RB_INSTALL_LOG
    assert rb.RB_INSTALL_LOG.lower().startswith(
        "c:\\windows\\temp\\")


def test_the_path_the_installer_writes_is_the_path_the_play_reads():
    """Two halves of one fact. If they drift, the install still logs and the play
    still looks, at different files -- and the failure reports "no installer log was
    written", which reads as the installer being silent rather than as a bug here."""
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    meta = rb.queue(db, env, created_by="t").metadata_dict
    told_to_write = meta["extra_vars"][rb.ARGS_VAR]
    read_back_from = meta["extra_vars"][ansible_local_service.WINPKG_LOG_VAR]
    assert f'-l "{read_back_from}"' in told_to_write
    db.close()


# ── the asset type ───────────────────────────────────────────────────────────

def test_an_exe_is_a_windows_package_not_a_playbook():
    """Unrecognised extensions fall through to "playbook" and fail as unparseable YAML,
    which reads as a broken playbook rather than a file that never was one."""
    assert ansible_local_service.asset_type("BeyondTrust.Agents.Bootstrapper.exe") == "winpkg"
    assert ansible_local_service.asset_type("thing.msi") == "winpkg"


def test_all_three_extension_tables_agree():
    """An extension lives in THREE places and they drift independently:

      * `ansible_local_service._EXT_TYPE` decides which play is generated;
      * `storage_service._TYPE_MAP` decides what a listing calls it;
      * `storage_service._ASSET_EXTENSIONS` decides whether a listing shows it AT ALL.

    The third is the quiet one — an extension missing there uploads fine, stores fine, and
    then never appears in any picker, which reads as a failed upload.
    """
    for ext in (".yml", ".sh", ".ps1", ".rpm", ".deb", ".exe", ".msi"):
        assert (ansible_local_service.asset_type("x" + ext)
                == storage_service.asset_type("x" + ext)), ext
        assert ext in storage_service._ASSET_EXTENSIONS, f"{ext} would never be listed"


def test_the_generated_play_renders_the_arguments_variable():
    play = ansible_local_service.generate_playbook_yaml("Bootstrapper.exe")
    assert "win_package" in play
    assert ansible_local_service.WINPKG_ARGS_VAR in play
    # A run that passes no arguments must still render valid YAML.
    assert "default('')" in play


# ── selecting the host ───────────────────────────────────────────────────────

def test_a_linux_vm_named_rb_is_refused_with_the_reason():
    """A WinRM run against a Linux host fails as a connection timeout, which looks like a
    firewall and sends an SE somewhere else entirely."""
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, os_family="linux")
    try:
        rb.select_rb_vm(db, env)
        raise AssertionError("a Linux VM was accepted")
    except rb.ResourceBrokerError as exc:
        assert "Windows program" in str(exc)
    finally:
        db.close()


def test_an_unknown_os_family_is_refused_too():
    """Slice 3 made blank mean UNKNOWN rather than a guess, and this honours that."""
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, os_family="")
    try:
        rb.select_rb_vm(db, env)
        raise AssertionError("an unknown OS was accepted")
    except rb.ResourceBrokerError as exc:
        assert "nothing" in str(exc)
    finally:
        db.close()


def test_the_match_is_exact_and_names_what_it_found():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="rb-old")
    try:
        rb.select_rb_vm(db, env)
        raise AssertionError("a fuzzy match was accepted")
    except rb.ResourceBrokerError as exc:
        assert "rb-old" in str(exc)
    finally:
        db.close()


# ── preflight ────────────────────────────────────────────────────────────────

def test_a_missing_zone_is_refused_with_why_it_matters():
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    rb.configure(db, env, zone_name="")
    try:
        rb.preflight(db, env)
        raise AssertionError("a POV with no zone was accepted")
    except rb.ResourceBrokerError as exc:
        assert "ZONE" in str(exc) and "prompt" in str(exc)
    finally:
        db.close()


def test_a_missing_installer_key_is_refused():
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    rb.clear_installer_key(env)
    try:
        rb.preflight(db, env)
        raise AssertionError("a POV with no key was accepted")
    except rb.ResourceBrokerError as exc:
        assert "installer key" in str(exc)
    finally:
        db.close()


def test_a_staged_file_that_is_not_a_windows_installer_is_refused():
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    rb.configure(db, env, asset_key="playbook.yml")
    try:
        rb.preflight(db, env)
        raise AssertionError("a playbook was accepted as the installer")
    except rb.ResourceBrokerError as exc:
        assert ".exe" in str(exc)
    finally:
        db.close()


def test_a_broker_whose_policy_predates_the_grant_is_refused():
    db = d.SessionLocal()
    agent = _agent(db, reported=("agent_discover", "agent_gateway"))
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env)
    rb.configure(db, env, zone_name="Z", asset_key="a.exe")
    rb.set_installer_key(env, "k")
    try:
        rb.preflight(db, env)
        raise AssertionError("a policy without agent_ansible was accepted")
    except rb.ResourceBrokerError as exc:
        assert "policy.yaml predates" in str(exc) and "Broker" in str(exc)
    finally:
        db.close()


# ── the queued job ───────────────────────────────────────────────────────────

def test_the_job_carries_ids_not_the_key():
    db = d.SessionLocal()
    env, agent, vm = _ready(db)
    job = rb.queue(db, env, created_by="t")
    meta = job.metadata_dict
    assert job.job_type == "agent_ansible"
    assert job.agent_id == agent.id and job.status == "queued"
    assert "ik-secret-123" not in json.dumps(meta)
    # Only the NAME of the var, bound to a config source — the epml_token_var pattern.
    assert meta["secret_vars"] == {rb.KEY_VAR: rb.installer_key_config_key(env.id)}
    assert meta["pov_environment_id"] == env.id
    assert meta["pov_vm_id"] == vm.platform_vm_id
    db.close()


def test_the_run_is_winrm_at_the_windows_vm():
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    meta = rb.queue(db, env, created_by="t").metadata_dict
    assert meta["transport"] == "winrm"
    assert meta["target_host"] == vm.private_ip
    assert meta["target_port"] == rb.RB_PORT
    db.close()


def test_no_windows_password_is_stored_anywhere_for_the_run():
    """The whole reason 5b uses the platform's stored_credentials."""
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    meta = rb.queue(db, env, created_by="t").metadata_dict
    assert not meta.get("login_user")
    assert "managed_account" not in meta or not meta["managed_account"]
    db.close()


# ── the broker policy ────────────────────────────────────────────────────────

def test_the_ansible_grant_is_scoped_to_windows_guests():
    """`targets:` grants a port probe; this grants a playbook running as root. Widening
    one must never widen the other."""
    policy = pov_broker.render_policy(["10.9.0.10", "10.9.0.20"], ["10.9.0.20"])
    block = policy.split("ansible:")[1]
    assert "10.9.0.20/32" in block
    assert "10.9.0.10/32" not in block, "the Linux broker is inside the playbook grant"
    assert "5985, 5986" in block


def test_a_pov_with_no_windows_guest_grants_nothing():
    """Fail-closed, and rendered disabled rather than omitted so an operator reading the
    file on the broker VM can see the feature exists."""
    policy = pov_broker.render_policy(["10.9.0.10"])
    block = policy.split("ansible:")[1]
    assert "enabled: false" in block
    assert "cidr" not in block


def test_windows_targets_narrows_to_the_named_host_once_it_exists():
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="dc01", ip="10.9.0.30")
    _vm(db, env, name="rb", ip="10.9.0.20")
    assert rb.windows_targets(db, env) == ["10.9.0.20"]
    db.close()


def test_windows_targets_falls_back_to_every_windows_guest_before_one_is_named():
    """The policy is written at ENROLMENT, usually before anyone has chosen an RB host —
    so the alternative is a grant that stays empty until the POV is re-brokered."""
    db = d.SessionLocal()
    agent = _agent(db)
    env = _env(db, broker_agent_id=agent.id)
    _vm(db, env, name="dc01", ip="10.9.0.30")
    _vm(db, env, name="web01", ip="10.9.0.40", os_family="linux")
    assert rb.windows_targets(db, env) == ["10.9.0.30"]
    db.close()


# ── the platform login ───────────────────────────────────────────────────────

def _with_credentials(entries):
    from web_dashboard.services import lab_platforms

    class _Fake:
        async def stored_credentials(self, env_id, vm_id):
            return entries
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: _Fake()
    return original


def _restore(original):
    from web_dashboard.services import lab_platforms
    lab_platforms.adapter = original


def test_the_login_comes_from_the_platform_and_is_never_stored():
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    original = _with_credentials([{"text": "administrator / Passw0rd", "notes": ""}])
    try:
        got = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
    finally:
        _restore(original)
    assert got == ("administrator", "Passw0rd")
    # Nothing about it was written to the row or the config space.
    db.refresh(env)
    assert "Passw0rd" not in json.dumps(env.metadata_dict)
    db.close()


def test_a_windows_guest_with_several_logins_resolves_rather_than_refusing():
    """The whole point. `_vm` makes a Windows guest, so the box holding the template's
    administrator beside a leftover service account is resolved by the guest-OS rule and
    the install proceeds — where before it failed 409 at 90% with "2 usable stored
    credentials"."""
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    original = _with_credentials([{"text": "btadmin / Hunter2", "notes": "leftover"},
                                  {"text": "administrator / Passw0rd", "notes": ""}])
    try:
        got = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
    finally:
        _restore(original)
    assert got == ("administrator", "Passw0rd")
    db.close()


def test_the_per_vm_override_is_read_off_the_row():
    """The remedy the ambiguity refusal names, end to end: the column, the row lookup, and
    `pick`'s `prefer`. It stores a USERNAME — the password is still read live off the
    platform on every run and written nowhere, which is the property slice 5b exists for."""
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    note = pov_env_service.set_vm_login(db, env, vm.platform_vm_id, "btadmin")
    assert "btadmin" in note
    original = _with_credentials([{"text": "administrator / Passw0rd", "notes": ""},
                                  {"text": "btadmin / Hunter2", "notes": ""}])
    try:
        got = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
    finally:
        _restore(original)
    assert got == ("btadmin", "Hunter2")
    db.refresh(vm)
    assert vm.login_username == "btadmin"
    assert "Hunter2" not in json.dumps(env.metadata_dict)

    # Clearing it restores the rule, which is the normal state.
    pov_env_service.set_vm_login(db, env, vm.platform_vm_id, "")
    db.refresh(vm)
    assert vm.login_username is None
    db.close()


def test_an_override_that_cannot_be_a_login_is_refused_at_the_form():
    """Refused when it is typed rather than at the next run. A value the selector could
    never match would turn every subsequent run on this guest into a refusal, and the
    operator would be nowhere near this page when it happened."""
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    try:
        pov_env_service.set_vm_login(db, env, vm.platform_vm_id, "the admin account")
        raise AssertionError("a sentence was stored as a login")
    except pov_env_service.VmLoginError as exc:
        assert "administrator" in str(exc)
    db.close()


def test_a_login_override_survives_a_vm_refresh():
    """`refresh_vms` upserts by platform_vm_id rather than rebuilding the rows — the same
    property the PAM artifact columns rely on. If that ever changes, an operator's answer
    to an ambiguity would evaporate on the next sync and the refusal would come back."""
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    pov_env_service.set_vm_login(db, env, vm.platform_vm_id, "btadmin")

    from web_dashboard.services import lab_platforms

    class _Fake:
        async def get_environment(self, env_id):
            return {"runstate": "running",
                    "vms": [{"id": vm.platform_vm_id, "name": vm.name,
                             "os_family": "windows", "runstate": "running",
                             "private_ip": "10.9.0.10"}]}
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: _Fake()
    try:
        asyncio.run(pov_env_service.refresh_vms(db, env))
    finally:
        lab_platforms.adapter = original
    db.refresh(vm)
    assert vm.login_username == "btadmin"
    db.close()


def test_an_unusable_platform_credential_names_the_vm():
    db = d.SessionLocal()
    env, _a, vm = _ready(db)
    original = _with_credentials([{"text": "no credential here", "notes": ""}])
    try:
        asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
        raise AssertionError("an unusable credential was accepted")
    except rb.ResourceBrokerError as exc:
        assert "rb" in str(exc)
    finally:
        _restore(original)
        db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_clears_the_key_and_says_the_tenant_still_lists_the_broker():
    """The RB's registration is a customer-side object this dashboard never created.

    It is named by zone and install key, and `ps_application_host_id` never held it — so
    the line distinguishes forgetting the override from retiring the broker.
    """
    db = d.SessionLocal()
    env, _a, _v = _ready(db)
    env.ps_application_host_id = 4242
    db.commit()
    line = rb.teardown(db, env)
    assert not rb.has_installer_key(env)
    assert "4242" in line and "retire it" in line
    assert "override" in line
    assert env.ps_application_host_id is None
    db.close()


# -- the application host override --------------------------------------------
#
# `ps_application_host_id` had NO writer for the life of this feature, and
# `pov_wireup.ps_context` refused the whole Password Safe half without one -- so a POV
# could install a Resource Broker successfully and still never onboard a single VM.
#
# The column is also not what people thought it was. `application_host_id` names another
# MANAGED SYSTEM carrying `IsApplicationHost`; what routes Password Safe to a private
# address is the broker's resource ZONE and the workgroup mapped to it. The evidence:
# `cloud_database_service` onboards private databases through a Resource Broker passing no
# `application_host_id` at all, and BeyondTrust's own Skytap POC runbook (SELab, rev 7.0)
# never mentions an application host anywhere -- it creates the zone, adds the workgroup to
# it, and installs the broker into it.
#
# So this is an override for the tenant that wants one, and an escape hatch so no POV is
# ever stuck on a value nothing derives.


def test_setting_an_application_host_records_it():
    db = d.SessionLocal()
    env = _env(db)
    line = rb.set_application_host(db, env, 4242)
    assert env.ps_application_host_id == 4242
    assert "4242" in line
    assert rb.describe(db, env)["ps_application_host_id"] == 4242
    db.close()


def test_zero_clears_it_because_that_is_the_normal_state():
    db = d.SessionLocal()
    env = _env(db, ps_application_host_id=4242)
    line = rb.set_application_host(db, env, 0)
    assert env.ps_application_host_id is None
    assert "Cleared" in line
    db.close()


def test_a_negative_application_host_id_is_refused():
    db = d.SessionLocal()
    env = _env(db)
    try:
        rb.set_application_host(db, env, -1)
        raise AssertionError("a negative managed system id was accepted")
    except rb.ResourceBrokerError as exc:
        assert "positive" in str(exc)
    assert env.ps_application_host_id is None
    db.close()


def test_a_non_numeric_application_host_id_is_refused_not_coerced():
    db = d.SessionLocal()
    env = _env(db)
    try:
        rb.set_application_host(db, env, "broker01")
        raise AssertionError("a non-numeric managed system id was accepted")
    except rb.ResourceBrokerError as exc:
        assert "whole number" in str(exc)
    db.close()


def test_teardown_is_a_no_op_on_a_pov_that_never_had_one():
    db = d.SessionLocal()
    env = _env(db)
    assert "No Resource Broker" in rb.teardown(db, env)
    db.close()


# -- the asset picker on both pages -------------------------------------------
#
# `rb_asset` is a storage KEY, and two pages write it: the POV page's RB editor and the
# blueprint form. The blueprint form used to be a free-text box, so a name that matched
# nothing in storage saved fine and only surfaced at Install time as "no Resource Broker
# installer is staged" -- a preflight refusal that reads like an upload was never done.
# Both are now the same picker, fed by the same call and the same extension filter.
#
# Neither half is covered by tests/test_templates_parse.py: its x-for check only looks at
# helpers CALLED as functions, and `x-for="a in winAssets"` is a plain property. An
# undeclared one is not an error in Alpine -- it is an empty dropdown, which looks exactly
# like a storage backend with nothing staged in it.

_POV_PAGES = ("index.html", "templates.html")


def _pov_template(name: str) -> str:
    path = os.path.join(_ROOT, "web_dashboard", "templates", "pov", name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_both_pov_pages_pick_the_rb_asset_rather_than_typing_it():
    for name in _POV_PAGES:
        src = _pov_template(name)
        assert 'x-for="a in winAssets"' in src, f"{name}: no RB asset picker"
        # Declared on the component, or the picker is silently always empty.
        assert "winAssets: []" in src, f"{name}: winAssets undeclared"
        assert "loadWinAssets()" in src, f"{name}: nothing fills winAssets"


def test_both_pages_filter_to_what_the_preflight_will_accept():
    """The filter must not offer a file `preflight` would then refuse.

    `asset_type` is the authority -- these extensions are the ones it calls a winpkg, and
    anything else reaches the RB panel only to be told it is "not a Windows installer".
    """
    for ext in (".exe", ".msi"):
        assert ansible_local_service.asset_type("Bootstrapper" + ext) == "winpkg"
    for name in _POV_PAGES:
        assert r"/\.(exe|msi)$/i" in _pov_template(name), f"{name}: wrong asset filter"


def test_the_picker_does_not_read_through_a_feature_gated_router():
    """The asset list must come from /api/storage, not from /api/config-mgmt.

    `/api/config-mgmt/assets` and `/api/storage/list-all` return the same rows, but the
    config-mgmt router is mounted behind ``_feature_gate("ansible_enabled")`` while the
    storage router is mounted unconditionally. Both POV pickers used the gated one and
    soft-failed to an empty list, so on an instance with Configuration Management toggled
    off the field said "No Windows installer is staged" about an .exe that was staged --
    visible on the Storage page, in the same browser, at the same moment.
    """
    with open(os.path.join(_ROOT, "web_dashboard", "main.py"), encoding="utf-8") as fh:
        main_src = fh.read()
    assert 'config_mgmt.router, dependencies=[_feature_gate("ansible_enabled")]' in main_src, (
        "the config-mgmt gate moved -- recheck whether this test still has a subject")
    assert "app.include_router(storage.router)" in main_src, (
        "the storage router is no longer mounted ungated, so it is no longer the safe "
        "source for the installer picker")
    for name in _POV_PAGES:
        src = _pov_template(name)
        picker = src.split("async loadWinAssets()", 1)[1].split(chr(10) + "    },", 1)[0]
        assert "/api/storage/list-all" in picker, f"{name}: picker does not read storage"
        assert "/api/config-mgmt" not in picker, (
            f"{name}: picker reads the feature-gated config-mgmt router again")


def test_the_picker_offers_only_the_backend_the_install_fetches_from():
    """`queue` sets ``asset_backend=active_backend()``, so an .exe on any other backend is
    a run that fails when the agent fetches the bundle. The picker filters to the active
    backend and reports the others separately -- "staged on the wrong backend" and "not
    staged at all" have different remedies, and the field used to give the second answer
    to both."""
    assert "asset_backend=storage_service.active_backend()" in pathlib_read(
        os.path.join(_ROOT, "web_dashboard", "services", "pov_resource_broker.py")), (
        "the install no longer fetches from the active backend -- the picker's filter "
        "was justified by that and needs rechecking")
    for name in _POV_PAGES:
        src = _pov_template(name)
        assert "winAssetsElsewhere" in src, f"{name}: wrong-backend case not surfaced"
        assert "winAssetsError" in src, f"{name}: a failed read still reads as 'not staged'"
        assert "/api/storage/backends" in src, f"{name}: nothing reads the active backend"


def pathlib_read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_staging_hint_points_at_the_page_that_can_actually_upload():
    """Both pages tell an SE where to stage the installer, and the link has been wrong
    twice. First it was `/config-management`, which 404s. Then it was `/config-mgmt`,
    which exists but carries no file input at all -- that page RUNS an asset, the Storage
    page is what accepts one. An SE following the hint landed on a page with nothing to
    click and concluded the .exe was unsupported."""
    with open(os.path.join(_ROOT, "web_dashboard", "main.py"), encoding="utf-8") as fh:
        main_src = fh.read()
    assert '@app.get("/storage"' in main_src
    for name in _POV_PAGES:
        src = _pov_template(name)
        assert 'href="/storage"' in src, f"{name}: no staging hint"
        assert "/config-management" not in src, f"{name}: dead upload link"
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "storage", "index.html"),
              encoding="utf-8") as fh:
        assert 'type="file"' in fh.read(), "the Storage page lost its uploader"


def _storage_page() -> str:
    path = os.path.join(_ROOT, "web_dashboard", "templates", "storage", "index.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_upload_form_offers_every_extension_the_backend_stores():
    """The Storage page gates uploads TWICE on the client -- the file input's `accept`
    and a second list in `uploadFile()` -- and neither is enforced server-side, so a
    missing extension is a purely cosmetic refusal that is nonetheless total: the picker
    greys the file out and the toast says "Unsupported file type".

    That is exactly how `.exe` was blocked. Every other table already called it a winpkg
    and `ansible_local_service` already generated a `win_package` play for it; only these
    two literals disagreed, so the Resource Broker bootstrapper could not be staged at
    all. Pinned against `_ASSET_EXTENSIONS` because that set is what decides whether an
    uploaded asset is ever LISTED -- offering an upload the listing then hides is the
    same dead end from the other direction.
    """
    src = _storage_page()
    accept = src.split('accept="', 1)[1].split('"', 1)[0]
    offered = {e.strip().lower() for e in accept.split(",") if e.strip()}
    allowed_js = src.split("const allowed = [", 1)[1].split("]", 1)[0]
    checked = {e.strip().strip("'\"").lower() for e in allowed_js.split(",") if e.strip()}
    for ext in storage_service._ASSET_EXTENSIONS:
        assert ext in offered, f"{ext}: the file picker greys it out"
        assert ext in checked, f"{ext}: uploadFile() rejects it before the request"
    assert offered == checked, "the two client-side lists disagree"


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
