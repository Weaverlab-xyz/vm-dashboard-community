"""The rep-side client contract: what scripts/ot/verify_tunnels.py and the docs
tell an SE to point at must be what provisioners/ot/ot-sim-debian.sh actually
serves.

Everything on the rep's side of a PRA protocol tunnel is a copy of a fact that
lives in the bake script -- a port, a tag name, an OPC UA endpoint path, a DB
offset. Nothing links the two, so the copies drift silently and the drift only
shows up in front of a customer, where every symptom looks the same: the tunnel
establishes and the client hangs. That is indistinguishable from a blocked
firewall, a dead container, or a sim that was never baked.

This already caught one live instance. Both docs advertised the OPC UA endpoint
as ``opc.tcp://opcua:4840/ot-sim/server/``; the sim has always served
``/freeopcua/server/`` (asyncua's default path, which the sim never overrode).
asyncua's own client ignores the path, so a Python check passed while a strict
client -- FUXA's node-opcua driver, which the same doc line tells you to
configure -- would not connect.

Run: python tests/test_ot_client_contract.py   (or under pytest)
"""
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SIM = os.path.join(_ROOT, "provisioners", "ot", "ot-sim-debian.sh")
_VERIFY = os.path.join(_ROOT, "scripts", "ot", "verify_tunnels.py")
_DOCS = [
    os.path.join(_ROOT, "docs", "cloud-ot.md"),
    os.path.join(_ROOT, "docs", "ot-protocol-clients.md"),
    os.path.join(_ROOT, "provisioners", "ot", "README.md"),
]


def _read(path):
    return open(path, encoding="utf-8").read()


