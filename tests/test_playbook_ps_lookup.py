"""Invariants for the optional Password Safe lookup in the sample playbooks.

Several shipped playbooks can OPTIONALLY source a secret from BeyondTrust Password
Safe via `beyondtrust.secrets_safe.secrets_safe_lookup`. The pattern is repeated
across files, so these assertions pin the parts that would otherwise rot silently:

  * the four `ps_*` connection vars are declared (the lookup is passed them
    explicitly — it does not read the environment itself);
  * every task carrying the lookup sets `no_log: true`, so a retrieved value can
    never reach job output;
  * the lookup is guarded by a `when:` — it must stay OPTIONAL, so a play without a
    Password Safe path behaves exactly as before;
  * the fetch writes to a private `_ps_*` fact, never back onto the caller-supplied
    var. Ansible extra vars outrank `set_fact`, so the latter would be SILENTLY
    ignored whenever the value was also supplied — the play would use the wrong one;
  * `api_version: '3.1'` everywhere, and `decrypt=True` only with
    `retrieval_type='SECRET'` (MANAGED_ACCOUNT omits it).

Run: python tests/test_playbook_ps_lookup.py   (or under pytest)
"""
import glob
import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLAYBOOKS = sorted(glob.glob(
    os.path.join(_ROOT, "examples", "playbooks", "**", "*.yml"), recursive=True))

_LOOKUP = "beyondtrust.secrets_safe.secrets_safe_lookup"
_PS_VARS = ("ps_api_url", "ps_client_id", "ps_client_secret", "ps_verify_ca")


def _rel(path):
    return os.path.relpath(path, _ROOT)


def _plays_with_lookup():
    """(path, play) for every play whose raw text references the lookup."""
    out = []
    for path in _PLAYBOOKS:
        text = open(path).read()
        if _LOOKUP not in text:
            continue
        for play in (yaml.safe_load(text) or []):
            if _LOOKUP in yaml.safe_dump(play):
                out.append((path, play))
    return out


def _lookup_tasks(play):
    return [t for t in (play.get("tasks") or []) if _LOOKUP in yaml.safe_dump(t)]


def _is_optional_rollout(path):
    """The plays under password-safe/ are dedicated Password Safe DEMOS — the lookup is
    their whole point, so it runs unconditionally and may set the caller's var directly.
    The invariants below apply to the plays where PS is an OPTIONAL alternative."""
    return os.sep + "password-safe" + os.sep not in path


def test_some_playbooks_use_the_lookup():
    """Guard against the whole suite silently becoming a no-op."""
    found = _plays_with_lookup()
    assert found, "no playbook references the Password Safe lookup"


def test_ps_connection_vars_are_declared():
    for path, play in _plays_with_lookup():
        declared = play.get("vars") or {}
        for var in _PS_VARS:
            assert var in declared, f"{_rel(path)}: play does not declare '{var}'"


def test_every_lookup_task_sets_no_log():
    for path, play in _plays_with_lookup():
        for task in _lookup_tasks(play):
            assert task.get("no_log") is True, (
                f"{_rel(path)}: task {task.get('name')!r} runs the lookup without no_log")


def test_lookup_is_optional():
    """A `when:` guard is what keeps the Password Safe path opt-in."""
    for path, play in _plays_with_lookup():
        if not _is_optional_rollout(path):
            continue
        for task in _lookup_tasks(play):
            assert task.get("when"), (
                f"{_rel(path)}: task {task.get('name')!r} runs the lookup "
                f"unconditionally — it must be guarded by a `when:` on its path var")


def test_fetch_targets_a_private_fact():
    """Never set_fact back onto the caller-supplied var — extra vars outrank
    set_fact, so it would be silently ignored when both are supplied."""
    for path, play in _plays_with_lookup():
        if not _is_optional_rollout(path):
            continue
        for task in _lookup_tasks(play):
            facts = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
            assert facts, f"{_rel(path)}: lookup task is not a set_fact"
            for name in facts:
                assert name.startswith("_ps_"), (
                    f"{_rel(path)}: lookup writes to {name!r}; it must write to a "
                    f"private '_ps_*' fact and be resolved at the use site")


def test_api_version_and_decrypt_flags():
    """Checked against the RAW file text: yaml.safe_dump doubles the single quotes
    inside the lookup expression, so round-tripped YAML can't be string-matched."""
    seen = 0
    for path in _PLAYBOOKS:
        text = open(path).read()
        if _LOOKUP not in text:
            continue
        seen += 1
        assert "api_version='3.1'" in text, (
            f"{_rel(path)}: lookup does not pin api_version='3.1'")
        if "retrieval_type='SECRET'" in text:
            assert "decrypt=True" in text, (
                f"{_rel(path)}: SECRET retrieval without decrypt=True")
        if "retrieval_type='MANAGED_ACCOUNT'" in text and "retrieval_type='SECRET'" not in text:
            assert "decrypt=" not in text, (
                f"{_rel(path)}: MANAGED_ACCOUNT retrieval must omit decrypt")
    assert seen, "no playbook text contained the lookup"


def test_no_playbook_templates_a_secret_without_no_log():
    """Any task interpolating a *_password / *_pat / *_token variable into a module
    must no_log. Catches the class of bug fixed alongside the lookup rollout, and the
    swarm join/store tasks that carry a cluster join token."""
    pat = re.compile(r"\{\{\s*_?[a-z_]*(password|pat|token)\b")
    offenders = []
    for path in _PLAYBOOKS:
        for play in (yaml.safe_load(open(path).read()) or []):
            for task in (play.get("tasks") or []):
                # only module args matter; `when:`/`assert` referencing a name is fine
                args = {k: v for k, v in task.items()
                        if k not in ("name", "when", "no_log", "register", "loop",
                                     "loop_control", "ansible.builtin.assert", "assert",
                                     "ansible.builtin.debug", "debug")}
                if pat.search(yaml.safe_dump(args)) and task.get("no_log") is not True:
                    offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "secret templated into a module without no_log:\n  " + "\n  ".join(offenders)



def _all_plays():
    for path in _PLAYBOOKS:
        for play in (yaml.safe_load(open(path).read()) or []):
            yield path, play


def _beyondtrust_module_tasks(play):
    """Tasks invoking a beyondtrust MODULE (not the lookup plugin)."""
    for task in (play.get("tasks") or []):
        if any(k.startswith("beyondtrust.") for k in task):
            yield task


def test_beyondtrust_modules_are_delegated_to_the_controller():
    """The lookup PLUGIN runs on the controller, but beyondtrust MODULES run on the
    target — which has no beyondtrust-bips-library; only the runner container does.
    Any such task in a `hosts: all` play must delegate to localhost or it dies with an
    import error. (A `hosts: localhost` play is already on the controller.)"""
    offenders = []
    for path, play in _all_plays():
        if play.get("hosts") == "localhost":
            continue
        for task in _beyondtrust_module_tasks(play):
            if task.get("delegate_to") != "localhost":
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, (
        "beyondtrust module in a remote play without delegate_to: localhost:\n  "
        + "\n  ".join(offenders))


def test_beyondtrust_write_tasks_are_not_logged():
    """EVERY beyondtrust module call passes the OAuth `client_secret` as a module
    arg — not just the ones writing a payload secret — so they all need no_log."""
    offenders = []
    for path, play in _all_plays():
        for task in _beyondtrust_module_tasks(play):
            if task.get("no_log") is not True:
                offenders.append(f"{_rel(path)}: {task.get('name')!r}")
    assert not offenders, "beyondtrust write task without no_log:\n  " + "\n  ".join(offenders)


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
