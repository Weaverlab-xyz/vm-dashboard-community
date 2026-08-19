"""Locally sealed credentials — `password_sealed` and `client_secret_sealed`.

For the site that keeps a hypervisor credential on the agent host but will not keep it as
text. `seal` encrypts one value against a key in the state volume and prints it; the
operator pastes the result in and deletes the plaintext.

What these tests are actually defending, because it is narrower than "encryption" suggests:
sealing is NOT protection from root on the agent host — the key is on the same machine,
which is unavoidable for a container that restarts unattended. It protects the config file,
which unlike the state volume gets copied into git repos, Ansible roles, support tickets,
screenshots and backups. So the properties worth pinning are the ones that make a *copy*
useless and the ones that keep a misconfiguration from reading as something else:

* the value is bound to the target address, so it cannot be relabelled onto an entry
  pointing somewhere an attacker controls — the same property `seal_aad`'s `ref` field
  carries for the dashboard-held credential, and the reason the dashboard may never set
  `host`;
* it is bound to a purpose, so a hypervisor password cannot be pasted in as a Password
  Safe client secret;
* the key id travels in the clear, because the mistake this feature actually produces is
  running `seal` without mounting the state volume, and "decryption failed" does not name
  that;
* `seal` refuses outright when the state directory is not a mounted volume, since the key
  it would create dies with the container and takes every sealed value with it;
* a sealed value found in a plaintext field is refused rather than sent, because sent as a
  literal it comes back as a wrong password — the misleading failure everything else in
  this file works to avoid.

No network and no container: the module is loaded directly with a temporary state dir.

Runs under pytest, or standalone:  python tests/test_agent_local_seal.py
"""
import importlib.util
import io
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")
_STATE = tempfile.mkdtemp(prefix="agent-seal-state-")

try:
    import yaml
    os.environ["AGENT_STATE_DIR"] = _STATE
    _spec = importlib.util.spec_from_file_location("agent_runner_seal", _PATH)
    agent = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(agent)
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

# The guard under test is `_state_dir_is_ephemeral`, and on a developer machine a tempdir
# and `/` are usually the same device — which is the answer it gives for "no volume
# mounted". Stub it off by default so every other test can seal, and drive the real one
# directly in test_seal_refuses_a_state_dir_that_will_not_survive_the_container.
_REAL_EPHEMERAL = agent._state_dir_is_ephemeral
agent._state_dir_is_ephemeral = lambda: False

HOST = "vcenter.lab.internal"


class _HoldingDash:
    """Stands in for the Dashboard client, recording what was registered for redaction."""

    def __init__(self):
        self.held = []

    def hold_secret(self, value):
        self.held.append(value)


def _sealed(value="s3cret-pw", host=HOST, purpose=None):
    return agent.local_seal(value, host=host,
                            purpose=purpose or agent.LOCAL_PURPOSE_PASSWORD)


# ── the token ─────────────────────────────────────────────────────────────────

def test_a_sealed_value_round_trips():
    token = _sealed()
    assert token.startswith(agent.LOCAL_SEAL_PREFIX)
    assert agent.local_unseal(token, host=HOST,
                              purpose=agent.LOCAL_PURPOSE_PASSWORD) == "s3cret-pw"


def test_the_token_is_one_plain_yaml_scalar():
    """It is pasted into a YAML file by hand, so it must need no quoting and fit one line.

    The prefix is what guarantees the scalar starts with a letter whatever the base64
    happens to be — a bare `_…` or `-…` value is a different YAML shape.
    """
    token = _sealed()
    assert " " not in token and "\n" not in token and "\t" not in token
    assert token[0].isalpha()
    doc = yaml.safe_load(f"connections:\n  - name: dc1\n    password_sealed: {token}\n")
    assert doc["connections"][0]["password_sealed"] == token


def test_the_plaintext_appears_nowhere_in_the_token():
    token = _sealed("correct-horse-battery-staple")
    assert "correct" not in token and "staple" not in token


def test_two_seals_of_one_value_differ():
    """A fresh nonce per seal. Otherwise the file leaks which connections share a password."""
    assert _sealed("same") != _sealed("same")


def test_an_empty_value_is_refused_and_blames_the_missing_flag():
    """`docker run` without `-i` gives the container no stdin, so the read comes back empty.

    That is the second-likeliest mistake after forgetting the volume, and "the value is
    empty" alone points at the value rather than at the flag.
    """
    try:
        _sealed("")
    except agent.LocalSealError as exc:
        assert "nothing to seal" in str(exc), exc
        assert "-it" in str(exc), "name the flag, not just the symptom"
    else:
        raise AssertionError("sealing an empty value would produce an empty password")


