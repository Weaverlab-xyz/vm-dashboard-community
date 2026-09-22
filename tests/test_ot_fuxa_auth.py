"""The OT cell's HMI authenticates, and does it with a key of its own.

FUXA ships with authentication OFF, and that is not "reduced" — it is absent. With
`secureEnabled` false the server short-circuits twice, and an anonymous caller is
handed administrator on the very endpoints that create and delete HMI users. So on a
stock image, anything that can reach :1881 owns the HMI, and a just-in-time account on
it would be theatre.

Turning it on is three changes that only work together, and each has a failure that
is silent in the worst direction:

* the settings are written BEFORE FUXA first starts. The other way to set them is
  POST /api/settings, which RESTARTS the runtime — and that restart lands inside the
  project seed, whose read-back then 401s and reports a project that seeded fine as
  "NOT seeded";
* the signing key is minted PER CELL, on first boot. Baked, every cell built from one
  image would share a JWT key and a token minted on one plant would validate on
  another. The bake runs apply.sh itself, so the cleanup has to put the placeholder
  back or the build VM's key ships;
* the admin password is rotated per cell at WIRE time, from the broker. It cannot be
  done at bake for the same reason as the key, and the broker is the only host that
  can reach the cell once the Purdue zoning is on.

Run: python tests/test_ot_fuxa_auth.py   (or under pytest)
"""
import io
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_SCRIPT = os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh")
_PLAY = os.path.join(_ROOT, "examples", "playbooks", "kubesolo",
                     "fuxa-admin-rotate.yml")
_SRC = io.open(_SCRIPT, encoding="utf-8").read()

_PLACEHOLDER = "REPLACE_ON_FIRST_BOOT"


def _code_only(text: str) -> str:
    """``text`` with whole-line comments removed.

    EVERY "must not appear" assertion below runs on this. The bake script, the play
    and the seed all EXPLAIN the things they avoid — "NOT Authorization: Bearer",
    "POST /api/settings restarts the runtime", "a root-owned 0600 file" — and
    asserting on the raw text makes each of those comments fail the very test it
    exists to explain. This file tripped over that four times before the helper
    existed; see the repo's note on describing a banned literal rather than quoting
    one.

    Whole-line comments only, deliberately: for a NEGATIVE assertion over-stripping
    is the dangerous direction, because it turns a real violation into a pass.
    """
    kept = [line for line in text.splitlines()
            if not line.lstrip().startswith("#")]
    return "\n".join(kept)


def _play():
    return list(yaml.safe_load_all(io.open(_PLAY, encoding="utf-8")))[0][0]


