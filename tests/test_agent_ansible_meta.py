"""Invariants for agent-executed Config-Management metadata.

``agent_job_meta`` guards a discovery payload; this guards a payload that ends in
``ansible-playbook``, so it is the stricter of the two boundaries in the same codebase.
Two allowlists do two different jobs and the split is what is being pinned:

  * ``RUN_META_KEYS`` is the job ROW — refs, never values, because it lands in the
    database (``ansible_run_meta``'s rule);
  * ``ENVELOPE_KEYS`` is the WIRE — scalars, enums and a network address, because a
    compromised dashboard must not be able to name a playbook, a file or a variable
    (``agent_job_meta``'s rule).

Collapse those two into one and the feature either puts a playbook in a signed-but-not-
sealed envelope, or puts a credential in the database. The assertions here are what make
that a test failure rather than a design drift nobody notices.

Pure module, stdlib only. Runs under pytest, or standalone:
    python tests/test_agent_ansible_meta.py
"""
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "agent_ansible_meta.py")
_spec = importlib.util.spec_from_file_location("agent_ansible_meta", _PATH)
aam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aam)


class _Payload:
    """A stand-in for RunRequest — read with getattr, so a plain object is equivalent."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _vm_meta(**over):
    base = dict(run_kind="vm", transport="ssh", target_host="10.20.10.5",
                connection_id="c-1", target_id="vm-1", target_label="web01")
    base.update(over)
    return aam.run_meta(_Payload(asset="p.yml"), description="d",
                        asset_backend="s3", **base)


# ── the two allowlists ────────────────────────────────────────────────────────

def test_the_envelope_is_a_strict_subset_of_the_job_row():
    """A field can only reach the agent if the job row also declares it. Otherwise the
    envelope would carry something no call site ever validated."""
    assert set(aam.ENVELOPE_KEYS) <= set(aam.RUN_META_KEYS)


def test_no_envelope_field_can_carry_executable_content():
    """The mirror of agent_job_meta's own audit. A playbook, a filename, a variable name
    or a URL in the envelope would be signed but NOT sealed — and signed means only "the
    dashboard sent it", which is exactly the trust this design refuses to extend."""
    banned = ("asset", "playbook", "script", "command", "cmd", "url", "path", "file",
              "image", "var", "secret", "key", "account", "user", "backend")
    for field in aam.ENVELOPE_KEYS:
        for word in banned:
            assert word not in field, (
                f"envelope field {field!r} contains {word!r} — the envelope is scalars, "
                f"enums and network addresses only; anything else rides the sealed bundle")


def test_the_envelope_is_exactly_the_four_scalars():
    """Spelled out, so widening the wire is a deliberate edit here as well as there."""
    assert aam.ENVELOPE_KEYS == ("run_kind", "transport", "target_host", "target_port")


def test_the_job_row_declares_a_default_for_every_key():
    """run_kwargs falls back to _DEFAULTS, so a key without one would KeyError on a job
    queued by an older build."""
    assert set(aam.RUN_META_KEYS) == set(aam._DEFAULTS)


def test_the_string_coercion_list_stays_inside_the_allowlist():
    assert set(aam._STRING_KEYS) <= set(aam.RUN_META_KEYS)


def test_the_job_row_carries_refs_never_values():
    """The names are the assertion: a field named for a value — password, token,
    private_key — would mean a credential in the database, which is the boundary
    ansible_run_meta exists to hold.

    The suffixes are the exemption and they carry real meaning rather than softening the
    rule: ``_var`` is the NAME of an Ansible variable, ``_source`` and ``_ref`` are pointers
    into a secret store. ``epml_token_var`` is the case that proves it — it names the
    variable an EPM-L token will be bound to, and the token itself is minted at run time and
    never written anywhere.
    """
    ref_suffixes = ("_var", "_source", "_ref")
    for field in aam.RUN_META_KEYS:
        if field.endswith(ref_suffixes):
            continue
        for word in ("password", "token", "private_key", "credential", "pem", "secret_value"):
            assert word not in field, f"job metadata field {field!r} looks like a value"


# ── projections ───────────────────────────────────────────────────────────────

def test_the_asset_name_never_reaches_the_agent():
    """The dashboard reads the asset out of storage and seals the bytes. Sending the NAME
    would hand an agent a string to fetch, which is the shape of every remote-file bug."""
    meta = _vm_meta()
    assert meta["asset"] == "p.yml"
    assert "asset" not in aam.envelope_payload(meta)


def test_extra_vars_never_reach_the_agent_unsealed():
    meta = _vm_meta()
    meta["extra_vars"] = {"role": "web"}
    assert "extra_vars" not in aam.envelope_payload(meta)