def test_a_seal_must_be_bound_to_an_address():
    """A seal bound to nothing has no anti-relabel property, which is most of the point."""
    try:
        agent.local_seal("pw", host="  ", purpose=agent.LOCAL_PURPOSE_PASSWORD)
    except agent.LocalSealError as exc:
        assert "bound to an address" in str(exc), exc
    else:
        raise AssertionError("an unbound seal must be refused")


def test_the_token_version_and_the_aad_version_cannot_drift():
    """Two independent "version 1"s in one construction is how a format change ships half
    applied. The prefix is built from `LOCAL_SEAL_VERSION` and the AAD carries the same
    number; this asserts the second half, which is the one a refactor can silently break."""
    import json
    assert agent.LOCAL_SEAL_PREFIX == f"sealed.v{agent.LOCAL_SEAL_VERSION}."
    aad = json.loads(agent.local_seal_aad(host="h", purpose="p").decode())
    assert aad["v"] == agent.LOCAL_SEAL_VERSION


def test_the_local_seal_namespace_never_collides_with_the_transport_seal():
    """`agent_sealing`'s constants are pinned byte-for-byte against the dashboard by
    tests/test_agent_runner_contract.py. Shadowing one of them here would break the wire
    format for the dashboard-held credential, in a file where both live side by side."""
    assert agent.SEAL_INFO != agent._LOCAL_CIPHER_INFO != agent._LOCAL_KEYID_INFO
    assert agent.SEAL_ALG not in (agent.LOCAL_PURPOSE_PASSWORD,
                                 agent.LOCAL_PURPOSE_PS_CLIENT)
    assert agent.LOCAL_SEAL_PREFIX not in str(agent.SEAL_INFO)


# ── the bindings ──────────────────────────────────────────────────────────────

def test_the_host_binding_stops_the_value_being_relabelled():
    """The attack the binding exists for, and it is not hypothetical.

    connections.yaml is re-read per job (`run_hypervisor` calls
    `HypervisorConnections.load()` every time), so an edit takes effect with no restart and
    with no Docker access. Someone who can write that file but cannot read the state volume
    — an operator account outside the `docker` group — could otherwise move this value onto
    an entry whose `host:` they own, with `verify_ssl: false`, and read the plaintext off
    the next sync. Same reasoning as `seal_aad`'s `ref`, and as the rule that the dashboard
    may hold the secret but never the target.
    """
    # Mixed case on purpose: the refusal has to echo the address as the BINDING saw it,
    # normalised, or an operator comparing it against their file sees two different strings
    # and concludes the message is about something else.
    decoy = "Attacker.Example.COM"
    token = _sealed()
    try:
        agent.local_unseal(token, host=decoy, purpose=agent.LOCAL_PURPOSE_PASSWORD)
    except agent.LocalSealError as exc:
        assert "did not authenticate" in str(exc), exc
        # Derived through `seal_host_key` rather than compared against a host-shaped
        # literal. That is the stronger assertion — it pins that the message is built from
        # the normalised host and not the raw input — and it keeps CodeQL's
        # py/incomplete-url-substring-sanitization off a line that is asserting on an error
        # string, not sanitising a URL.
        assert repr(agent.seal_host_key(decoy)) in str(exc), (
            "name the address it was asked for, as the binding normalised it")
        assert "moved here from another entry" in str(exc), "say what this defends"
    else:
        raise AssertionError("a sealed value must not open against a different host")


def test_the_host_binding_normalises_case_whitespace_and_the_trailing_dot():
    """Or the same address typed two ways is a refusal for no reason an operator can see —
    and indistinguishable from the relabelling this binding exists to catch.

    The trailing dot is the non-obvious one: `vc.lab.local.` is the same name as
    `vc.lab.local` to DNS, and both forms appear in real inventories.
    """
    token = agent.local_seal("pw", host="  VCenter.Lab.Internal.  ",
                             purpose=agent.LOCAL_PURPOSE_PASSWORD)
    for spelling in ("vcenter.lab.internal", "VCENTER.LAB.INTERNAL",
                     "vcenter.lab.internal.", " vcenter.lab.internal "):
        assert agent.local_unseal(token, host=spelling,
                                  purpose=agent.LOCAL_PURPOSE_PASSWORD) == "pw", spelling


def test_the_purpose_binding_separates_the_two_files():
    """A hypervisor password must not open as a Password Safe client secret."""
    token = _sealed()
    try:
        agent.local_unseal(token, host=HOST,
                           purpose=agent.LOCAL_PURPOSE_PS_CLIENT)
    except agent.LocalSealError:
        pass
    else:
        raise AssertionError("the purpose must be bound, not decorative")


