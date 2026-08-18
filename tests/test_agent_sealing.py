"""Invariants for sealing a just-in-time credential to a remote agent.

Every assertion here is a silent failure if it regresses. A seal that still opens after
the connection ref, the job or the agent changed is a seal bound to nothing in particular —
and the consequence is not an error but a *wrong credential delivered successfully*. The
``ref`` case is the sharp one: unbound, a credential released for one connection can be
relabelled as the credential for a connection pointing at a host the attacker controls,
and the agent then presents a vCenter administrator password to them.

The AAD is never transmitted; both ends rebuild it from state they already trust. So these
tests pass the context in explicitly on both sides, exactly as the real callers do.

Needs ``cryptography`` (already a dependency — config_service imports Fernet from it).
Runs under pytest, or standalone:
    python tests/test_agent_sealing.py
"""
import base64
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "agent_sealing.py")
_spec = importlib.util.spec_from_file_location("agent_sealing", _PATH)
sealing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sealing)

CTX = dict(agent_id="ag-1", audience="https://agents.example.com",
           job_id="job-1", ref="dc1-vcenter")
SECRET = "vCenter!Admin#2026"


def _pair():
    return sealing.generate_reply_keypair()


def _mutate(envelope, field, index=0, xor=1):
    """Flip one bit of a base64 field, leaving everything else intact."""
    out = dict(envelope)
    raw = bytearray(base64.b64decode(out[field]))
    raw[index] ^= xor
    out[field] = base64.b64encode(bytes(raw)).decode("ascii")
    return out


# ── the happy path ────────────────────────────────────────────────────────────

def test_a_sealed_credential_round_trips():
    priv, pub = _pair()
    assert sealing.open_sealed(priv, sealing.seal(pub, SECRET, **CTX), **CTX) == SECRET


def test_the_envelope_carries_no_context_and_no_plaintext():
    """The AAD is rebuilt locally, never sent. If it were sent, the sender would be
    declaring the context and the receiver merely confirming the declaration — a binding
    that binds nothing, and the usual way this design is got wrong."""
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    assert set(envelope) == {"v", "alg", "epk", "nonce", "ct"}
    for absent in ("aad", "salt", "info", "agent_id", "job_id", "ref", "secret"):
        assert absent not in envelope
    assert SECRET not in str(envelope)


def test_two_seals_of_one_secret_differ():
    """Proves the ephemeral keypair really is per-seal. If it were not, the fixed-nonce
    question would become load-bearing."""
    priv, pub = _pair()
    a, b = sealing.seal(pub, SECRET, **CTX), sealing.seal(pub, SECRET, **CTX)
    assert a["ct"] != b["ct"] and a["epk"] != b["epk"] and a["nonce"] != b["nonce"]
    assert sealing.open_sealed(priv, a, **CTX) == sealing.open_sealed(priv, b, **CTX)


def test_an_empty_and_a_large_secret_both_survive():
    """An empty credential is returned, not refused — whether that is acceptable is the
    caller's policy. And nothing here caps the plaintext."""
    priv, pub = _pair()
    assert sealing.open_sealed(priv, sealing.seal(pub, "", **CTX), **CTX) == ""
    big = "z" * 100_000
    assert sealing.open_sealed(priv, sealing.seal(pub, big, **CTX), **CTX) == big


# ── the AAD binding, one field at a time ──────────────────────────────────────

def test_every_context_field_is_bound_independently():
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    for field in ("agent_id", "audience", "job_id", "ref"):
        wrong = dict(CTX)
        wrong[field] = "tampered"
        try:
            sealing.open_sealed(priv, envelope, **wrong)
        except sealing.SealError:
            continue
        raise AssertionError(f"{field} is not bound into the tag")


def test_the_ref_binding_stops_credential_relabelling():
    """Named separately from the loop above because it is the one that turns credential
    confusion into credential exfiltration, and it deserves to fail by name."""
    priv, pub = _pair()
    for_vcenter = sealing.seal(pub, SECRET, **{**CTX, "ref": "dc1-vcenter"})
    try:
        sealing.open_sealed(priv, for_vcenter, **{**CTX, "ref": "attacker-controlled"})
    except sealing.SealError:
        return
    raise AssertionError("a seal for one connection opened as another")


def test_a_field_value_cannot_inject_a_delimiter():
    """Canonical JSON, so a ref containing what looks like the field separator cannot
    shift the meaning of its neighbour. Same property `agent_signing` relies on."""
    a = sealing.seal_aad(agent_id="a", audience="b", epk="c", job_id="d",
                         ref='x","job_id":"evil')
    b = sealing.seal_aad(agent_id="a", audience="b", epk="c", job_id="evil", ref="x")
    assert a != b


def test_the_transported_ephemeral_key_is_covered_by_the_tag():
    priv, pub = _pair()
    a = sealing.seal(pub, SECRET, **CTX)
    b = sealing.seal(pub, SECRET, **CTX)
    spliced = dict(a, epk=b["epk"])
    try:
        sealing.open_sealed(priv, spliced, **CTX)
    except sealing.SealError:
        return
    raise AssertionError("a ciphertext re-paired with a different epk still opened")


# ── refusals ──────────────────────────────────────────────────────────────────

def test_a_different_recipient_key_cannot_open_it():
    _, pub = _pair()
    other_priv, _ = _pair()
    try:
        sealing.open_sealed(other_priv, sealing.seal(pub, SECRET, **CTX), **CTX)
    except sealing.SealError:
        return
    raise AssertionError("a seal opened under the wrong key")


