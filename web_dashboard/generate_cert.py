"""
Generate a self-signed TLS certificate for the dashboard.

Usage:
    python web_dashboard/generate_cert.py [hostname] [ip_address]

Defaults to the current machine's hostname and attempts to auto-detect the
primary LAN IPv4 address.  Outputs:
    web_dashboard/certs/cert.pem
    web_dashboard/certs/key.pem

The certificate is valid for 10 years and includes Subject Alternative Names
(SANs) for localhost, 127.0.0.1, and the provided hostname/IP so browsers
accept it for all access paths.

NOTE: Browsers will still show a "not trusted" warning because the cert is
self-signed.  To eliminate warnings on your local machines use mkcert:
    https://github.com/FiloSottile/mkcert
    mkcert myhost localhost 127.0.0.1
"""
import datetime
import ipaddress
import os
import socket
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# ── Resolve hostname / IP ──────────────────────────────────────────────────────

hostname = sys.argv[1] if len(sys.argv) > 1 else socket.gethostname()

if len(sys.argv) > 2:
    lan_ip = sys.argv[2]
else:
    # Try to auto-detect a non-loopback IPv4 address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "127.0.0.1"

print(f"Generating certificate for hostname={hostname!r}, ip={lan_ip!r}")

# ── Generate RSA key ───────────────────────────────────────────────────────────

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# ── Build certificate ──────────────────────────────────────────────────────────

subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VM CLI Dashboard"),
    x509.NameAttribute(NameOID.COMMON_NAME, hostname),
])

san_entries = [
    x509.DNSName(hostname),
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
]
try:
    san_entries.append(x509.IPAddress(ipaddress.IPv4Address(lan_ip)))
except ValueError:
    pass  # skip if not a valid IPv4

cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
    .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
    .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    .sign(key, hashes.SHA256())
)

# ── Write output files ─────────────────────────────────────────────────────────

out_dir = os.path.join(os.path.dirname(__file__), "certs")
os.makedirs(out_dir, exist_ok=True)

cert_path = os.path.join(out_dir, "cert.pem")
key_path = os.path.join(out_dir, "key.pem")

with open(cert_path, "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))

with open(key_path, "wb") as f:
    f.write(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))

print(f"  cert: {cert_path}")
print(f"  key:  {key_path}")
print()
print("Start the dashboard with HTTPS:")
print(f"  uvicorn web_dashboard.main:app --host 0.0.0.0 --port 8443 \\")
print(f"    --ssl-certfile {cert_path} \\")
print(f"    --ssl-keyfile  {key_path}")
print()
print("Or run:  .\\start_dashboard.ps1")