def test_the_aad_is_never_carried_in_the_token():
    """Both sides rebuild it from the config file. A transmitted binding binds nothing."""
    token = _sealed(host="vcenter.lab.internal")
    assert "vcenter" not in token
    assert agent.LOCAL_PURPOSE_PASSWORD not in token


def test_a_tampered_token_is_refused():
    """Flipped at the BYTE level, not the base64 character level.

    Changing the last character before the `=` padding can decode to the same bytes — the
    padding bits are not significant — so a character-level flip is a test that passes by
    accident most of the time and fails the rest. Decode, flip, re-encode.
    """
    import base64
    prefix, _, blob = _sealed().rpartition(".")
    raw = bytearray(base64.urlsafe_b64decode(blob.encode("ascii")))
    for index in (0, len(raw) - 1):          # the nonce, and the GCM tag
        bad = bytearray(raw)
        bad[index] ^= 0x01
        token = f"{prefix}.{base64.urlsafe_b64encode(bytes(bad)).decode('ascii')}"
        try:
            agent.local_unseal(token, host=HOST,
                               purpose=agent.LOCAL_PURPOSE_PASSWORD)
        except agent.LocalSealError:
            continue
        raise AssertionError(f"AES-GCM must reject a token modified at byte {index}")


def test_a_field_value_cannot_inject_a_delimiter():
    """The AAD is canonical JSON, not concatenation — the same reason `seal_aad` is.

    A host containing what looks like a field separator must not let one binding be
    reinterpreted as another.
    """
    a = agent.local_seal_aad(host='a", "purpose": "x', purpose="p")
    b = agent.local_seal_aad(host="a", purpose='x", "host": "p')
    assert a != b


# ── the key ───────────────────────────────────────────────────────────────────

def test_the_key_id_names_both_keys_when_they_differ():
    """The predicted #1 mistake: `seal` run without the state volume.

    The value is then sealed against a key that no longer exists, and without the id in the
    token the symptom is "decryption failed", which points at nothing. With it the agent can
    say which key sealed the value and which one is present.
    """
    token = _sealed()
    real = agent._LOCAL_KEY_CACHE
    agent._LOCAL_KEY_CACHE = b"\x11" * 32
    try:
        agent.local_unseal(token, host=HOST, purpose=agent.LOCAL_PURPOSE_PASSWORD)
    except agent.LocalSealError as exc:
        assert agent._key_id(real) in str(exc), "name the key that sealed it"
        assert agent._key_id(b"\x11" * 32) in str(exc), "name the key that is here"
        assert "state volume" in str(exc), "name the cause, not just the mismatch"
    else:
        raise AssertionError("the key id must be checked")
    finally:
        agent._LOCAL_KEY_CACHE = real


def test_the_key_id_shares_no_derivation_with_the_key_that_encrypts():
    """Both roles are HKDF'd out of the key file under distinct `info` labels.

    The same domain separation `SEAL_INFO` gives the transport seal, and the reason it is
    worth two hash calls: the id travels in the clear on every token, so it must not be a
    handle on the material that does the encrypting. It also means the file's raw bytes stop
    being load-bearing on their own — the id derivation can change, and a future
    operator-supplied passphrase can live in the same file without either role borrowing the
    other's material.
    """
    import base64
    material = agent._LOCAL_KEY_CACHE
    key_id = agent._key_id(material)
    cipher = agent._local_derive(material, agent._LOCAL_CIPHER_INFO)

    assert len(key_id) == 8 and int(key_id, 16) >= 0     # 4 bytes, hex
    assert key_id not in base64.b64encode(material).decode()
    assert key_id not in material.hex()
    assert key_id not in cipher.hex(), "the id must not be derivable from the cipher key"
    assert cipher != material, "the file bytes must not be used directly as the AES key"
    assert agent._local_derive(material, agent._LOCAL_KEYID_INFO, 4).hex() == key_id


def test_the_key_file_is_created_private():
    """0600 in a 0700 directory, the same standard `identity.json` is held to.

    Skipped where mode bits do not map — this asserts the POSIX property that matters in
    the container, and the agent image is always Linux even on a Windows host.
    """
    path = agent._LOCAL_KEY_FILE
    assert os.path.exists(path), "sealing should have created the key"
    if os.name == "nt":
        return
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(agent.STATE_DIR).st_mode & 0o700) == "0o700"


