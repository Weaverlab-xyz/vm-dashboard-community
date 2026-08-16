"""Invariants for remote-agent request signing.

The agent's credential never crosses the wire — a signature does. These assertions pin
the properties that makes that worth doing, because every one of them is a silent
failure if it regresses: a signature that still verifies after the method, path, body
or audience changed is a signature that authenticates nothing in particular.

The canonicalization test is the subtle one. ``notify_transports.serialize`` and
``agent_signing.serialize`` must produce identical bytes; they are separate functions
only because this module has to stay importable without ``httpx``. If they drift, the
two halves of the codebase disagree about what "the exact bytes" means.

Needs ``cryptography`` (already a dependency — config_service imports Fernet from it).
Runs under pytest, or standalone:
    python tests/test_agent_signing.py
"""
import importlib.util
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "agent_signing.py")
_spec = importlib.util.spec_from_file_location("agent_signing", _PATH)
sig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sig)

PRIV, PUB = sig.generate_keypair()

_REQ = dict(agent_id="a-1", audience="https://agents.example.com",
            method="POST", path="/api/agent/lease", body=b'{"x":1}')


def _signed(**over):
    """Sign a request and return (headers, kwargs-for-verify) with the SAME ts/nonce,
    which is the whole point of sign_request returning them."""
    req = dict(_REQ)
    req.update(over)
    headers = sig.sign_request(PRIV, **req)
    return headers, dict(
        req, timestamp=headers[sig.HEADER_TIMESTAMP],
        nonce=headers[sig.HEADER_NONCE], signature=headers[sig.HEADER_SIGNATURE])


# ── canonicalization ──────────────────────────────────────────────────────────

def test_serialize_matches_notify_transports_byte_for_byte():
    """The two implementations exist for import-weight reasons, not because they are
    allowed to differ. Skips if httpx is absent (this machine), runs in CI."""
    try:
        sys.path.insert(0, _ROOT)
        from web_dashboard.services import notify_transports  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — httpx missing locally; CI has it
        print("     (skipped: notify_transports not importable here)")
        return
    for payload in ({"b": 1, "a": 2}, {"z": [1, 2], "a": {"n": None}}, {}):
        assert sig.serialize(payload) == notify_transports.serialize(payload)


def test_canonical_form_is_deterministic():
    a = sig.canonical_request(agent_id="x", timestamp="1", nonce="n", audience="aud",
                              method="POST", path="/p", body=b"hi")
    b = sig.canonical_request(agent_id="x", timestamp="1", nonce="n", audience="aud",
                              method="POST", path="/p", body=b"hi")
    assert a == b


def test_canonical_form_hashes_the_body_rather_than_embedding_it():
    """A fixed-size canonical form regardless of payload size."""
    small = sig.canonical_request(body=b"x", **{k: v for k, v in dict(
        agent_id="x", timestamp="1", nonce="n", audience="a", method="POST",
        path="/p").items()})
    large = sig.canonical_request(body=b"x" * 100000, **{k: v for k, v in dict(
        agent_id="x", timestamp="1", nonce="n", audience="a", method="POST",
        path="/p").items()})
    assert len(small) == len(large)
    assert b"x" * 100 not in large


def test_method_is_case_normalised():
    lower = sig.canonical_request(agent_id="x", timestamp="1", nonce="n", audience="a",
                                  method="post", path="/p", body=b"")
    upper = sig.canonical_request(agent_id="x", timestamp="1", nonce="n", audience="a",
                                  method="POST", path="/p", body=b"")
    assert lower == upper


def test_field_values_cannot_inject_a_delimiter():
    """Why the canonical form is JSON and not a newline-joined string: a path
    containing what looks like a field separator must not be able to shift the meaning
    of a neighbouring field."""
    a = sig.canonical_request(agent_id="x", timestamp="1", nonce="n", audience="a",
                              method="POST", path='/p","ts":"999', body=b"")
    b = sig.canonical_request(agent_id="x", timestamp="999", nonce="n", audience="a",
                              method="POST", path="/p", body=b"")
    assert a != b
    assert json.loads(a)["ts"] == "1"       # the injected value did not become the ts


# ── round trip ────────────────────────────────────────────────────────────────

def test_a_signed_request_verifies():
    _, kw = _signed()
    assert sig.verify_request(PUB, **kw)


def test_sign_request_returns_the_ts_and_nonce_it_signed():
    """Regenerating either at send time produces a signature that can never verify —
    an annoying bug to find, so the API hands them back."""
    headers, kw = _signed()
    assert headers[sig.HEADER_TIMESTAMP] == kw["timestamp"]
    assert headers[sig.HEADER_NONCE] == kw["nonce"]
    assert headers[sig.HEADER_AGENT_ID] == "a-1"


# ── tamper detection: each covered field, one at a time ───────────────────────

