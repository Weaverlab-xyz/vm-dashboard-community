"""The agent's directory probe: an anonymous LDAP rootDSE read.

What these pin:

- the request is a base-scope SearchRequest for the rootDSE — never a BindRequest — and
  the agent sends nothing else, checked against a real local socket server;
- replies are parsed from bytes an LDAP server actually sends (fixtures encoded with
  ldap3's RFC 4511 ASN.1 spec, not hand-written), for AD and OpenLDAP;
- classification: AD vs other LDAP, the domain derived from the naming context, and the
  functional level named;
- a server that answers with no entry, garbage, or nothing is "not a directory", not a
  crash;
- an "all" sweep stays hypervisor-only, and directory findings are matched against
  registered directories by host:port on the dashboard.

Run: python tests/test_agent_directory_probe.py   (or under pytest)
"""
import importlib.util
import os
import socket
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")
_spec = importlib.util.spec_from_file_location("agent_runner_dir", _PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

# SearchResultEntry + SearchResultDone, encoded by ldap3's rfc4511 model.
AD_REPLY = bytes.fromhex(
    "3082019d020101648201960400308201903033041464656661756c744e616d696e67436f6e74657874"
    "311b041944433d636f72702c44433d6578616d706c652c44433d636f6d30360417726f6f74446f6d61"
    "696e4e616d696e67436f6e74657874311b041944433d636f72702c44433d6578616d706c652c44433d"
    "636f6d3026040b646e73486f73744e616d6531170415444330312e636f72702e6578616d706c652e63"
    "6f6d303c040f6c646170536572766963654e616d6531290427636f72702e6578616d706c652e636f6d"
    "3a646330312440434f52502e4558414d504c452e434f4d3024041d646f6d61696e436f6e74726f6c6c"
    "657246756e6374696f6e616c6974793103040137301a0413666f7265737446756e6374696f6e616c69"
    "747931030401373059040e6e616d696e67436f6e74657874733147041944433d636f72702c44433d65"
    "78616d706c652c44433d636f6d042a434e3d436f6e66696775726174696f6e2c44433d636f72702c44"
    "433d6578616d706c652c44433d636f6d301e0414737570706f727465644c44415056657273696f6e31"
    "06040133040132300c02010165070a010004000400")
OPENLDAP_REPLY = bytes.fromhex(
    "308181020101647c040030783025040e6e616d696e67436f6e74657874733113041164633d6578616d"
    "706c652c64633d6f7267301b0414737570706f727465644c44415056657273696f6e31030401333018"
    "040a76656e646f724e616d65310a04084f70656e4c4441503018040d76656e646f7256657273696f6e"
    "31070405322e362e37300c02010165070a010004000400")
DONE_ONLY = bytes.fromhex("300c02010165070a010004000400")


def test_request_is_a_base_scope_rootdse_search():
    req = agent.rootdse_request()
    tag, msg, end = agent._ber_read(req, 0)
    assert tag == 0x30 and end == len(req)
    _t, _id, pos = agent._ber_read(msg, 0)
    op_tag, op, _ = agent._ber_read(msg, pos)
    assert op_tag == 0x63, "must be a SearchRequest"
    _t, base, p = agent._ber_read(op, 0)
    assert base == b"", "base object must be the rootDSE"
    scope_tag, scope, _ = agent._ber_read(op, p)
    assert scope_tag == 0x0A and scope == b"\x00", "scope must be baseObject"
    assert b"defaultNamingContext" in op and b"dnsHostName" in op


def test_parses_and_classifies_active_directory():
    attrs = agent.parse_rootdse_response(AD_REPLY)
    f = agent.classify_rootdse(attrs, "10.0.0.5", 636)
    assert f["kind"] == "directory" and f["product"] == "active_directory"
    assert f["domain"] == "corp.example.com"
    assert f["base_dn"] == "DC=corp,DC=example,DC=com"
    assert f["dc_hostname"] == "DC01.corp.example.com"
    assert f["functional_level"] == "Windows Server 2016"
    assert f["endpoint"] == "ldaps://10.0.0.5:636"
    assert f["confidence"] == "confirmed"
    assert f["suggested_name"] == "corp.example.com"


def test_parses_and_classifies_openldap():
    attrs = agent.parse_rootdse_response(OPENLDAP_REPLY)
    f = agent.classify_rootdse(attrs, "10.0.0.9", 389)
    assert f["product"] == "openldap"
    assert f["domain"] == "example.org" and f["base_dn"] == "dc=example,dc=org"
    assert f["vendor"] == "OpenLDAP 2.6.7" and f["functional_level"] == ""


def test_no_entry_or_garbage_is_not_a_directory():
    assert agent.parse_rootdse_response(DONE_ONLY) == {}
    assert agent.classify_rootdse({}, "10.0.0.1", 389) is None
    # A partial message is "not yet", so the reader keeps reading.
    assert agent.parse_rootdse_response(AD_REPLY[:40]) is None
    assert agent.parse_rootdse_response(b"HTTP/1.1 400 Bad Request\r\n") is None


def _serve_once(reply: bytes):
    """A one-shot TCP server; returns (port, received-bytes holder, thread)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def run():
        conn, _ = srv.accept()
        conn.settimeout(3)
        data = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                try:
                    _t, _m, end = agent._ber_read(data, 0)
                    if end == len(data):
                        break
                except ValueError:
                    continue
        except OSError:
            pass
        got["data"] = data
        conn.sendall(reply)
        conn.close()
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], got, t


def test_live_probe_sends_only_a_search_and_never_binds():
    port, got, t = _serve_once(AD_REPLY)
    f = agent.probe_directory("127.0.0.1", port, 3)
    t.join(3)
    data = got["data"]
    _t, msg, end = agent._ber_read(data, 0)
    assert end == len(data), "exactly one LDAP message was sent"
    _t, _id, pos = agent._ber_read(msg, 0)
    op_tag, _op, _ = agent._ber_read(msg, pos)
    assert op_tag == 0x63, "the only operation sent is a SearchRequest"
    assert op_tag != 0x60, "a BindRequest must never be sent"
    assert f["domain"] == "corp.example.com"


def test_unreachable_port_is_none():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert agent.probe_directory("127.0.0.1", port, 1) is None


def test_all_sweep_stays_hypervisor_only():
    assert "directory" in agent._PORT_DEFAULTS
    assert "directory" not in agent._HYPERVISOR_FAMILIES
    assert agent.AGENT_VERSION.startswith("2.8")


def test_dashboard_meta_and_annotation():
    sys.path.insert(0, _ROOT)
    from web_dashboard.services import agent_job_meta as ajm
    assert "directory" in ajm.VALID_SCAN_KINDS
    meta = ajm.normalize({"scan_kind": "directory", "hostnames": ["corp.example.com"]})
    assert meta["ports"]["directory"] == [389, 636]
    for key in ("domain", "base_dn", "dc_hostname", "functional_level", "vendor"):
        assert key in ajm.FINDING_KEYS
    out = ajm.discover_findings({"findings": [agent.classify_rootdse(
        agent.parse_rootdse_response(AD_REPLY), "10.0.0.5", 636)]})
    assert out["findings"][0]["domain"] == "corp.example.com"


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