def test_the_job_path_never_creates_a_key():
    """`local_key()` defaults to create=False, and that default is the safety property.

    An agent that generated a key because it could not find one would turn "the wrong
    volume is mounted" into a silent new key and a pile of values it can no longer open.
    """
    token = _sealed()
    real = agent._LOCAL_KEY_CACHE
    saved = agent._LOCAL_KEY_FILE
    agent._LOCAL_KEY_CACHE = None
    agent._LOCAL_KEY_FILE = os.path.join(_STATE, "absent", "sealing.key")
    try:
        agent.local_unseal(token, host=HOST, purpose=agent.LOCAL_PURPOSE_PASSWORD)
    except agent.LocalSealError as exc:
        assert "no sealing key" in str(exc) or "there is none at" in str(exc), exc
        assert "state volume" in str(exc), exc
        assert not os.path.exists(agent._LOCAL_KEY_FILE), (
            "a job path must never write a key")
    else:
        raise AssertionError("a missing key must be refused, not generated")
    finally:
        agent._LOCAL_KEY_FILE = saved
        agent._LOCAL_KEY_CACHE = real


def test_a_truncated_key_file_says_so_instead_of_failing_to_decrypt():
    real = agent._LOCAL_KEY_CACHE
    saved = agent._LOCAL_KEY_FILE
    short = os.path.join(_STATE, "short.key")
    with open(short, "w", encoding="utf-8") as fh:
        fh.write("YWJj")  # b"abc"
    agent._LOCAL_KEY_CACHE = None
    agent._LOCAL_KEY_FILE = short
    try:
        agent.local_key()
    except agent.LocalSealError as exc:
        assert "3 bytes, not 32" in str(exc), exc
    else:
        raise AssertionError("a short key must be named, not used")
    finally:
        agent._LOCAL_KEY_FILE = saved
        agent._LOCAL_KEY_CACHE = real


def test_a_utf16_key_file_gets_the_same_explanation_as_every_other_secret_file():
    """The fourth hand-written secret file, so it inherits the PowerShell 5.1 diagnosis.

    `test_agent_runner_contract.test_the_other_hand_written_secret_files_decode_the_same_way`
    makes the same point for `password_file` and `client_secret_file`. An operator who
    creates this key by hand on Windows hits UTF-16, and a decode traceback would say
    nothing.
    """
    real = agent._LOCAL_KEY_CACHE
    saved = agent._LOCAL_KEY_FILE
    bad = os.path.join(_STATE, "utf16.key")
    with open(bad, "wb") as fh:
        fh.write("YWJj".encode("utf-16"))
    agent._LOCAL_KEY_CACHE = None
    agent._LOCAL_KEY_FILE = bad
    try:
        agent.local_key()
    except agent.LocalSealError as exc:
        assert "UTF-16" in str(exc), exc
    else:
        raise AssertionError("an undecodable key file must be explained")
    finally:
        agent._LOCAL_KEY_FILE = saved
        agent._LOCAL_KEY_CACHE = real


def test_seal_refuses_a_state_dir_that_will_not_survive_the_container():
    """`seal` without `-v` seals against a key that dies on exit, taking the value with it.

    Nothing recovers from that — not the plaintext, which the operator has been told to
    delete — so it is a refusal at seal time rather than a mystery at job time.
    """
    real = agent._LOCAL_KEY_CACHE
    saved_file, saved_probe = agent._LOCAL_KEY_FILE, agent._state_dir_is_ephemeral
    agent._LOCAL_KEY_CACHE = None
    agent._LOCAL_KEY_FILE = os.path.join(_STATE, "never-written.key")
    agent._state_dir_is_ephemeral = lambda: True
    try:
        agent.local_seal("pw", host=HOST, purpose=agent.LOCAL_PURPOSE_PASSWORD)
    except agent.LocalSealError as exc:
        assert "outlives this container" in str(exc), exc
        assert "tmpfs" in str(exc), "name the other way this happens"
        assert "dashboard_agent_state" in str(exc), "give the flag that fixes it"
        assert not os.path.exists(agent._LOCAL_KEY_FILE), "and write nothing"
    else:
        raise AssertionError("sealing against a disposable key must be refused")
    finally:
        agent._state_dir_is_ephemeral = saved_probe
        agent._LOCAL_KEY_FILE = saved_file
        agent._LOCAL_KEY_CACHE = real


def test_the_ephemeral_refusal_has_a_lab_override():
    """In the shape of AGENT_INSECURE_TLS. A wrong refusal here blocks a real operator —
    running `seal` outside a container on a single-partition host looks identical to no
    volume mounted — so there has to be a way past it."""
    real = agent._LOCAL_KEY_CACHE
    saved_file, saved_probe = agent._LOCAL_KEY_FILE, agent._state_dir_is_ephemeral
    saved_ok = agent.SEAL_EPHEMERAL_OK
    agent._LOCAL_KEY_CACHE = None
    agent._LOCAL_KEY_FILE = os.path.join(_STATE, "lab-override.key")
    agent._state_dir_is_ephemeral = lambda: True
    agent.SEAL_EPHEMERAL_OK = True
    try:
        token = agent.local_seal("pw", host=HOST, purpose=agent.LOCAL_PURPOSE_PASSWORD)
        assert agent.local_unseal(token, host=HOST,
                                  purpose=agent.LOCAL_PURPOSE_PASSWORD) == "pw"
    finally:
        agent.SEAL_EPHEMERAL_OK = saved_ok
        agent._state_dir_is_ephemeral = saved_probe
        agent._LOCAL_KEY_FILE = saved_file
        agent._LOCAL_KEY_CACHE = real