def test_tampering_with_the_body_fails():
    _, kw = _signed()
    kw["body"] = b'{"x":2}'
    assert not sig.verify_request(PUB, **kw)


def test_tampering_with_the_path_fails():
    """A lease signature replayed against /complete is the attack this closes."""
    _, kw = _signed()
    kw["path"] = "/api/agent/jobs/1/complete"
    assert not sig.verify_request(PUB, **kw)


def test_tampering_with_the_method_fails():
    _, kw = _signed()
    kw["method"] = "DELETE"
    assert not sig.verify_request(PUB, **kw)


def test_a_signature_for_another_audience_fails():
    """A signature captured by a staging host an attacker controls must not replay
    against production."""
    _, kw = _signed()
    kw["audience"] = "https://evil.example.com"
    assert not sig.verify_request(PUB, **kw)


def test_tampering_with_the_nonce_fails():
    _, kw = _signed()
    kw["nonce"] = sig.new_nonce()
    assert not sig.verify_request(PUB, **kw)


def test_another_agents_key_does_not_verify():
    _, other_pub = sig.generate_keypair()
    _, kw = _signed()
    assert not sig.verify_request(other_pub, **kw)


# ── freshness ─────────────────────────────────────────────────────────────────

def test_a_stale_timestamp_is_rejected():
    old = str(int(time.time()) - sig.SIGNATURE_WINDOW_SECONDS - 5)
    _, kw = _signed()
    kw["timestamp"] = old
    kw["signature"] = sig.sign_bytes(PRIV, sig.canonical_request(
        agent_id=kw["agent_id"], timestamp=old, nonce=kw["nonce"],
        audience=kw["audience"], method=kw["method"], path=kw["path"], body=kw["body"]))
    assert not sig.verify_request(PUB, **kw), "a correctly-signed but stale request must still fail"


def test_a_future_timestamp_is_rejected():
    """An agent with a fast clock would otherwise mint signatures that outlive the
    window."""
    assert not sig.timestamp_fresh(str(int(time.time()) + 3600))


def test_a_non_numeric_timestamp_is_rejected_without_raising():
    for bad in ("", "abc", None, "12.5.6"):
        assert not sig.timestamp_fresh(bad)


def test_the_window_boundary_is_inclusive_both_ways():
    now = 1_000_000.0
    w = sig.SIGNATURE_WINDOW_SECONDS
    assert sig.timestamp_fresh(str(int(now - w)), now=now)
    assert sig.timestamp_fresh(str(int(now + w)), now=now)
    assert not sig.timestamp_fresh(str(int(now - w - 1)), now=now)


# ── malformed input never raises ──────────────────────────────────────────────

def test_verify_never_raises_on_garbage():
    """These values arrive from the network and from a database column, so every
    malformed shape has to be a False rather than a 500."""
    for pub, s in ((PUB, "not-base64!!"), ("not-base64!!", "abcd"), ("", ""),
                   ("c2hvcnQ=", "abcd"), (PUB, ""), (PUB, "YWJjZA==")):
        assert sig.verify_bytes(pub, s, b"msg") is False


def test_a_public_key_of_the_wrong_length_is_refused():
    import base64
    short = base64.b64encode(b"x" * 31).decode()
    try:
        sig.load_public_key(short)
        raise AssertionError("a 31-byte key should not load")
    except sig.SignatureError:
        pass


def test_generated_keys_are_the_expected_shape():
    import base64
    priv, pub = sig.generate_keypair()
    assert len(base64.b64decode(pub)) == 32
    assert len(base64.b64decode(priv)) == 32
    assert priv != pub


def test_nonces_are_unique_and_long_enough():
    nonces = {sig.new_nonce() for _ in range(1000)}
    assert len(nonces) == 1000
    assert all(len(n) == 32 for n in nonces)   # 128 bits as hex


# ── envelopes (dashboard -> agent) ────────────────────────────────────────────

def test_an_envelope_verifies_and_detects_tampering():
    """Provenance: an attacker who can INSERT a job row still cannot make the agent
    run it, because they cannot produce this signature."""
    env = {"job_id": "j1", "job_type": "agent_discover", "payload": {"scan_kind": "k8s"}}
    s = sig.sign_envelope(PRIV, env)
    assert sig.verify_envelope(PUB, env, s)

    tampered = {"job_id": "j1", "job_type": "agent_discover",
                "payload": {"scan_kind": "both", "cidrs": ["0.0.0.0/0"]}}
    assert not sig.verify_envelope(PUB, tampered, s)


def test_envelope_key_order_does_not_change_the_signature():
    """sort_keys means the agent can rebuild the dict in any order and still verify."""
    a = {"job_id": "j1", "job_type": "agent_discover"}
    b = {"job_type": "agent_discover", "job_id": "j1"}
    assert sig.verify_envelope(PUB, b, sig.sign_envelope(PRIV, a))


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
