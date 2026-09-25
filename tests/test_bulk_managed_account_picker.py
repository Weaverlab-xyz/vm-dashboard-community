"""Static assertions on the BULK managed-account picker, across two templates.

A bulk run used to read its Password Safe account list from ONE sample host and apply
the chosen account to every selected object. The fix replaces that with a per-target
table fed by ``POST /api/config-mgmt/bulk-managed-accounts``.

What is pinned here is the set of joins that rot SILENTLY — nothing throws, the page
still renders, and the operator gets the old behaviour back:

  * The writer of the hand-off (``inventory/list.html``) and its reader
    (``config-mgmt/index.html``) are different files. The sample host lived in that
    gap, which is why its absence is asserted in BOTH rather than only where it was
    used.
  * An ``@change`` / ``x-show`` naming a method the component does not define is a
    dead control: Alpine logs and moves on. This file caught exactly that during
    development (``resuggestBulkAccounts`` referenced twice, defined nowhere).
  * The ``<option value>`` format and the parser that splits it live ~600 lines apart.

Pure text/AST assertions — no browser, no DB, no app. There is no JS engine on the
dev machine (``test_template_scripts.py`` skips its parse for want of node), so these
checks are the local safety net for this page.

Runs under pytest, or standalone:
    python tests/test_bulk_managed_account_picker.py
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG = os.path.join(_ROOT, "web_dashboard", "templates", "config-mgmt", "index.html")
_INV = os.path.join(_ROOT, "web_dashboard", "templates", "inventory", "list.html")
_API = os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py")


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


CFG = _read(_CFG)
INV = _read(_INV)
API = _read(_API)


# ── the sample host is gone from BOTH sides of the hand-off ────────────────────

def test_the_sample_host_is_gone_from_both_templates():
    """The writer is inventory/list.html and the reader is config-mgmt/index.html.
    Leaving either half behind is how the old path stays quietly reachable."""
    for name, src in (("inventory/list.html", INV),
                      ("config-mgmt/index.html", CFG)):
        assert "sampleHost" not in src, f"sampleHost still referenced in {name}"
        assert "sampleName" not in src, f"sampleName still referenced in {name}"


def test_the_sample_wording_is_gone_from_the_picker():
    """The UI face of the bug: a note telling the operator the accounts came from a
    representative host. If this text is back, the table probably isn't."""
    assert "as a sample" not in CFG
    assert "representative host" not in INV


def test_the_hand_off_still_carries_ids_kind_and_names():
    """Shrunk, not broken — the receiving page needs all three."""
    m = re.search(r"cfgmgmt_bulk_selection'[^;]*?JSON\.stringify\(\{(.*?)\}\)",
                  INV, re.S)
    assert m, "the bulk hand-off payload moved or changed shape"
    body = m.group(1)
    for key in ("ids:", "kind:", "names:"):
        assert key in body, f"hand-off no longer carries {key}"


# ── the request the table is fed by ────────────────────────────────────────────

def test_the_page_calls_the_bulk_endpoint_the_api_serves():
    assert "/api/config-mgmt/bulk-managed-accounts" in CFG
    assert '"/bulk-managed-accounts"' in API, "the endpoint route moved"


def test_the_bulk_lookup_sends_ids_only_never_a_host():
    """The security property: the server re-resolves every id through its own rows,
    so a browser-supplied address cannot aim an account lookup (and then a run) at a
    machine that was never selected."""
    m = re.search(r"bulk-managed-accounts',\s*\{(.*?)\}\)", CFG, re.S)
    assert m, "the bulk-managed-accounts request body moved"
    body = m.group(1)
    assert "inventory_ids" in body
    assert "host" not in body, "the bulk lookup must not send an address"


# ── the submit carries the per-target maps ─────────────────────────────────────

def test_the_bulk_post_carries_both_per_target_maps():
    """Without these the table is decorative: the operator picks per row and every
    job still runs on the batch default."""
    m = re.search(r"run-bulk',\s*\{(.*?)\n\s*\}\)", CFG, re.S)
    assert m, "the run-bulk request body moved"
    body = m.group(1)
    assert "perTargetManagedRefs()" in body, \
        "the bulk POST no longer spreads the per-target refs"


def test_per_target_refs_builds_both_map_keys_the_api_declares():
    """The wire names have to match BulkRunRequest's fields exactly — pydantic
    silently drops an unknown key, which is how epml_token_var went missing."""
    m = re.search(r"perTargetManagedRefs\(\)\s*\{(.*?)\n    \},", CFG, re.S)
    assert m, "perTargetManagedRefs() moved"
    body = m.group(1)
    for key in ("managed_accounts", "managed_becomes"):
        assert key in body, f"perTargetManagedRefs no longer builds {key}"
        assert f"{key}: dict[str, ManagedAccountRef | None]" in API, \
            f"BulkRunRequest no longer declares {key}"