def _flat(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flat(task.get(key) or [])


def _play_text():
    return io.open(_PLAY, encoding="utf-8").read()


# ── The bake ─────────────────────────────────────────────────────────────────

def test_authentication_is_on_by_default():
    found = re.search(r'OT_FUXA_SECURE="\$\{OT_FUXA_SECURE:-(\d)\}"', _SRC)
    assert found and found.group(1) == "1", (
        "an HMI that authenticates nobody must not be the default — with "
        "secureEnabled off FUXA hands every anonymous caller administrator")


def test_turning_it_off_says_what_that_means():
    assert "NO authorization at all" in _SRC, (
        "OT_FUXA_SECURE=0 is allowed but its consequence is not stated, and the "
        "consequence is that anyone who can reach the port owns the HMI")


def test_the_settings_are_written_before_fuxa_ever_starts():
    """The ordering that sidesteps the restart trap entirely.

    POST /api/settings restarts the FUXA runtime, and that restart inside the project
    seed makes the read-back 401 — reporting a project that seeded fine as NOT
    seeded. Writing the file while nothing is running has no ordering problem at all.
    """
    settings_write = _SRC.index("mysettings.json")
    first_apply = _SRC.index("/opt/ot-sim/kubesolo/apply.sh")
    assert settings_write < first_apply, (
        "the settings are written after the workloads start, so FUXA read the old "
        "ones and the change only takes effect on some later restart")
    assert "/api/settings" not in _code_only(_SRC), (
        "the bake POSTs settings, which restarts the runtime mid-seed")


def test_the_settings_are_merged_not_clobbered():
    assert "json.load(handle)" in _SRC and "settings[\"secureEnabled\"] = True" in _SRC, (
        "the settings file is the whole document; replacing it would drop keys "
        "(uiPort among them) that nothing here knows to restore")


def test_the_token_lifetime_is_short_and_explained():
    found = re.search(r'OT_FUXA_TOKEN_EXPIRES="\$\{OT_FUXA_TOKEN_EXPIRES:-([^}]+)\}"', _SRC)
    assert found, "tokenExpiresIn is not configurable"
    assert found.group(1) in ("15m", "10m", "5m"), found.group(1)
    assert "REVOCATION WINDOW" in _SRC, (
        "the token lifetime is not explained as what it actually is: FUXA verifies a "
        "socket's token once at connect and never re-checks, so an open session "
        "survives a deletion for exactly this long")


# ── The signing key ──────────────────────────────────────────────────────────

def test_the_baked_signing_key_is_a_placeholder():
    assert _PLACEHOLDER in _SRC
    assert 'settings["secretCode"] = "%s"' % _PLACEHOLDER in _SRC, (
        "a real signing key baked into the image would be shared by every cell built "
        "from it, so a token minted on one plant would validate on another")


def test_each_cell_mints_its_own_key_on_first_boot():
    apply_start = _SRC.index("cat > /opt/ot-sim/kubesolo/apply.sh")
    apply_body = _SRC[apply_start:_SRC.index("\nEOF\n", apply_start)]
    assert _PLACEHOLDER in apply_body, "apply.sh never replaces the placeholder"
    assert "secrets.token_urlsafe" in apply_body, "the key is not randomly generated"
    assert apply_body.index(_PLACEHOLDER) < apply_body.index("kubectl apply -f"), (
        "the key is minted after the HMI starts, so FUXA read the placeholder and "
        "signed the session's tokens with a value every cell shares")


def test_the_cleanup_puts_the_placeholder_back():
    """The bake RUNS apply.sh to smoke-test the stack, which mints a real key on the
    BUILD VM. Without this reset that key ships inside the image, which is the exact
    thing the placeholder exists to prevent."""
    cleanup = _SRC[_SRC.index("resetting the cluster's identity"):]
    assert "resetting FUXA's signing key" in cleanup, (
        "the key minted while smoke-testing is left in the image")
    assert _PLACEHOLDER in cleanup


def test_the_settings_file_is_readable_by_the_container():
    """The appdata directory is 0777 because the pinned image's UID is unknown; a
    root-owned 0600 settings file is one FUXA cannot read, and the symptom is
    authentication silently not being on."""
    # Scoped to the settings file. A blanket ban would also catch the tunnel
    # kubeconfig's chmod 0600, which is correct where it is — that file is read by
    # root on the host, not by a container whose UID nobody here knows.
    settings_block = _code_only(_SRC[_SRC.index('FUXA_SETTINGS=/var/lib'):])
    settings_block = settings_block[:settings_block.index("kubectl apply -f")]
    assert "chmod 0666" in settings_block, (
        "FUXA's settings file is not made readable by the container")
    assert "chmod 0600" not in settings_block, (
        "a root-owned 0600 settings file is one FUXA cannot read at startup, and the "
        "symptom is authentication silently not being on")


# ── The seed, which now has to authenticate ──────────────────────────────────

def _seed() -> str:
    found = re.search(r"cat > /opt/ot-sim/plc-sim/fuxa_seed\.py <<'EOF'\n(.*?)\nEOF\n",
                      _SRC, re.S)
    assert found, "the seed script is not where it was"
    return found.group(1)


def test_the_seed_signs_in_and_uses_the_right_header():
    seed = _seed()
    assert "_signin" in seed, "the seed never authenticates, so it 401s on a secure HMI"
    assert "x-access-token" in seed, (
        "no Authorization: Bearer parsing exists anywhere in the FUXA server, so that "
        "header is an unauthenticated request that looks correct")
    assert "Bearer" not in _code_only(seed)


def test_the_seed_still_works_against_an_hmi_with_auth_off():
    seed = _seed()
    assert 'TOKEN = ""' in seed, "an empty token must be a valid state"
    assert "continuing unauthenticated" in seed, (
        "a failed sign-in must not stop the seed — an image baked with "
        "OT_FUXA_SECURE=0 has nothing to sign in to")


def test_the_seed_is_still_never_fatal():
    """It has always been a convenience: a FUXA whose project format moved must leave
    the operator with today's behaviour, not a failed 15-minute bake."""
    seed = _seed()
    assert "die " not in seed


# ── Proving it took effect ───────────────────────────────────────────────────

def test_the_bake_proves_authentication_rather_than_assuming_it():
    """The one check that matters, and it is exact: with secureEnabled ON,
    /api/users answers 401 to an anonymous caller; with it OFF the same request
    returns 200 with the user list. The status code IS the answer."""
    assert "anonymous GET /api/users" in _SRC, "the bake never probes"
    probe = _SRC[_SRC.index("anonymous GET /api/users"):]
    probe = probe[:probe.index("\nfi\n")]
    assert "401|403)" in probe, "a refusal is not recognised as success"
    assert "200) die" in probe, (
        "an anonymous caller listing users does not fail the bake, so an image that "
        "claims authentication it does not have would ship")


def test_the_probe_failure_names_what_to_check():
    probe = _SRC[_SRC.index("anonymous GET /api/users"):]
    assert "mysettings.json" in probe and "secureEnabled" in probe, (
        "the refusal does not say where the setting lives or what it is called — and "
        "whether a partial file merges is a property of the pinned FUXA")


# ── The rotation play ────────────────────────────────────────────────────────

def test_the_play_targets_the_broker_not_the_cell():
    header = _play_text()[:2000]
    assert "DMZ broker" in header and "NOT the cell" in header, (
        "once the Purdue zoning is on the cell admits the Gateway and the broker and "
        "nothing else, so the broker is the only host this can run from")


def test_the_play_is_idempotent_so_rewire_is_safe():
    names = [t.get("name", "") for t in _flat(_play()["tasks"])]
    assert any("Try the new password first" in n for n in names), (
        "without this a second run finds neither password and refuses — correctly, "
        "and permanently")


def test_the_play_refuses_to_guess_rather_than_resetting():
    """Somebody else set that password, and a play that 'fixes' it has just locked
    them out of the HMI."""
    text = _play_text()
    assert "Refuse to guess" in text
    assert "lock them out" in text
    assert "Nothing was changed" in text


def test_the_play_preserves_the_accounts_own_fields():
    """create and update are ONE upsert, so a field omitted is a field rewritten —
    and `groups` is the admin account's own permission bitmask."""
    text = _play_text()
    assert "_fuxa_admin.groups | default(-1)" in text, (
        "the admin's groups value is not carried over, so the rotation would rewrite "
        "the account's permissions")
    assert "_fuxa_admin.fullname" in text


def test_the_play_never_writes_a_blank_info():
    """FUXA parses `info` AFTER answering 200; on a failure the account is silently
    absent from its in-memory map — able to sign in, then 401 on everything."""
    text = _play_text()
    assert "default('{}', true)" in text, "info could be written empty or null"
    assert "resolved promise" in text, "the trap is not explained"


def test_the_play_uses_the_right_header_and_body_shape():
    text = _play_text()
    assert "x-access-token" in text
    assert "Authorization" not in _code_only(text)
    assert "params:" in text, "the body is not wrapped in params"
    assert "never a list" in text, (
        "FUXA's own OpenAPI declares params as an array and is wrong; the reason is "
        "not recorded")


def test_the_play_confirms_by_signing_in_again():
    names = [t.get("name", "") for t in _flat(_play()["tasks"])]
    assert any("Confirm the new password works" in n for n in names), (
        "the write answers 200 for a payload FUXA then drops, so the only real "
        "confirmation is a sign-in")


def test_the_new_password_never_appears_in_output():
    for task in _flat(_play()["tasks"]):
        # Skip block wrappers: json.dumps of a block contains its children's text,
        # but the wrapper itself handles nothing and cannot carry no_log for them.
        if any(key in task for key in ("block", "rescue", "always")):
            continue
        body = json.dumps(task)
        if "fuxa_new_password" in body and "assert" not in body:
            assert task.get("no_log") is True, (
                f"{task.get('name')!r} handles the password without no_log")


# ── The service side ─────────────────────────────────────────────────────────

def test_the_service_mints_the_password_once_and_rotates_before_deploying():
    service = io.open(os.path.join(_ROOT, "web_dashboard", "services",
                                   "ot_faas_service.py"), encoding="utf-8").read()
    assert "ensure_fuxa_admin_password" in service
    assert "FUXA_ROTATE_PLAYBOOK" in service
    # By reference, like every other credential here.
    assert 'secret_vars={"fuxa_new_password": fuxa_admin_config_key(child_id)}' in service
    wiring = io.open(os.path.join(_ROOT, "web_dashboard", "services",
                                  "ot_service.py"), encoding="utf-8").read()
    assert wiring.index("queue_fuxa_rotate") < wiring.index("queue_deploy"), (
        "the adapter is deployed before the HMI has adopted the password it is given, "
        "so its first grant would 401")


def test_teardown_clears_the_hmi_password_too():
    service = io.open(os.path.join(_ROOT, "web_dashboard", "services",
                                   "ot_faas_service.py"), encoding="utf-8").read()
    assert "_clear_stash" in service
    block = service[service.index("def _clear_stash"):]
    assert "fuxa_admin_config_key" in block and "bearer_config_key" in block, (
        "a destroyed cell leaves one of its secrets in the config store forever")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