def test_tampering_with_any_transported_field_refuses():
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    for field in ("ct", "nonce", "epk"):
        try:
            sealing.open_sealed(priv, _mutate(envelope, field), **CTX)
        except sealing.SealError:
            continue
        raise AssertionError(f"a flipped bit in {field!r} was accepted")


def test_a_truncated_or_extended_ciphertext_refuses():
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    raw = base64.b64decode(envelope["ct"])
    for mutated in (raw[:-1], raw + b"\x00"):
        bad = dict(envelope, ct=base64.b64encode(mutated).decode("ascii"))
        try:
            sealing.open_sealed(priv, bad, **CTX)
        except sealing.SealError:
            continue
        raise AssertionError("a resized ciphertext was accepted")


def test_the_gcm_tag_is_checked():
    """The tag is the last 16 bytes; flipping one must refuse."""
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    try:
        sealing.open_sealed(priv, _mutate(envelope, "ct", index=-1), **CTX)
    except sealing.SealError:
        return
    raise AssertionError("a flipped tag byte was accepted")


def test_a_version_or_algorithm_mismatch_says_which():
    """A deliberate deviation from `verify_bytes`' one-answer doctrine: both ends are
    already authenticated, the distinction is not attacker-useful, and a failed job renders
    this text and nothing else — so the operator has to be able to tell a version skew from
    a wrong key."""
    priv, pub = _pair()
    envelope = sealing.seal(pub, SECRET, **CTX)
    try:
        sealing.open_sealed(priv, dict(envelope, v=99), **CTX)
        raise AssertionError("a v99 envelope was accepted")
    except sealing.SealError as exc:
        assert "version" in str(exc).lower()
    try:
        sealing.open_sealed(priv, dict(envelope, alg="none"), **CTX)
        raise AssertionError("an unknown alg was accepted")
    except sealing.SealError as exc:
        assert "algorithm" in str(exc).lower()


def test_the_algorithm_field_selects_nothing():
    """`alg` exists to produce a readable refusal, not to choose a cipher. An `alg` that
    selects an algorithm is the JWT `alg:none` mistake."""
    src = open(_PATH, encoding="utf-8").read()
    assert 'envelope.get("alg") != SEAL_ALG' in src
    for forbidden in ("alg_map", "ALGORITHMS[", "getattr(hashes,"):
        assert forbidden not in src


def test_an_all_zero_peer_key_is_an_ordinary_refusal():
    """A small-order public key drives the shared secret to all zeroes; `cryptography`
    rejects it. It must not be distinguishable from any other refusal, or whoever supplied
    the key learns they hit the check."""
    zero = base64.b64encode(bytes(32)).decode("ascii")
    try:
        sealing.seal(zero, SECRET, **CTX)
    except sealing.SealError:
        return
    raise AssertionError("an all-zero peer key produced a seal")


def test_malformed_input_raises_sealerror_and_nothing_else():
    priv, pub = _pair()
    good = sealing.seal(pub, SECRET, **CTX)
    cases = [
        {}, {"v": 1}, dict(good, epk="not base64!!"), dict(good, epk=""),
        dict(good, nonce=base64.b64encode(b"short").decode("ascii")),
        dict(good, ct=""), dict(good, epk=base64.b64encode(b"tooshort").decode("ascii")),
    ]
    for bad in cases:
        try:
            sealing.open_sealed(priv, bad, **CTX)
        except sealing.SealError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{type(exc).__name__} escaped for {bad!r}") from exc
        raise AssertionError(f"malformed envelope accepted: {bad!r}")
    for not_a_dict in (None, "", [], 7):
        try:
            sealing.open_sealed(priv, not_a_dict, **CTX)
        except sealing.SealError:
            continue
        raise AssertionError(f"{not_a_dict!r} was accepted as an envelope")


def test_a_bad_reply_key_is_refused_before_anything_is_sealed():
    for bad in ("", "not base64!!", base64.b64encode(b"short").decode("ascii")):
        try:
            sealing.seal(bad, SECRET, **CTX)
        except sealing.SealError:
            continue
        raise AssertionError(f"sealed to a malformed reply key: {bad!r}")


# ── construction details worth pinning ────────────────────────────────────────

def test_both_public_keys_go_into_the_key_derivation():
    """Mirrors HPKE's `kem_context = enc || pkRm`. Deriving from the raw DH output alone is
    the classic omission and it loses the binding between ciphertext and recipient, so the
    IKM composition is pinned rather than left to a comment."""
    src = open(_PATH, encoding="utf-8").read()
    assert "epk_raw + recipient_raw + shared" in src


def test_the_nonce_is_random_and_the_right_length():
    priv, pub = _pair()
    nonces = {sealing.seal(pub, SECRET, **CTX)["nonce"] for _ in range(20)}
    assert len(nonces) == 20, "nonces repeated — is os.urandom still being used?"
    assert len(base64.b64decode(nonces.pop())) == sealing.NONCE_BYTES == 12


def test_the_info_string_is_constant():
    """Domain separation only. All variable context belongs in the AAD, so that there is
    exactly one canonicalization of variable data rather than two that have to agree."""
    assert isinstance(sealing.SEAL_INFO, bytes)
    src = open(_PATH, encoding="utf-8").read()
    assert "info=SEAL_INFO" in src
    assert "info=" not in src.replace("info=SEAL_INFO", "")


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