def test_a_tmpfs_state_dir_counts_as_ephemeral():
    """`st_dev` alone says "a different filesystem", not "a durable one".

    A `--tmpfs /var/lib/dashboard-agent` mount passes the device check and is gone on exit,
    so the fstype has to be looked at too. This is the one case where the cheap check gives
    the wrong answer in the expensive direction: the values become unopenable and the
    plaintext has already been deleted.
    """
    saved = agent._mount_fstype
    agent._mount_fstype = lambda path: "tmpfs"
    try:
        # A different device (so the first check passes) but a tmpfs, so still ephemeral.
        if _REAL_EPHEMERAL() and os.stat(_STATE).st_dev == os.stat("/").st_dev:
            return  # same device here anyway; the device check already refuses
        assert agent._state_dir_is_ephemeral() is True
    finally:
        agent._mount_fstype = saved


def test_the_real_ephemeral_probe_answers_without_raising():
    """The stubs above must not be hiding a broken implementation."""
    assert isinstance(_REAL_EPHEMERAL(), bool)
    assert isinstance(agent._mount_fstype(_STATE), str)
    assert isinstance(agent._mount_fstype("/definitely/not/a/mount/point"), str)


def test_the_fstype_lookup_takes_the_longest_matching_mount():
    """Driven against a real-shaped mountinfo, because the tmpfs refusal depends on it.

    Longest prefix, not first match: `/` matches everything, so an exact- or first-match
    parser would report the root filesystem for a path inside a nested mount and the tmpfs
    case would never fire. The `- <fstype>` separator is what makes this parseable at all;
    the optional fields before it are variable in number, which is why the line is split on
    that separator rather than by index.
    """
    fixture = os.path.join(_STATE, "mountinfo")
    with open(fixture, "w", encoding="utf-8") as fh:
        fh.write(
            "24 30 0:22 / / rw,relatime - overlay overlay rw,lowerdir=/x\n"
            "31 24 0:26 / /var/lib rw,relatime - ext4 /dev/sdb rw\n"
            "32 31 0:27 / /var/lib/dashboard-agent rw,relatime shared:9 - tmpfs tmpfs rw\n"
            "33 24 0:28 / /etc/dashboard-agent ro,relatime - 9p host rw\n"
            "garbage line with no separator\n")
    saved = agent._MOUNTINFO
    agent._MOUNTINFO = fixture
    try:
        assert agent._mount_fstype("/var/lib/dashboard-agent") == "tmpfs"
        # A subdirectory of a mount inherits it — the case the st_dev check also gets right.
        assert agent._mount_fstype("/var/lib/dashboard-agent/nested") == "tmpfs"
        assert agent._mount_fstype("/var/lib/other") == "ext4"
        assert agent._mount_fstype("/etc/dashboard-agent") == "9p"
        assert agent._mount_fstype("/anything/else") == "overlay"
        # A path that is a string prefix but not a path prefix must not match.
        assert agent._mount_fstype("/var/libother") == "overlay"
    finally:
        agent._MOUNTINFO = saved


def test_a_missing_mountinfo_raises_no_objection():
    """A non-Linux host or a restricted /proc must not turn into a refusal to seal."""
    saved = agent._MOUNTINFO
    agent._MOUNTINFO = os.path.join(_STATE, "no-such-mountinfo")
    try:
        assert agent._mount_fstype("/var/lib/dashboard-agent") == ""
    finally:
        agent._MOUNTINFO = saved


def test_the_key_file_is_never_overwritten():
    """`O_EXCL`, not write-temp-and-rename.

    Two `seal` runs racing must not have the second replace a key the first has already
    sealed a value against; nothing recovers from that. So a create against an existing
    path reports the race rather than clobbering, and the caller re-reads.
    """
    before = open(agent._LOCAL_KEY_FILE, encoding="utf-8").read()
    assert agent._generate_local_key() == "", (
        "creating over an existing key must report the race, not succeed")
    assert open(agent._LOCAL_KEY_FILE, encoding="utf-8").read() == before


# ── precedence in _secret_for ─────────────────────────────────────────────────

def test_a_sealed_password_is_used():
    conn = {"name": "dc1", "host": HOST, "password_sealed": _sealed()}
    assert agent._secret_for(conn) == "s3cret-pw"


