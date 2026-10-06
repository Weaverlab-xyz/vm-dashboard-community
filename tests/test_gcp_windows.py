"""GCP Windows servers: the windows-keys password exchange and the launch shape.

GCE has no "get password" call. The guest agent watches the `windows-keys` metadata
entry, resets the named account, and writes the password RSA-OAEP-encrypted to serial
port 4. What these pin:

- the metadata entry is the shape the agent reads (base64 big-endian modulus/exponent,
  RFC 3339 expiry), and the reply is matched to OUR key by modulus — port 4 holds every
  reply ever written;
- decryption is OAEP with SHA-1 (what the agent uses), round-tripped with a real key;
- our entry is removed afterwards, and other users' entries are left alone;
- a Windows launch carries no ssh-keys, gets at least a 50 GB disk, and passes caller
  metadata through (managed-ad-domain for a domain join);
- the admin-password endpoint needs gcp:write and refuses Password Safe-managed accounts.

Run: python tests/test_gcp_windows.py   (or under pytest)
"""
import asyncio
import base64
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from web_dashboard.services import gcp_service  # noqa: E402


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _agent_reply(entry: dict, password: str, public_key) -> str:
    enc = public_key.encrypt(password.encode(), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    return json.dumps({"modulus": entry["modulus"], "exponent": entry["exponent"],
                       "userName": entry["userName"], "ready": True,
                       "encryptedPassword": base64.b64encode(enc).decode()})


def test_entry_is_the_shape_the_agent_reads():
    key = _key()
    entry = gcp_service.windows_key_entry("gcpadmin", key.public_key())
    n = int.from_bytes(base64.b64decode(entry["modulus"]), "big")
    e = int.from_bytes(base64.b64decode(entry["exponent"]), "big")
    assert n == key.public_key().public_numbers().n
    assert e == 65537
    assert entry["userName"] == "gcpadmin"
    assert entry["expireOn"].endswith("Z") and "T" in entry["expireOn"]


def test_reply_matched_by_modulus_and_decrypted():
    key, other = _key(), _key()
    mine = gcp_service.windows_key_entry("gcpadmin", key.public_key())
    theirs = gcp_service.windows_key_entry("someone", other.public_key())
    serial = "\n".join([
        "boot noise",
        _agent_reply(theirs, "NotMine1!", other.public_key()),
        _agent_reply(mine, "Gx7!pass-word", key.public_key()),
        "more noise"])
    reply = gcp_service.parse_windows_password_reply(serial, mine["modulus"])
    assert reply["userName"] == "gcpadmin"
    assert gcp_service.decrypt_windows_password(reply["encryptedPassword"], key) == "Gx7!pass-word"
    assert gcp_service.parse_windows_password_reply("nothing here", mine["modulus"]) is None


class _Item:
    def __init__(self, key, value):
        self.key, self.value = key, value


def test_removal_keeps_other_users_keys_and_other_metadata():
    items = [_Item("startup", "x"),
             _Item("windows-keys", '{"modulus": "AAA", "userName": "a"}\n'
                                   '{"modulus": "BBB", "userName": "b"}')]
    out = dict(gcp_service._metadata_without(items, "windows-keys", drop_modulus="BBB"))
    assert out["startup"] == "x"
    assert "AAA" in out["windows-keys"] and "BBB" not in out["windows-keys"]
    # Removing the last entry drops the key entirely rather than leaving it blank.
    only = [_Item("windows-keys", '{"modulus": "BBB"}')]
    assert gcp_service._metadata_without(only, "windows-keys", drop_modulus="BBB") == []


def test_reset_password_end_to_end_and_cleans_up():
    calls = []
    store = {"serial": ""}

    def set_key(project, zone, name, entry, remove_modulus=""):
        calls.append(("set", entry is not None, remove_modulus))
        if entry is not None:
            # The agent answers on the next serial read.
            store["entry"] = entry

    keys = {}
    orig_gen = rsa.generate_private_key

    def gen(**kw):
        k = orig_gen(**kw)
        keys["k"] = k
        return k

    def serial(project, zone, name):
        if "entry" in store and not store["serial"]:
            store["serial"] = _agent_reply(store["entry"], "Pw-from-agent1", keys["k"].public_key())
            return ""
        return store["serial"]

    saved = (gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync)
    gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync = set_key, serial
    rsa.generate_private_key = gen
    try:
        pw = asyncio.run(gcp_service.reset_windows_password(
            "p", "us-central1-a", "win01", "gcpadmin", timeout_s=60, interval_s=0))
    finally:
        gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync = saved
        rsa.generate_private_key = orig_gen
    assert pw == "Pw-from-agent1"
    assert calls[0][:2] == ("set", True)
    assert calls[-1][0] == "set" and calls[-1][1] is False and calls[-1][2]  # removed by modulus


def test_reset_password_surfaces_an_agent_error():
    def set_key(*a, **k):
        pass

    holder = {}

    def serial(project, zone, name):
        return json.dumps({"modulus": holder["m"], "errorMessage": "user is a domain account"})

    orig = gcp_service.windows_key_entry

    def entry(*a, **k):
        e = orig(*a, **k)
        holder["m"] = e["modulus"]
        return e

    saved = (gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync,
             gcp_service.windows_key_entry)
    gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync = set_key, serial
    gcp_service.windows_key_entry = entry
    try:
        asyncio.run(gcp_service.reset_windows_password("p", "z", "w", "u",
                                                       timeout_s=5, interval_s=0))
    except gcp_service.GCPError as e:
        assert "domain account" in str(e)
    else:
        raise AssertionError("agent error was swallowed")
    finally:
        (gcp_service._set_windows_key_sync, gcp_service._serial_port_4_sync,
         gcp_service.windows_key_entry) = saved


def test_windows_launch_shape():
    """No ssh-keys, a 50 GB floor, caller metadata and the service account pass through."""
    captured = {}

    class _Client:
        def __init__(self, **kw):
            pass

        def get(self, **kw):
            nic = types.SimpleNamespace(network_i_p="10.0.0.9", access_configs=[])
            return types.SimpleNamespace(network_interfaces=[nic], status="RUNNING",
                                         self_link="sl")

    def insert(client, project, zone, name, instance):
        captured["instance"] = instance

    from google.cloud import compute_v1
    saved = (compute_v1.InstancesClient, gcp_service._insert_instance_with_retry,
             gcp_service._gcp_creds, gcp_service._require_compute)
    compute_v1.InstancesClient = _Client
    gcp_service._insert_instance_with_retry = insert
    gcp_service._gcp_creds = lambda: None
    gcp_service._require_compute = lambda: None
    try:
        gcp_service._launch_instance_sync(
            "p", "us-central1-a", "win01", "e2-standard-2",
            "projects/windows-cloud/global/images/family/windows-2022", "", False,
            "gcp-user", "", 20, None, None, True,
            {"managed-ad-domain": "projects/p/locations/global/domains/corp.example.com"},
            "joiner@p.iam.gserviceaccount.com")
    finally:
        (compute_v1.InstancesClient, gcp_service._insert_instance_with_retry,
         gcp_service._gcp_creds, gcp_service._require_compute) = saved
    inst = captured["instance"]
    keys = [i.key for i in inst.metadata.items]
    assert "ssh-keys" not in keys
    assert "managed-ad-domain" in keys
    assert inst.disks[0].initialize_params.disk_size_gb == 50
    assert inst.service_accounts[0].email == "joiner@p.iam.gserviceaccount.com"


def test_public_windows_images_detected_by_name():
    assert gcp_service._image_is_windows_sync(
        "projects/windows-cloud/global/images/family/windows-2022")


def test_endpoint_requires_write_and_refuses_ps_managed():
    import inspect
    from web_dashboard.api import gcp as api_gcp
    src = inspect.getsource(api_gcp.get_instance_admin_password)
    assert 'require_permission("gcp", "write")' in src
    assert "passwordsafe_managed" in src and "409" in src


def test_deploy_wires_windows_hooks():
    """Static: the GCP deploy routes Windows through the shared hook and teardown."""
    import inspect
    from web_dashboard.services import gcp_vm_service
    src = inspect.getsource(gcp_vm_service)
    assert "reset_windows_password" in src
    assert "windows_server_hook.wire" in src
    assert "windows_server_hook.teardown" in src
    assert 'windows_admin_secret.resolve_backend("gcp")' in src


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
