"""Runs INSIDE an ECS / Cloud Run Ansible runner task: collect this run's credential.

docs/design/dashboard-workload-identity.md, Slice 5. Instead of the dashboard copying a
checked-out Password Safe credential into AWS or GCP Secrets Manager for the task to
read, the task proves who it is and collects the credential, sealed to a key it made:

  1. an X25519 key pair, here, never written anywhere;
  2. a proof of the task's own cloud identity, bound to this run's token —
       ECS:       a SigV4-presigned ``sts:GetCallerIdentity`` signed with the task role,
                  with the token's SHA-256 in a SIGNED header, so a captured proof
                  cannot be replayed with another token;
       Cloud Run: a Google-signed ID token from the metadata server whose AUDIENCE
                  carries the token's SHA-256, for the same reason;
  3. ``POST <dashboard>/api/agent/runner-credential`` with the token, the proof and the
     public key; the dashboard verifies the identity and answers with the credential
     sealed to that key (``services/agent_sealing``'s format);
  4. the values are opened and MERGED into the 0600 vars file the manifest step writes.

Shipped base64 in the task's environment and run with ``python3``, so the runner image
(chrweav/ansible-winrm) is unchanged: it has ``requests`` and ``cryptography``, and this
uses nothing else. Kept as a module so the tests import the very code that ships.

Nothing secret is ever printed: not the token, the proof, the key or a value. A failure
prints what failed and the dashboard's own reason, which never carries one either.
"""
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import quote

# ── agent_sealing's format, restated (tests pin it byte-for-byte) ─────────────
SEAL_VERSION = 1
SEAL_ALG = "X25519-HKDF-SHA256-AES256GCM"
SEAL_INFO = b"vm-dashboard/agent-secret-seal/v1"
REF = "ansible-vars"
STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
BINDING_HEADER = "x-dashboard-runner-binding"


def binding(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def gcp_audience(url: str, token: str) -> str:
    return f"{url}?binding={binding(token)}"


def _serialize(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_aad(*, agent_id: str, audience: str, epk: str, job_id: str, ref: str) -> bytes:
    return _serialize({"agent_id": str(agent_id), "aud": str(audience), "epk": str(epk),
                       "job_id": str(job_id), "ref": str(ref), "v": SEAL_VERSION})


def keypair() -> tuple:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv = X25519PrivateKey.generate()
    raw_priv = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption())
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw)
    return base64.b64encode(raw_priv).decode(), base64.b64encode(raw_pub).decode()


def open_sealed(private_b64: str, env: dict, *, agent_id: str, audience: str,
                job_id: str, ref: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                                  X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    if env.get("v") != SEAL_VERSION or env.get("alg") != SEAL_ALG:
        raise ValueError("the dashboard sealed with a version this runner does not know")
    priv = X25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    epk_raw = base64.b64decode(env["epk"])
    shared = priv.exchange(X25519PublicKey.from_public_bytes(epk_raw))
    me = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=SEAL_INFO).derive(epk_raw + me + shared)
    aad = seal_aad(agent_id=agent_id, audience=audience, epk=env["epk"], job_id=job_id,
                   ref=ref)
    plain = AESGCM(key).decrypt(base64.b64decode(env["nonce"]), base64.b64decode(env["ct"]),
                                aad)
    return str(json.loads(plain.decode("utf-8"))["secret"])


# ── ECS: a presigned GetCallerIdentity, signed with the task role ─────────────

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sts_proof(region: str, access_key: str, secret_key: str, session_token: str,
              token: str, now: _dt.datetime = None) -> dict:
    """SigV4 for ``POST https://sts.<region>.amazonaws.com/`` GetCallerIdentity. Nothing
    is sent from here: the dashboard sends it, and STS's answer names the caller."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    host = f"sts.{region}.amazonaws.com"
    headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8",
               "host": host, "x-amz-date": amz_date, BINDING_HEADER: binding(token)}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = ";".join(sorted(headers))
    canonical = "\n".join([
        "POST", "/", "",
        "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
        signed, hashlib.sha256(STS_BODY.encode()).hexdigest()])
    scope = f"{day}/{region}/sts/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, "sts", "aws4_request"):
        k = _sign(k, part)
    sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    return {"method": "POST", "url": f"https://{host}/", "headers": headers,
            "body": STS_BODY}


def _ecs_proof(token: str, region: str, rel: str) -> dict:
    import requests
    if not rel:
        raise RuntimeError("this ECS task has no task role (no credentials endpoint) — set "
                           "the runner's task role ARN in Config Management settings")
    c = requests.get(f"http://169.254.170.2{rel}", timeout=5).json()
    return sts_proof(region, c["AccessKeyId"], c["SecretAccessKey"], c.get("Token", ""),
                     token)


def _gcp_proof(token: str, url: str) -> dict:
    import requests
    aud = quote(gcp_audience(url, token), safe="")
    r = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
        f"default/identity?audience={aud}&format=full",
        headers={"Metadata-Flavor": "Google"}, timeout=5)
    r.raise_for_status()
    return {"id_token": r.text.strip()}


def merge_vars(path: str, values: dict) -> None:
    current = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
    current.update(values)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(current, fh)
    os.chmod(path, 0o600)


def main(environ=None) -> int:
    import requests
    e = environ if environ is not None else os.environ
    url, token = e["RUNNER_CREDENTIAL_URL"], e["RUNNER_CREDENTIAL_TOKEN"]
    platform, job = e["RUNNER_CREDENTIAL_PLATFORM"], e["RUNNER_CREDENTIAL_JOB"]
    try:
        proof = (_ecs_proof(token, e["RUNNER_CREDENTIAL_STS_REGION"],
                            e.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", ""))
                 if platform == "ecs" else _gcp_proof(token, url))
        private, public = keypair()
        r = requests.post(url, json={"token": token, "proof": proof, "reply_key": public},
                          timeout=30)
    except Exception as exc:  # noqa: BLE001 — the type only; never the request
        print(f"runner-credential: could not ask the dashboard ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail") or "")
        except Exception:  # noqa: BLE001
            pass
        print(f"runner-credential: the dashboard refused ({r.status_code}): {detail}",
              file=sys.stderr)
        return 3
    values = json.loads(open_sealed(private, r.json()["sealed"],
                                    agent_id=f"runner:{platform}",
                                    audience=e["RUNNER_CREDENTIAL_AUDIENCE"], job_id=job,
                                    ref=REF))
    merge_vars(e["RUNNER_CREDENTIAL_VARS_FILE"], values)
    print(f"runner-credential: collected {len(values)} credential var(s) for this run",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
