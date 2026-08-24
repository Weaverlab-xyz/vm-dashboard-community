"""policy.yaml is the security boundary, so the WRONG SHAPE must be as loud as unparseable.

Everything else about this file already fails closed: missing is fatal, unreadable is
fatal, invalid YAML is fatal, a bad CIDR is fatal. The gap was the shape in between —
valid YAML that is not a target list. ``_parse_targets`` used to iterate anything and
``continue`` past whatever was not a dict, which turns a typo into a grant that quietly
does not exist:

    ansible:
      targets:
        -cidr: 192.168.68.0/24        # no space after the dash
        -cidr: 192.168.68.100/32

That is valid YAML for a MAPPING whose keys are the string ``-cidr`` — not a list of two
entries. The old parser iterated the mapping, got strings, skipped them all, and returned
``[]``. Observed live on 2026-08-24: every Config-Management job was then refused with
"policy.yaml does not allow Config Management against 192.168.68.100:22 — add it under
`ansible.targets:`", pointing the operator at a line they had already written, while
PyYAML's last-wins duplicate-key rule had additionally thrown away the first of the two
without a word. Nothing anywhere reported a problem.

So three properties, each of which is the whole point of the file:

  * A ``targets:`` that is not a list is FATAL, and the message names the missing space —
    the odd key (``-cidr``) is the fingerprint, so it gets echoed back.
  * An entry the agent cannot read as a target is FATAL rather than skipped. A skipped
    entry is worse than a rejected one: the agent starts, the digest matches, and the
    grant is simply absent.
  * A duplicated key is FATAL rather than last-wins. In a file where a key IS a grant,
    silently keeping only the last copy loses one the operator wrote.

Standalone or under pytest:
    python tests/test_agent_policy_shape.py
"""
import importlib.util
import os
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