def test_a_row_with_no_account_is_omitted_not_sent_as_null():
    """Load-bearing. Omitting falls back to the batch default name, which fails THAT
    job at dispatch with a message naming the host. Sending null would run the job
    with no managed account at all — falling back to ansible_user plus an SSH key,
    which can SUCCEED under the wrong identity."""
    m = re.search(r"perTargetManagedRefs\(\)\s*\{(.*?)\n    \},", CFG, re.S)
    body = m.group(1)
    assert re.search(r"if \(acct\)", body), \
        "per-target refs no longer skip a row with no account"
    assert "= null" not in body, "a no-match row must be omitted, not sent as null"


def test_a_scheduled_batch_sends_names_rather_than_pinned_ids():
    """An id pinned today can name an account that has been removed or re-registered
    by the time Saturday's window opens."""
    m = re.search(r"perTargetManagedRefs\(\)\s*\{(.*?)\n    \},", CFG, re.S)
    body = m.group(1)
    assert "scheduleMode" in body, "the pin/name decision no longer reads the schedule"
    assert "account_name: acct.account_name" in body


# ── option value format vs the parser that splits it ───────────────────────────

def test_the_option_value_format_matches_the_parser():
    """`${s.system_id}:${a.account_id}` is written in five <option> templates and
    parsed in one place ~600 lines away."""
    assert CFG.count("`${s.system_id}:${a.account_id}`") >= 4, \
        "an option value no longer uses the system:account composite"
    m = re.search(r"resolveManagedIn\(systems, key\)\s*\{(.*?)\n    \},", CFG, re.S)
    assert m, "resolveManagedIn() moved"
    assert "key.split(':').map(Number)" in m.group(1)


def test_the_row_readers_are_parameterised_by_system_list():
    """Bulk mode has ONE ACCOUNT LIST PER TARGET. A reader that looked at the shared
    `managedSystems` would resolve a row's key against another host's accounts and
    silently produce the wrong ids — the original bug in a new place."""
    assert "resolveManagedIn(systems, key)" in CFG
    assert "_managedAccountObj(key, systems)" in CFG
    # The single-target shim must survive, or the single-run and agent paths break.
    assert "resolveManaged(key) { return this.resolveManagedIn(this.managedSystems, key); }" in CFG


# ── every method the markup calls must exist ───────────────────────────────────

_REFERENCED = [
    "loadBulkManagedAccounts", "resuggestBulkAccounts", "markBulkManual",
    "perTargetManagedRefs", "bulkDefaultSystems", "bulkUnmatchedCount",
    "bulkDefaultName", "bulkDefaultBecomeName", "resolveManagedIn",
]


def test_every_bulk_method_the_markup_calls_is_defined():
    """Alpine logs an unknown member and carries on, so a typo here is a control that
    silently does nothing rather than an error anyone sees. This caught
    `resuggestBulkAccounts` being referenced twice and defined nowhere."""
    for name in _REFERENCED:
        defined = re.search(
            r"^\s+(?:async\s+)?(?:get\s+)?%s\s*[(:]" % re.escape(name), CFG, re.M)
        assert defined, f"{name} is referenced by the picker but never defined"


def test_every_bulk_state_field_the_markup_binds_is_declared():
    for name in ("bulkTargets", "bulkTargetsLoading", "bulkTargetsError",
                 "bulkAccountKeys", "bulkBecomeKeys", "bulkTruncated",
                 "bulkFailed", "bulkManualRows"):
        assert re.search(r"^\s+%s:" % re.escape(name), CFG, re.M), \
            f"{name} is bound in the markup but not declared on the component"


def test_clear_bulk_resets_the_per_target_state():
    """Leaving a stale table behind after Clear would let the single-run path submit
    account choices made for a selection that no longer exists."""
    m = re.search(r"clearBulk\(\)\s*\{(.*?)\n    \},", CFG, re.S)
    assert m, "clearBulk() moved"
    body = m.group(1)
    for name in ("bulkTargets", "bulkAccountKeys", "bulkBecomeKeys", "bulkManualRows"):
        assert name in body, f"clearBulk() no longer resets {name}"


def test_bulk_mode_loads_the_table_not_the_single_host_lookup():
    """The one-line regression that would restore the old behaviour wholesale."""
    assert "if (this.bulk) await this.loadBulkManagedAccounts();" in CFG
    assert "if (this.bulk) await this.loadManagedAccounts();" not in CFG


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
