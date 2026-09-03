"""The k8s token panel's repair control, and the gate that stranded a cluster twice.

Registration commits ``ps_token_account_id`` at step 2 and can then fail at step 4
(the fatal SyncedAccounts link), step 5 (the proving rotation) or step 6 (deleting the
legacy Secret). From the moment step 2 lands, ``ps_token_managed`` is true — and that
flag **hides the register form**. So the control that re-runs the register is the only
route back for every one of those failures, and re-POSTing ``…/ps-token`` is idempotent
and documented as exactly that repair path.

The gate on that control has now been wrong twice, both times by trying to enumerate
which half-state is repairable:

  1. it also required ``pravault_account_id``, which stranded a cluster whose PRA tunnel
     was registered AFTER its token (no subscriber account exists yet, so there was
     nothing to gate on and no button);
  2. it required ``!psTokenStatus.linked``, which stranded a cluster that linked fine
     and died at the rotation. That happened live on AKS twice: the rotator held no
     Azure data-plane role, so step 5 got a 403, and the only controls left were
     "Rotate now" (which re-hits the same 403 and applies no RBAC) and "Remove".

Enumerating is the bug, so this file pins the absence of the enumeration rather than any
particular condition. It also pins the key-drift class the PRA picker test describes:
the markup reads nine ``psTokenStatus.*`` keys and reading an absent one is not an error
in JavaScript — ``x-show="psTokenStatus.vault_has_real_credential"`` on a key nobody
serves is silently always false, which would hide a warning rather than break a page.

Reads source text only; imports nothing from the app. Runs under pytest or standalone:
    python tests/test_k8s_repair_gate.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL = os.path.join(_ROOT, "web_dashboard", "templates", "k8s", "index.html")
_TOKENSVC = os.path.join(_ROOT, "web_dashboard", "services", "ps_k8s_token_service.py")
_PSAPI = os.path.join(_ROOT, "web_dashboard", "services", "ps_api_service.py")

_REPAIR_HANDLER = "repairPsTokenRegistration"


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _repair_block():
    """The ``<template x-if=...>`` wrapping the repair button, gate included."""
    tpl = _src(_TPL)
    click = tpl.index(f"{_REPAIR_HANDLER}()")
    start = tpl.rindex("<template x-if=", 0, click)
    end = tpl.index("</template>", click)
    return tpl[start:end]


def test_the_repair_control_exists_and_is_wired_to_a_handler():
    tpl = _src(_TPL)
    assert f'@click="{_REPAIR_HANDLER}()"' in tpl, "no repair control in the token panel"
    assert f"async {_REPAIR_HANDLER}()" in tpl, (
        f"the button calls {_REPAIR_HANDLER}() but the Alpine component does not define "
        "it — an undefined handler throws where only the console sees it")


def test_the_repair_gate_asks_only_whether_there_is_a_registration():
    """The fix, and the thing that must not regress. Any of these in the gate means
    somebody has started enumerating repairable half-states again, and some other
    half-state is now stranded with no control."""
    gate = _repair_block().split(">")[0]
    assert "ps_token_managed" in gate, (
        "the repair control must be offered for a registered cluster — that is the "
        "state in which the register form is hidden")
    for forbidden in ("linked", "pravault_account_id", "pra_vault_present",
                      "vault_has_real_credential"):
        assert forbidden not in gate, (
            f"the repair gate tests {forbidden!r}. Registration can fail at the link, "
            "the rotation OR the Secret delete, and re-POSTing …/ps-token repairs all "
            "three; gating on one of them strands the others with no control at all. "
            "Use it to COLOUR the button, not to decide whether it exists.")


def test_the_repair_gate_does_not_depend_on_password_safe_being_reachable():
    """``psTokenStatus`` is a live Password Safe read. "Password Safe is unreachable" is
    a reason to repair, not a reason the repair is unavailable — and the status read is
    explicitly documented as never blocking the modal from opening."""
    gate = _repair_block().split(">")[0]
    assert "psTokenStatus" not in gate, (
        "the repair gate depends on the live status read, so a Password Safe outage "
        "(or a slow first paint, since psTokenStatus starts null) hides the control")


def test_the_button_is_still_allowed_to_look_urgent():
    """Dropping the gate must not drop the signal. A cluster that is unlinked or whose
    vault never got a real credential should still read as needing attention."""
    block = _repair_block()
    assert ":class=" in block, "the repair button lost its conditional styling"
    assert "red" in block, (
        "nothing marks the repair button as urgent for a cluster that actually needs it")


# ── the status keys the markup reads ─────────────────────────────────────────────

def _served_status_keys():
    """Every key ``sync_status`` can put in its dict, including what it merges in from
    ``ps_api_service.synced_account_status``."""
    svc = _src(_TOKENSVC)
    start = svc.index("async def sync_status(")
    end = svc.index("\nasync def ", start + 10)
    body = svc[start:end]
    keys = set(re.findall(r'"([a-z_]+)":', body))
    keys |= set(re.findall(r'out\["([a-z_]+)"\]', body))

    # sync_status does out.update(await synced_account_status(...)), so those keys are
    # served too — and they live in another module, which is exactly how a rename there
    # would silently empty this panel.
    api = _src(_PSAPI)
    astart = api.index("async def synced_account_status(")
    aend = api.index("\nasync def ", astart + 10)
    keys |= set(re.findall(r'"([a-z_]+)":', api[astart:aend]))
    return keys


def test_every_status_key_the_markup_reads_is_one_the_api_serves():
    """Same defect class as the PRA picker drift: a template reading an absent key
    renders as "false"/"—" rather than failing, so a rename on the server silently
    blanks a diagnostic instead of breaking anything visibly."""
    read = set(re.findall(r"psTokenStatus\.([a-z_]+)", _src(_TPL)))
    served = _served_status_keys()
    missing = sorted(read - served)
    assert not missing, (
        f"the token panel reads psTokenStatus keys that sync_status never returns: "
        f"{missing}. In JavaScript that is silently falsy, so the panel keeps rendering "
        f"and simply stops telling the operator anything. Served: {sorted(served)}")


def test_the_placeholder_state_is_served_and_surfaced():
    """The AKS failure's signature: registered, linked, and the vault still holding the
    create-time placeholder because the proving rotation was refused. Password Safe
    cannot show this — the account exists and has a password either way — so it comes
    from the dashboard's own stored state, on the same "seeded OR rotated" test
    ``register`` uses to decide the refill."""
    svc = _src(_TOKENSVC)
    start = svc.index("async def sync_status(")
    body = svc[start:svc.index("\nasync def ", start + 10)]
    assert "vault_has_real_credential" in body, (
        "sync_status no longer reports whether anything ever put a real credential in "
        "the vault")
    compact = "".join(body.split())
    assert 'st.get("seeded")orst.get("rotated")' in compact, (
        "the test must be seeded OR rotated, matching register()'s refill decision — a "
        "short credential that was genuinely seeded and left unrotated is fine")
    assert "psTokenStatus.vault_has_real_credential" in _src(_TPL), (
        "the panel never tells the operator the vault holds a dead placeholder")


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