_spec = importlib.util.spec_from_file_location("agent", _AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
sys.modules["agent"] = agent
_spec.loader.exec_module(agent)


def _policy(doc: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(textwrap.dedent(doc))
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


def _fatal(doc: str) -> str:
    """The AgentFatal message for a policy, or an assertion failure if it loaded."""
    try:
        pol = _policy(doc)
    except agent.AgentFatal as exc:
        return str(exc)
    raise AssertionError(
        f"a malformed policy loaded instead of failing closed "
        f"(targets={pol.allow}, ansible.targets={pol.ansible_allow})")


# ── the live failure: `-cidr:` with no space after the dash ───────────────────

def test_the_missing_space_after_the_dash_is_fatal_under_ansible_targets():
    """The observed case, minus the duplicate: one `-cidr` key instead of a list.

    The old parser returned [] here, which reads downstream as "the operator granted no
    Config-Management targets" — indistinguishable from an operator who wrote nothing.
    """
    msg = _fatal("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: chrweav/ansible-winrm:latest
          targets:
            -cidr: 192.168.68.0/24
        """)
    assert "ansible.targets" in msg, f"must name which list is wrong: {msg}"
    assert "-cidr" in msg, f"the offending key is the fingerprint, echo it: {msg}"
    assert "dash" in msg, f"must name the missing space after the dash: {msg}"


def test_the_same_typo_in_the_top_level_targets_is_fatal_and_says_why():
    """`targets:` had a second line of defence — "declares no targets" — but that message
    describes a file with no targets in it, which is not what the operator is looking at.
    It has to be about the shape, or they read it as the agent being wrong."""
    msg = _fatal("targets:\n  -cidr: 192.168.68.0/24\n")
    assert "`targets`" in msg, msg
    assert "-cidr" in msg and "dash" in msg, msg
    assert "declares no targets" not in msg, f"the vague message won the race: {msg}"


def test_the_exact_live_shape_two_dash_keys_still_names_the_missing_space():
    """PyYAML sees two `-cidr` keys in one mapping and keeps the last, so this trips the
    duplicate-key guard BEFORE the shape guard. That message must not send the operator
    off to merge two entries — the mistake is still the space."""
    msg = _fatal("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: chrweav/ansible-winrm:latest
          targets:
            -cidr: 192.168.68.0/24
            -cidr: 192.168.68.100/32
        """)
    assert "-cidr" in msg and "dash" in msg, msg
    assert "Merge them into a single" not in msg, f"wrong remedy for this shape: {msg}"


def test_a_targets_that_is_a_bare_string_is_fatal():
    """A string is iterable too, so the old loop walked its characters and skipped every
    one of them — the same silent empty list by a different route."""
    msg = _fatal("targets: 10.20.0.0/24\n")
    assert "`targets`" in msg and "list" in msg, msg


# ── an entry the parser cannot read is refused, not skipped ───────────────────

def test_a_non_mapping_entry_is_fatal_rather_than_skipped():
    msg = _fatal("targets:\n  - 10.20.0.0/24\n")
    assert "10.20.0.0/24" in msg, f"must quote what it could not read: {msg}"
    assert "cidr" in msg, msg


def test_an_entry_naming_neither_cidr_nor_fqdn_is_fatal():
    """`- ports: [22]` matches no host at all. Skipping it left the rest of the list
    working, so the file looked fine and one range was missing."""
    msg = _fatal("""
        targets:
          - cidr: 10.20.0.0/24
          - ports: [22]
        """)
    assert "`targets`" in msg and "fqdn" in msg, msg
    assert "ports" in msg, f"must say which entry: {msg}"


def test_a_skipped_entry_cannot_hide_behind_a_good_one():
    """The property in one line: a policy naming two targets never yields one."""
    for bad in ("- 192.168.68.0/24", "- name: dc1", "- cidr:"):
        _fatal(f"targets:\n  - cidr: 10.20.0.0/24\n  {bad}\n")


def test_a_scalar_ports_is_fatal_because_it_used_to_WIDEN_the_grant():
    """The one typo in here that failed OPEN. `ports: 22` is not a list, so the old
    parser stored None — and None means every port in the range, which is the opposite
    of what someone naming a port is asking for."""
    msg = _fatal("targets:\n  - cidr: 10.20.0.0/24\n    ports: 22\n")
    assert "ports" in msg and "[22]" in msg, msg
    assert "EVERY port" in msg, f"must say which way it failed: {msg}"


def test_a_non_numeric_port_is_a_message_not_a_traceback():
    msg = _fatal("targets:\n  - cidr: 10.20.0.0/24\n    ports: [ssh]\n")
    assert "ports" in msg and "22" in msg, msg


# ── duplicate keys ────────────────────────────────────────────────────────────

def test_a_duplicated_key_is_fatal_rather_than_last_wins():
    """Two `targets:` blocks: PyYAML keeps the second and discards the first in silence.
    In a file where a key is a grant, that is a range the operator believes they have."""
    msg = _fatal("""
        targets:
          - cidr: 10.1.0.0/24
        targets:
          - cidr: 10.2.0.0/24
        """)
    assert "`targets`" in msg and "twice" in msg, msg
    # Both line numbers, so the operator can find the copy they forgot about.
    assert "line 2" in msg and "line 4" in msg, f"must locate both copies: {msg}"


def test_a_duplicate_nested_inside_the_ansible_block_is_caught_too():
    msg = _fatal("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: a
          vm_image: b
        """)
    assert "vm_image" in msg and "twice" in msg, msg


def test_a_yaml_merge_override_is_not_a_duplicate():
    """`<<:` exists to be overridden by the keys beside it. Rejecting that would break
    valid YAML to catch a typo, which is not a trade this file gets to make."""
    pol = _policy("""
        _base: &base
          ports: [22]
        targets:
          - <<: *base
            cidr: 10.20.0.0/24
            ports: [443]
        """)
    assert pol.allows("10.20.0.5", 443)
    assert not pol.allows("10.20.0.5", 22), "the override must win, as YAML says"


# ── and the shapes that were always legal still are ───────────────────────────

def test_a_well_formed_policy_is_unaffected():
    pol = _policy("""
        targets:
          - cidr: 10.20.0.0/24
            ports: [443, 8006]
          - cidr: 10.30.0.0/16
        ansible:
          enabled: true
          vm_image: chrweav/ansible-winrm:latest
          targets:
            - cidr: 192.168.68.0/24
              ports: [22]
        """)
    assert pol.allows("10.20.0.5", 443)
    assert not pol.allows("10.20.0.5", 22), "a named port list still excludes"
    assert pol.allows("10.30.9.9", 1521), "omitted ports still means any port"
    pol.check_ansible("192.168.68.100", 22)          # the grant survives the round trip


def test_an_absent_or_empty_targets_keeps_its_own_message():
    """Both were already fatal, and they are a different mistake from a bad shape —
    "you wrote nothing" must not be reworded into "you wrote it wrong"."""
    for doc in ("job_types: [agent_discover]\n", "targets: []\n"):
        assert "declares no targets" in _fatal(doc)


def test_an_ansible_block_with_no_targets_is_still_simply_empty():
    """Absent stays absent: fail-closed here is an empty ansible_allow, not a refusal to
    start — an operator may enable the block before filling in the ranges."""
    pol = _policy("""
        targets:
          - cidr: 10.20.0.0/24
        ansible:
          enabled: true
          vm_image: x
        """)
    assert pol.ansible_allow == []
    try:
        pol.check_ansible("10.20.10.5", 22)
        raise AssertionError("an empty ansible.targets granted a run")
    except agent.PolicyRefusal:
        pass


def test_these_are_startup_failures_not_job_refusals():
    """AgentFatal exits the process with the message. PolicyRefusal fails one job and the
    agent keeps running — which for a misshapen file means it keeps running wrong."""
    assert issubclass(agent.AgentFatal, Exception)
    assert not issubclass(agent.AgentFatal, agent.PolicyRefusal)
    assert not issubclass(agent.PolicyRefusal, agent.AgentFatal)


def test_the_parser_has_no_silent_skip_left_in_it():
    """A source check, because the lenient `continue` reads as harmless tidying and the
    behaviour it restores is invisible in every other test."""
    src = open(_AGENT_PATH, encoding="utf-8").read()
    body = src.split("def _parse_targets", 1)[1].split("\n    @classmethod", 1)[0]
    assert "continue" not in body, "a skipped target is a grant that silently vanishes"
    assert "yaml.safe_load(raw)" not in src, "policy.yaml must use the strict loader"


def test_the_shipped_example_policy_still_loads():
    """It is what every operator starts from, so it has to pass the stricter parser."""
    pol = agent.Policy.load(os.path.join(_ROOT, "examples", "remote-agent",
                                         "policy.example.yaml"))
    assert pol.allow, "the example must grant something"


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