def test_a_sealed_password_beats_a_leftover_plaintext_one():
    """Same rule as a remote source over a literal: whoever sealed a value is done with the
    plaintext, and silently preferring the leftover would keep a cleared host authenticating
    with the secret it thought it had removed."""
    for stale in ("password", "password_file"):
        conn = {"name": "dc1", "host": HOST, "password_sealed": _sealed(),
                stale: "stale-plaintext"}
        assert agent._secret_for(conn) == "s3cret-pw"


def test_both_remote_authorities_still_beat_a_sealed_value():
    """`password_sealed` is not a third authority — it is the same local credential, not
    written down. So the ordering above it is unchanged."""
    calls = []

    class _Ctx(agent.JobSecrets):
        def dashboard_secret(self, conn):
            calls.append(conn["name"])
            return "from-dashboard"

    ctx = _Ctx(job_id="j1", dashboard=_HoldingDash(), ref="dc1")
    conn = {"name": "dc1", "host": HOST, "dashboard_secret": True,
            "password_sealed": _sealed()}
    assert agent._secret_for(conn, ctx) == "from-dashboard"
    assert calls == ["dc1"]


def test_a_sealed_value_in_the_password_field_is_refused_not_sent():
    """Sent as a literal it comes back as a wrong password — the misleading failure this
    whole precedence chain exists to avoid — and quietly opening it would create a second,
    undocumented way to configure the same thing with none of the leftover warnings."""
    try:
        agent._secret_for({"name": "dc1", "host": HOST, "password": _sealed()})
    except agent.PolicyRefusal as exc:
        assert "not the field for one" in str(exc), exc
        assert "password_sealed" in str(exc), "name the right key"
    else:
        raise AssertionError("a sealed value in `password` must be refused")


def test_a_typo_in_the_key_name_lands_on_the_no_credential_refusal():
    """Unknown keys are ignored by design — `HypervisorConnections.load` keeps raw dicts —
    so a misspelled `password_sealed` is invisible. It still cannot send an empty password."""
    try:
        agent._secret_for({"name": "dc1", "host": HOST, "password_seald": _sealed()})
    except agent.PolicyRefusal as exc:
        assert "declares no credential" in str(exc), exc
    else:
        raise AssertionError("expected the no-credential refusal")


def test_a_plaintext_pasted_into_password_sealed_is_not_reported_as_truncation():
    """The likeliest mistake in a paste-it-in workflow is pasting the wrong thing.

    This used to say the value "starts with 'sealed.v1.' but is not a sealed value" about a
    value that plainly does not, because the prefix was sliced off without being checked —
    sending the operator after a truncation that never happened. The connection name also
    appeared twice, once from the wrapper and once from inside the message.
    """
    try:
        agent._secret_for({"name": "dc1", "host": HOST, "password_sealed": "hunter2"})
    except agent.PolicyRefusal as exc:
        text = str(exc)
        assert "is not a sealed value" in text, text
        assert "truncated" not in text, "do not blame a truncation that did not happen"
        assert text.count("'dc1'") == 1, f"the connection is named twice: {text}"
        assert "seal it" in text, "say what to do with a plaintext"
    else:
        raise AssertionError("expected a refusal")


def test_a_cut_off_token_says_so_at_whichever_point_it_was_cut():
    """The other half of the message split above: a value that really is a cut-off token.

    Two cut points, two messages. Cut inside the key id and there is no data section at all
    — "not a complete one". Cut inside the data and the base64 no longer decodes, which is
    also what a line-wrapped paste looks like, so that message names the wrapping.
    """
    token = _sealed()
    head, _, blob = token.rpartition(".")
    # A length of 4n+1 is never valid base64, whatever the padding happened to be.
    ragged = blob[:len(blob) - ((len(blob) - 1) % 4)]
    assert len(ragged) % 4 == 1 and len(ragged) > 1

    for value, expected in (
            (token[:len(agent.LOCAL_SEAL_PREFIX) + 5], "not a complete one"),
            (f"{head}.{ragged}", "one unbroken line"),
            # Cut by a whole base64 group: it still decodes, so GCM is what rejects it.
            (f"{head}.{blob[:-4]}", "did not authenticate")):
        try:
            agent._secret_for({"name": "dc1", "host": HOST, "password_sealed": value})
        except agent.PolicyRefusal as exc:
            assert expected in str(exc), (value, str(exc))
        else:
            raise AssertionError(f"expected a refusal for {value!r}")


