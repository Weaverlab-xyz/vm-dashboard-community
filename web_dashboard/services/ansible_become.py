"""Which privilege-escalation method a Config-Management run uses.

Ansible defaults to ``sudo`` when a play says ``become: true`` and nothing names a
method. That default is wrong on any host where escalation is brokered — a BeyondTrust
Privilege Management for Unix & Linux (PMUL) host escalates through ``pbrun``, and an
account entitled there usually has no sudoers entry at all. The failure is unhelpful:
sudo prompts, the prompt lands on the module's stdout, and Ansible reports
``Module result deserialization failed: No start of json char found`` rather than
"this account cannot sudo".

**Only the method NAME travels.** Ansible's become plugins also take ``become_exe`` and
``become_flags``, which are command fragments the TARGET executes as root; neither is
exposed here and neither may be added. A plugin name is looked up in Ansible's own
plugin table and cannot become a command, which is the whole reason this is a closed
allowlist of names rather than a free-text field.

Mirrored by ``runners.agent.agent._BECOME_METHODS`` — the agent re-checks the value
rather than trusting the dashboard that sent it, on the same principle as
``_check_extra_vars``. The two lists are pinned to each other by
tests/test_ansible_become.py, because a method the dashboard offers and the agent
refuses would be a run that fails only on agent-executed targets.

Pure and stdlib-only, so it is testable without FastAPI or a database.
"""

# Ansible become plugins worth offering for the targets this dashboard manages.
# `""` is not in here: empty means "say nothing and let Ansible default", which is what
# every run created before this field existed must keep doing.
BECOME_METHODS = (
    "sudo",         # the Ansible default
    "pbrun",        # BeyondTrust Privilege Management for Unix & Linux
    "pmrun",        # BeyondTrust Privilege Management, the pmrun submit binary
    "doas",
    "dzdo",         # Delinea/Centrify
    "pfexec",       # Solaris
    "su",
    "ksu",
    "sesu",
    "machinectl",
    "runas",        # Windows
)

# The var Ansible reads. Set server-side into the TRUSTED var channel on both runner
# paths, never accepted from an operator: `ansible_*` is connection configuration, and
# the agent refuses the whole prefix coming from the dashboard for that reason.
BECOME_METHOD_VAR = "ansible_become_method"


class BecomeError(ValueError):
    """An unusable become method. Carries a message meant for an operator."""


def normalize(value) -> str:
    """The method as it should be stored, or ``""`` for "use Ansible's default".

    Raises :class:`BecomeError` naming the allowed values, because the alternative —
    dropping an unrecognised method — is a run that silently escalates by sudo on a host
    where that is exactly what the operator was trying to avoid.
    """
    method = str(value or "").strip().lower()
    if not method:
        return ""
    if method not in BECOME_METHODS:
        raise BecomeError(
            f"{method!r} is not a supported become method. Use one of: "
            f"{', '.join(BECOME_METHODS)}.")
    return method


def apply_to(play_vars: dict, method: str) -> dict:
    """Set the become-method var on ``play_vars`` in place, and return it.

    A no-op for ``""`` so an unset field leaves Ansible's own default alone rather than
    pinning every existing run to sudo explicitly.
    """
    method = normalize(method)
    if method:
        play_vars[BECOME_METHOD_VAR] = method
    return play_vars
