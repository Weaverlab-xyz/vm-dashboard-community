"""dashboard_spire.check_agent_address: is this host:port THIS dashboard's SPIRE server?

Found live: with "SPIRE server address for agents" blank, a POV instance sharing a
Container Apps environment with another dashboard handed its brokers port 8081, which there
is the other dashboard's SPIRE server. The broker's TLS failed and it fell back to Ed25519
after a two-minute wait, with nothing saying why.

Pinned against a real TLS listener, with certificates made here in SPIRE's shape (a leaf
whose only identity is a SPIFFE URI SAN, under an intermediate, under the trust root):

  * this server's chain and ID           -> True
  * another trust root (another server)  -> False, saying to set the address
  * this root, another SPIFFE ID         -> False, naming what it presents
  * nothing listening                    -> None: proves nothing about the agent's network
  * not TLS at all                       -> False

Run: python tests/test_spire_agent_address_check.py   (or under pytest)
"""
import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db"))
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-spire-agent-address")

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ModuleNotFoundError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.services import dashboard_spire  # noqa: E402

_NOW = datetime.datetime.now(datetime.timezone.utc)


def _cert(subject, key, issuer=None, issuer_key=None, ca=False, uri=None):
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, subject)]))
         .issuer_name(issuer.subject if issuer is not None else
                      x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, subject)]))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(_NOW - datetime.timedelta(minutes=5))
         .not_valid_after(_NOW + datetime.timedelta(hours=1))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if ca:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                          crl_sign=True, content_commitment=False,
                                          key_encipherment=False, data_encipherment=False,
                                          key_agreement=False, encipher_only=False,
                                          decipher_only=False), critical=True)
    if uri:
        b = (b.add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]),
                             critical=False)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                   ExtendedKeyUsageOID.CLIENT_AUTH]),
                            critical=False))
    return b.sign(issuer_key or key, hashes.SHA256())


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _trust_domain(td, uri=None):
    """(root PEM, a function serving this domain's server on a port)."""
    rk, ik, lk = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _cert("root " + td, rk, ca=True)
    inter = _cert("SPIFFE", ik, issuer=root, issuer_key=rk, ca=True)
    leaf = _cert("SPIRE", lk, issuer=inter, issuer_key=ik,
                 uri=uri or f"spiffe://{td}/spire/server")
    d = tempfile.mkdtemp(prefix="spire-tls-")
    chain, key = os.path.join(d, "chain.pem"), os.path.join(d, "key.pem")
    open(chain, "w", encoding="ascii").write(_pem(leaf) + _pem(inter))
    open(key, "wb").write(lk.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    def serve():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(chain, key)
        ctx.set_alpn_protocols(["h2"])
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)

        def accept():
            try:
                conn, _ = lsock.accept()
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (OSError, ssl.SSLError):
                pass
            finally:
                lsock.close()
        threading.Thread(target=accept, daemon=True).start()
        return lsock.getsockname()[1]
    return _pem(root), serve


def test_this_dashboards_server_is_recognised():
    root, serve = _trust_domain("pov.example.test")
    verdict, why = dashboard_spire.check_agent_address("127.0.0.1", serve(), root,
                                                       "pov.example.test")
    assert verdict is True, why


def test_another_dashboards_server_is_refused_and_says_what_to_set():
    """The live case: the port reaches a real SPIRE server, just not this one."""
    mine, _ = _trust_domain("pov.example.test")
    _, serve_other = _trust_domain("dash.example.test")
    verdict, why = dashboard_spire.check_agent_address("127.0.0.1", serve_other(), mine,
                                                       "pov.example.test")
    assert verdict is False, why
    assert "SPIRE server address for agents" in why


def test_the_right_root_but_another_spiffe_id_is_refused():
    root, serve = _trust_domain("pov.example.test",
                                uri="spiffe://pov.example.test/workload/not-the-server")
    verdict, why = dashboard_spire.check_agent_address("127.0.0.1", serve(), root,
                                                       "pov.example.test")
    assert verdict is False and "workload/not-the-server" in why, why


def test_nothing_listening_is_inconclusive():
    root, _ = _trust_domain("pov.example.test")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    verdict, why = dashboard_spire.check_agent_address("127.0.0.1", port, root,
                                                       "pov.example.test", timeout=2)
    assert verdict is None, why


def test_something_that_is_not_tls_is_refused():
    root, _ = _trust_domain("pov.example.test")
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)

    def answer():
        conn, _ = lsock.accept()
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()
        lsock.close()
    threading.Thread(target=answer, daemon=True).start()
    verdict, why = dashboard_spire.check_agent_address("127.0.0.1", lsock.getsockname()[1],
                                                       root, "pov.example.test", timeout=3)
    assert verdict is False, why


def test_tls_below_1_2_is_never_negotiated():
    """CodeQL: create_default_context alone can still allow TLS 1.0/1.1 on some builds."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "dashboard_spire.py"),
               encoding="utf-8").read()
    body = src.split("def check_agent_address(", 1)[1].split("\ndef ", 1)[0]
    assert "ctx.minimum_version = ssl.TLSVersion.TLSv1_2" in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fns.sort(key=lambda f: f.__code__.co_firstlineno)
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