def test_a_sealed_value_inside_a_password_file_is_refused_too():
    path = os.path.join(_STATE, "sealed-in-a-file.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_sealed())
    try:
        agent._secret_for({"name": "dc1", "host": HOST, "password_file": path})
    except agent.PolicyRefusal as exc:
        assert "password_sealed" in str(exc), exc
    else:
        raise AssertionError("a sealed value in a password_file must be refused")


def test_an_unopenable_sealed_value_surfaces_as_a_refusal_naming_the_connection():
    """`LocalSealError` must not escape into a job handler as an unhandled exception — a
    failed job renders `error_message` and nothing else, so the remedy has to be in it."""
    conn = {"name": "dc1", "host": "moved.lab.internal",
            "password_sealed": _sealed(host=HOST)}
    try:
        agent._secret_for(conn)
    except agent.PolicyRefusal as exc:
        assert "dc1" in str(exc) and "did not authenticate" in str(exc), exc
    else:
        raise AssertionError("expected a PolicyRefusal")


def test_every_credential_source_is_registered_for_outbound_redaction():
    """Sealing a value and then leaking it into Live Output would defeat the point.

    `hold_secret` used to have exactly one caller — the dashboard fetch — so a credential
    from a local file, an inline literal or a Password Safe checkout was never scrubbed, and
    a hypervisor that echoes it into `str(exc)` could carry it into the job row the dashboard
    renders as the failure reason. Every source goes through `_held` now.
    """
    dash = _HoldingDash()
    ctx = agent.JobSecrets(job_id="j1", dashboard=dash, ref="dc1")
    conn = {"name": "dc1", "host": HOST, "password_sealed": _sealed()}
    assert agent._secret_for(conn, ctx) == "s3cret-pw"
    assert dash.held == ["s3cret-pw"]

    # And the plaintext forms, which were the pre-existing gap.
    dash = _HoldingDash()
    agent._secret_for({"name": "dc1", "password": "inline-pw"},
                      agent.JobSecrets(job_id="j1", dashboard=dash, ref="dc1"))
    assert dash.held == ["inline-pw"]

    path = os.path.join(_STATE, "held.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("from-a-file")
    dash = _HoldingDash()
    agent._secret_for({"name": "dc1", "password_file": path},
                      agent.JobSecrets(job_id="j1", dashboard=dash, ref="dc1"))
    assert dash.held == ["from-a-file"]


def test_the_price_of_registering_a_plaintext_credential_is_over_redaction():
    """The trade in registering every source, written down because it is a real cost.

    `redact` is a blind substring replace, and `hold_secret` only skips values under four
    characters. `connections.example.yaml` ships `username: root@pam` for Proxmox and
    `username: root` for XCP-ng, so a lab whose password is literally `root` now sees its
    *username* redacted out of Live Output.

    Registering it anyway is the deliberate call: the alternative is that a hypervisor which
    echoes the credential into `str(exc)` carries it into the job row, and the dashboard is a
    different trust domain from the host. An operator who put the password in the file in
    cleartext accepted host-readable exposure — not dashboard-readable. Cosmetic damage to a
    log line is the cheaper failure, and it is visible; a leaked credential is not.
    """
    class _Dash(_HoldingDash):
        def __init__(self):
            super().__init__()
            self._held_set = set()

        def hold_secret(self, value):
            super().hold_secret(value)
            if value and len(str(value)) >= 4:
                self._held_set.add(str(value))

        def redact(self, text):
            for secret in self._held_set:
                text = text.replace(secret, "***redacted***")
            return text

    dash = _Dash()
    agent._secret_for({"name": "lab-proxmox", "password": "root"},
                      agent.JobSecrets(job_id="j1", dashboard=dash, ref="lab-proxmox"))
    assert dash.redact("authenticated to pve as root@pam") == (
        "authenticated to pve as ***redacted***@pam"), (
        "if this ever stops being true, the four-character floor changed and this trade "
        "should be revisited")


def test_secret_for_still_works_with_no_job_context():
    """Thirteen call sites pass `checkins`, but the contract test calls it with none."""
    assert agent._secret_for({"name": "dc1", "password": "inline"}) == "inline"
    assert agent._secret_for({"name": "dc1", "password": "inline"}, []) == "inline"


# ── passwordsafe.yaml ─────────────────────────────────────────────────────────

def _ps_file(**fields):
    path = os.path.join(_STATE, "passwordsafe.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(dict({"api_url": "https://ps.corp.internal",
                             "client_id": "cid"}, **fields), fh)
    return path


def test_a_sealed_client_secret_is_used():
    token = agent.local_seal("oauth-shhh", host=agent.ps_seal_host("https://ps.corp.internal"),
                             purpose=agent.LOCAL_PURPOSE_PS_CLIENT)
    ps = agent.PasswordSafe.from_file(_ps_file(client_secret_sealed=token))
    assert ps._secret == "oauth-shhh"