def test_the_envelope_round_trips_the_scalars():
    env = aam.envelope_payload(_vm_meta(transport="winrm", target_port=5986))
    assert env == {"run_kind": "vm", "transport": "winrm",
                   "target_host": "10.20.10.5", "target_port": 5986}


def test_run_kwargs_restores_every_key_from_an_older_row():
    """A row written before a field existed must resume the way that build would have,
    not fail — the same tolerance ansible_run_meta.run_kwargs documents."""
    out = aam.run_kwargs({"run_kind": "vm"})
    assert set(out) == set(aam.RUN_META_KEYS)
    assert out["extra_vars"] == {}


def test_run_kwargs_never_hands_back_the_shared_default():
    a = aam.run_kwargs({})
    a["extra_vars"]["x"] = 1
    assert aam.run_kwargs({})["extra_vars"] == {}, "a caller mutated the shared default"


# ── normalize + check ─────────────────────────────────────────────────────────

def test_a_database_run_is_always_a_localhost_play():
    """transport is forced, not trusted: a database run that SSHed to the DB endpoint
    would authenticate with a DB credential against sshd."""
    meta = _vm_meta(run_kind="database", transport="ssh", target_port=5432,
                    target_id="db-1")
    assert meta["transport"] == "local"


def test_the_port_defaults_per_transport_not_globally():
    """The bug this pins: a default of 22 in _DEFAULTS survives normalize(), so a WinRM
    run whose caller sent no port is silently aimed at 22."""
    assert _vm_meta(transport="ssh")["target_port"] == 22
    assert _vm_meta(transport="winrm")["target_port"] == 5985
    assert _vm_meta(transport="winrm", target_port=5986)["target_port"] == 5986


def test_an_unknown_kind_or_transport_is_refused_not_defaulted():
    """Unlike a discovery scan, a config run must not quietly do something adjacent."""
    assert "run_kind must be one of" in aam.check({"run_kind": "shell",
                                                   "target_host": "h"})
    assert "transport must be one of" in aam.check({"run_kind": "vm",
                                                   "transport": "telnet",
                                                   "target_host": "h"})


def test_the_refusal_names_the_value_that_was_sent():
    """normalize() blanks a bad enum, so check() has to read the raw value or the message
    says "got ''" to somebody who sent "shell"."""
    assert "'shell'" in aam.check({"run_kind": "shell", "target_host": "h"})


def test_a_target_with_no_address_is_refused_with_the_reason():
    reason = aam.check(_vm_meta(target_host=""))
    assert "no address" in reason and "guest-detail" in reason


def test_a_vm_run_must_name_its_connection():
    """The connection is what proves the VM came from an agent-bound sync; without it the
    bundle route has nothing to authorize against."""
    assert "agent-bound connection" in aam.check(_vm_meta(connection_id=""))


def test_a_database_run_must_name_its_database():
    assert "must name the database" in aam.check(
        _vm_meta(run_kind="database", target_id="", target_port=5432))


def test_a_well_formed_run_passes():
    assert aam.check(_vm_meta()) == ""
    assert aam.check(_vm_meta(run_kind="database", target_id="db-1",
                             target_port=5432)) == ""


# ── transport from the guest OS ───────────────────────────────────────────────

def test_windows_guests_get_winrm_whatever_the_hypervisor_called_them():
    """Three vocabularies reach this: a Hyper-V KVP marketing string, a vSphere family,
    and a vSphere guest-OS code."""
    for reported in ("Windows Server 2022 Datacenter", "WINDOWS",
                     "windows9Server64Guest", "Microsoft Windows 11"):
        assert aam.transport_for_guest_os(reported) == "winrm", reported


def test_non_windows_guests_get_ssh_including_the_darwin_trap():
    """`Darwin` contains "win", which is why the match is on "windows"."""
    for reported in ("Ubuntu 22.04.3 LTS", "LINUX", "rhel9_64Guest", "Darwin", "", None):
        assert aam.transport_for_guest_os(reported) == "ssh", reported


# ── the module is loadable without the app ────────────────────────────────────

def test_the_module_imports_nothing_from_the_app():
    """Stdlib only, so the boundary is testable without FastAPI or a database — the same
    property agent_job_meta and ansible_run_meta keep."""
    src = open(_PATH, encoding="utf-8").read()
    for line in src.splitlines():
        if re.match(r"\s*(from|import)\s", line):
            assert "web_dashboard" not in line and not line.strip().startswith("from ."), \
                f"agent_ansible_meta must stay app-free: {line.strip()!r}"


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