def _verify_module():
    """Import the standalone script by path -- scripts/ has no package."""
    spec = importlib.util.spec_from_file_location("ot_verify_tunnels", _VERIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SRC = _read(_SIM)
_VT = _verify_module()


def test_ports_match_what_the_sims_bind():
    """The listen port in each sim's source, not the compose mapping -- a client
    reaches the sim's own port through the tunnel's remote half."""
    bound = {
        "modbus": re.search(r'StartTcpServer\(context=context, address=\("0\.0\.0\.0", (\d+)\)\)', _SRC),
        "s7": re.search(r"server\.start\(tcp_port=(\d+)\)", _SRC),
        "enip": re.search(r'ADDRESS = "0\.0\.0\.0:(\d+)"', _SRC),
        "opcua": re.search(r'ENDPOINT = "opc\.tcp://0\.0\.0\.0:(\d+)', _SRC),
    }
    for key, match in bound.items():
        assert match, f"{key}: could not find the sim's listen port in {_SIM}"
        assert _VT.DEFAULT_PORTS[key] == int(match.group(1)), (
            f"{key}: verify_tunnels defaults to {_VT.DEFAULT_PORTS[key]} but the sim "
            f"binds {match.group(1)}")


def test_compose_publishes_every_sim_port_unchanged():
    """A remapped host port would make the tunnel's remote half wrong even though
    the sim itself is fine."""
    published = dict(re.findall(r'- "(\d+):(\d+)"', _SRC))
    for key, port in _VT.DEFAULT_PORTS.items():
        assert str(port) in published, (
            f"{key}: compose publishes no host port {port} -- the tunnel's remote "
            f"port would have to change too")
        assert published[str(port)] == str(port), (
            f"{key}: compose maps host {port} to container {published[str(port)]}")


def test_opcua_endpoint_path_and_namespace():
    endpoint = re.search(r'ENDPOINT = "(opc\.tcp://[^"]+)"', _SRC)
    assert endpoint, "the OPC UA sim declares no ENDPOINT"
    path = endpoint.group(1).split("4840", 1)[1]
    assert _VT.OPCUA_PATH == path, (
        f"verify_tunnels uses OPC UA path {_VT.OPCUA_PATH!r}, the sim serves {path!r}")

    namespace = re.search(r'register_namespace\("([^"]+)"\)', _SRC)
    assert namespace, "the OPC UA sim registers no namespace"
    assert _VT.OPCUA_NAMESPACE == namespace.group(1), (
        f"verify_tunnels browses namespace {_VT.OPCUA_NAMESPACE!r}, the sim "
        f"registers {namespace.group(1)!r}")


def test_docs_advertise_the_endpoint_path_the_sim_serves():
    """FUXA's OPC UA driver and UaExpert both take the full endpoint URL, path
    included, and a wrong path is refused rather than ignored."""
    endpoint = re.search(r'ENDPOINT = "opc\.tcp://0\.0\.0\.0:\d+([^"]*)"', _SRC)
    path = endpoint.group(1)
    for doc in _DOCS:
        if not os.path.exists(doc):
            continue
        for line in _read(doc).splitlines():
            for url in re.findall(r"opc\.tcp://[^\s`)|\"']+", line):
                url = url.rstrip(".,;")
                if url.count("/") <= 2:  # host:port only, no path claimed
                    continue
                assert url.endswith(path) or url.endswith(path.rstrip("/")), (
                    f"{os.path.basename(doc)}: advertises {url}, but the sim serves "
                    f"the path {path!r}")


def test_value_names_match_the_opcua_and_cip_tags():
    """One demo script reads the same four values through any vendor's protocol;
    two of the four protocols address them BY NAME."""
    opcua_vars = re.findall(r'await plant\.add_variable\(idx, "(\w+)"', _SRC)
    assert list(_VT.VALUES) == opcua_vars, (
        f"verify_tunnels reads {list(_VT.VALUES)}, the OPC UA sim exposes {opcua_vars}")

    cip = re.search(r"^TAGS = \[([^\]]+)\]", _SRC, re.M)
    assert cip, "the EtherNet/IP sim declares no TAGS"
    cip_tags = re.findall(r'"(\w+)"', cip.group(1))
    assert list(_VT.VALUES) == cip_tags, (
        f"verify_tunnels reads {list(_VT.VALUES)}, the CIP sim serves {cip_tags}")


def test_docs_spell_cip_tag_names_the_way_the_sim_serves_them():
    """CIP tag names are case-sensitive and pylogix reports a miss as a Status,
    not an exception -- so the wrong case reads as "the tunnel is broken".
    docs/cloud-ot.md said ``Read("COUNTER")`` for as long as the tag existed."""
    cip = re.search(r"^TAGS = \[([^\]]+)\]", _SRC, re.M)
    tags = set(re.findall(r'"(\w+)"', cip.group(1)))
    for doc in _DOCS:
        if not os.path.exists(doc):
            continue
        for name in re.findall(r'Read\("(\w+)"\)', _read(doc)):
            assert name in tags, (
                f"{os.path.basename(doc)}: tells the reader to Read(\"{name}\"), but "
                f"the sim serves {sorted(tags)} -- CIP tag names are case-sensitive")


def test_s7_db_layout_matches():
    """S7 has no tag names -- the client reads raw big-endian words at fixed
    offsets, so a resized DB reads as garbage rather than as an error."""
    size = re.search(r"^DB_SIZE = (\d+)", _SRC, re.M)
    assert size, "the S7 sim declares no DB_SIZE"
    packed = re.search(r'struct\.pack_into\(\s*"(>[HBIfd]+)"', _SRC)
    assert packed, "the S7 sim does not pack its DB with a fixed struct format"
    src = _read(_VERIFY)
    read = re.search(r'struct\.unpack\("(>[HBIfd]+)", bytes\(data\)\)', src)
    assert read, "verify_tunnels does not unpack the S7 DB with a fixed format"
    assert read.group(1) == packed.group(1), (
        f"verify_tunnels unpacks {read.group(1)!r}, the sim packs {packed.group(1)!r}")
    assert "db_read(1, 0, %s)" % size.group(1) in src, (
        f"verify_tunnels must read DB1 offset 0 for {size.group(1)} bytes")


def test_every_baked_sim_has_a_reader():
    """OT_SIMS names the simulators the image can carry; a new one that no client
    check knows about ships as an untested protocol."""
    known = re.search(r"^\s*(modbus\|opcua\|enip\|s7)\) ;;", _SRC, re.M)
    assert known, "could not find the OT_SIMS validation list in the bake script"
    for sim in known.group(1).split("|"):
        assert sim in _VT.READERS, (
            f"the image can bake the '{sim}' sim but verify_tunnels has no reader "
            f"for it")


def test_dnp3_is_not_offered_as_a_cell_protocol():
    """The cell does not simulate DNP3 (opendnp3 needs a source build). Offering
    it here would send an SE at a port with no listener."""
    assert "dnp3" not in _VT.DEFAULT_PORTS
    assert "DNP3" in _VT.__doc__, (
        "verify_tunnels must say why DNP3 is absent -- the deploy form still "
        "offers the preset, so its absence looks like an oversight")


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