def test_a_sealed_client_secret_beats_both_plaintext_forms():
    token = agent.local_seal("oauth-shhh", host=agent.ps_seal_host("https://ps.corp.internal"),
                             purpose=agent.LOCAL_PURPOSE_PS_CLIENT)
    ps = agent.PasswordSafe.from_file(_ps_file(
        client_secret_sealed=token, client_secret="stale",
        client_secret_file="/nonexistent/would-raise-if-read"))
    assert ps._secret == "oauth-shhh", (
        "sealed must be resolved before a leftover file, or an unreadable leftover raises "
        "before the value actually in use is even looked at")


def test_every_api_url_form_seals_the_same():
    """passwordsafe.example.yaml accepts the bare host and the full v3 path, and
    `PasswordSafe.__init__` normalises by appending — so binding the raw string would make a
    value sealed against one form fail against the other for no visible reason.

    The port and the schemeless forms are the ones that got this wrong. `netloc` keeps
    `:443`, so `https://ps` and `https://ps:443` — the same server, written two legitimate
    ways — did not open each other's values. And with no scheme, `urlparse` reads
    `ps.corp.internal:443/…` as *scheme* `ps.corp.internal`, which left the binding as the
    string `'443'`: every such host collapsed onto its port number and the anti-relabel
    property was gone for that form.
    """
    forms = ["https://ps.corp.internal",
             "https://ps.corp.internal/BeyondTrust/api/public/v3",
             "https://ps.corp.internal:443",
             "https://PS.Corp.Internal./BeyondTrust/api/public/v3",
             "ps.corp.internal",
             "ps.corp.internal:443/BeyondTrust/api/public/v3"]
    resolved = {agent.ps_seal_host(f) for f in forms}
    assert resolved == {"ps.corp.internal"}, resolved


def test_a_sealed_value_in_the_plaintext_client_secret_is_refused():
    token = agent.local_seal("oauth-shhh", host=agent.ps_seal_host("https://ps.corp.internal"),
                             purpose=agent.LOCAL_PURPOSE_PS_CLIENT)
    try:
        agent.PasswordSafe.from_file(_ps_file(client_secret=token))
    except agent.PolicyRefusal as exc:
        assert "client_secret_sealed" in str(exc), exc
    else:
        raise AssertionError("expected a refusal")


def test_a_connection_password_cannot_be_used_as_a_client_secret():
    """The purpose binding, end to end through the two file parsers."""
    try:
        agent.PasswordSafe.from_file(_ps_file(client_secret_sealed=_sealed()))
    except agent.PolicyRefusal as exc:
        assert "did not authenticate" in str(exc), exc
    else:
        raise AssertionError("a hypervisor password must not open as a client secret")


# ── the seal subcommand ───────────────────────────────────────────────────────

def _run(argv, stdin=""):
    """Drive `main()` as the container's entrypoint does, capturing stdout."""
    saved = sys.argv, sys.stdin, sys.stdout
    sys.argv = ["agent.py"] + argv
    sys.stdin = io.StringIO(stdin)
    sys.stdout = captured = io.StringIO()
    try:
        code = agent.main()
    finally:
        sys.argv, sys.stdin, sys.stdout = saved
    return code, captured.getvalue()


def test_seal_prints_the_token_and_nothing_else_on_stdout():
    """So `seal … > value.txt` yields a pasteable line. Everything else goes to stderr."""
    code, out = _run(["seal", "--host", HOST], stdin="piped-secret\n")
    assert code == 0, out
    token = out.strip()
    assert "\n" not in token, f"stdout carried more than the token: {out!r}"
    assert agent.local_unseal(token, host=HOST,
                              purpose=agent.LOCAL_PURPOSE_PASSWORD) == "piped-secret"


def test_seal_api_url_produces_a_client_secret_token():
    code, out = _run(["seal", "--api-url", "https://ps.corp.internal/BeyondTrust/api/public/v3"],
                     stdin="oauth-shhh")
    assert code == 0, out
    assert agent.local_unseal(out.strip(), host="ps.corp.internal",
                              purpose=agent.LOCAL_PURPOSE_PS_CLIENT) == "oauth-shhh"


def test_seal_needs_exactly_one_address():
    assert _run(["seal"], stdin="x")[0] == 2
    assert _run(["seal", "--host"], stdin="x")[0] == 2
    assert _run(["seal", "--host", "a", "--api-url", "b"], stdin="x")[0] == 2
    assert _run(["seal", "--wat", "a"], stdin="x")[0] == 2


def test_seal_refuses_an_empty_value_rather_than_sealing_one():
    assert _run(["seal", "--host", HOST], stdin="\n")[0] == 2


def test_any_other_argument_is_refused_rather_than_ignored():
    """The container took no arguments before this, so a typo used to start a normal agent."""
    code, _ = _run(["--host", HOST])
    assert code == 2


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
    sys.exit(1 if failures else 0)
