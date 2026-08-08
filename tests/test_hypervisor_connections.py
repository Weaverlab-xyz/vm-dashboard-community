"""Tests for hypervisor_connections: the resolver, the seed, and the credential boundary.

Runs against a real throwaway SQLite database, because the things worth pinning here —
the unique constraint, "copies not moves", ciphertext at rest — are storage behaviour.

Runs under pytest, or standalone:  python tests/test_hypervisor_connections.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "hv.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.database import Base, engine, SessionLocal, HypervisorConnection
    from web_dashboard.services import config_service
    from web_dashboard.services import hypervisor_connection_service as hcs
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


def _fresh():
    """A session with an empty table and no legacy config."""
    db = SessionLocal()
    db.query(HypervisorConnection).delete()
    db.commit()
    for spec in hcs._SINGLETON_SPEC.values():
        config_service.set(spec["host"], "")
    config_service.set(hcs._SEED_MARK, "")
    return db


def _legacy_proxmox():
    config_service.set("proxmox_host", "10.1.0.5")
    config_service.set("proxmox_token_id", "root@pam!dash")
    config_service.set("proxmox_token_secret", "s3cr3t")


# ── resolution order ───────────────────────────────────────────────────────────

def test_an_empty_table_falls_back_to_the_legacy_singletons():
    """The COMPAT branch. Without it, shipping this table is a breaking change for
    every install that has PROXMOX_HOST set and nothing else."""
    db = _fresh()
    _legacy_proxmox()
    conn = hcs.resolve(db, "proxmox")
    assert conn.host == "10.1.0.5"
    assert conn.secret == "s3cr3t"
    assert conn.options["token_id"] == "root@pam!dash"
    assert conn.port == 8006          # the per-kind default, not a guess
    db.close()


def test_an_unconfigured_kind_says_what_to_do():
    db = _fresh()
    try:
        hcs.resolve(db, "nutanix")
    except hcs.HypervisorConnectionError as exc:
        assert "Connections page" in str(exc)
    else:
        raise AssertionError("expected an error")
    db.close()


def test_a_single_connection_resolves_without_a_default():
    db = _fresh()
    hcs.create(db, kind="nutanix", name="pc", created_by="t", host="10.0.0.1", secret="p")
    db.query(HypervisorConnection).update({"is_default": False})
    db.commit()
    assert hcs.resolve(db, "nutanix").name == "pc"
    db.close()


def test_two_connections_with_no_default_refuse_rather_than_guess():
    db = _fresh()
    hcs.create(db, kind="nutanix", name="a", created_by="t", host="1.1.1.1", secret="p")
    hcs.create(db, kind="nutanix", name="b", created_by="t", host="2.2.2.2", secret="p")
    db.query(HypervisorConnection).update({"is_default": False})
    db.commit()
    try:
        hcs.resolve(db, "nutanix")
    except hcs.HypervisorConnectionError as exc:
        assert "none is the default" in str(exc)
    else:
        raise AssertionError("guessing between two vCenters is worse than refusing")
    db.close()


def test_an_explicit_id_never_silently_falls_back():
    db = _fresh()
    a = hcs.create(db, kind="nutanix", name="a", created_by="t", host="1.1.1.1", secret="p")
    hcs.create(db, kind="proxmox", name="p", created_by="t", host="2.2.2.2", secret="p")
    assert hcs.resolve(db, "nutanix", a["id"]).name == "a"
    for bad, why in ((a["id"], "wrong kind"), ("no-such-id", "missing")):
        try:
            hcs.resolve(db, "proxmox", bad)
        except hcs.HypervisorConnectionError:
            pass
        else:
            raise AssertionError(f"{why} must be an error, not a fallback")
    db.close()


def test_an_inactive_connection_is_refused_by_id():
    db = _fresh()
    a = hcs.create(db, kind="xcpng", name="a", created_by="t", host="1.1.1.1", secret="p")
    hcs.update(db, a["id"], is_active=False)
    try:
        hcs.resolve(db, "xcpng", a["id"])
    except hcs.HypervisorConnectionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("expected an error")
    db.close()


# ── the seed ───────────────────────────────────────────────────────────────────

def test_the_seed_copies_and_is_idempotent_twice_over():
    db = _fresh()
    _legacy_proxmox()
    assert hcs.seed_from_settings(db) == 1
    assert hcs.seed_from_settings(db) == 0, "the config mark must short-circuit"

    # An operator can delete the mark; the per-kind existence check still holds.
    config_service.set(hcs._SEED_MARK, "")
    assert hcs.seed_from_settings(db) == 0
    assert db.query(HypervisorConnection).filter_by(kind="proxmox").count() == 1
    db.close()


def test_the_seed_leaves_the_legacy_keys_alone():
    """Copies, never moves — that is what makes a rollback a no-op rather than an
    outage. The previous image reads the keys it always read."""
    db = _fresh()
    _legacy_proxmox()
    hcs.seed_from_settings(db)
    assert config_service.get("proxmox_host") == "10.1.0.5"
    assert config_service.get("proxmox_token_secret") == "s3cr3t"
    db.close()


def test_a_seeded_connection_matches_what_the_old_config_meant():
    db = _fresh()
    _legacy_proxmox()
    before = hcs.resolve(db, "proxmox")          # via the COMPAT branch
    hcs.seed_from_settings(db)
    after = hcs.resolve(db, "proxmox")           # via the table
    for attr in ("host", "port", "username", "secret", "verify_ssl", "options"):
        assert getattr(before, attr) == getattr(after, attr), attr
    db.close()


def test_the_seed_skips_a_kind_an_operator_already_configured():
    db = _fresh()
    _legacy_proxmox()
    hcs.create(db, kind="proxmox", name="mine", created_by="t", host="9.9.9.9", secret="p")
    assert hcs.seed_from_settings(db) == 0
    assert hcs.resolve(db, "proxmox").host == "9.9.9.9"
    db.close()


# ── the credential boundary ────────────────────────────────────────────────────

def test_the_secret_is_ciphertext_at_rest_and_plaintext_only_on_resolve():
    db = _fresh()
    hcs.create(db, kind="vsphere", name="vc", created_by="t",
               host="vc.corp", username="svc", secret="hunter2")
    row = db.query(HypervisorConnection).filter_by(name="vc").one()
    assert row.secret_enc and "hunter2" not in row.secret_enc
    assert hcs.resolve(db, "vsphere").secret == "hunter2"
    db.close()


def test_the_api_projection_carries_no_credential_field():
    db = _fresh()
    out = hcs.create(db, kind="vsphere", name="vc", created_by="t",
                     host="vc.corp", secret="hunter2")
    assert "hunter2" not in repr(out)
    for banned in ("secret", "secret_enc", "password", "token"):
        assert banned not in out, f"serialize() must not expose {banned}"
    assert out["has_secret"] is True      # the boolean is the whole story the UI needs
    db.close()


def test_repr_never_prints_the_credential():
    # A Connection reaches tracebacks and debug logs; a default dataclass repr would
    # print the password in both.
    db = _fresh()
    hcs.create(db, kind="vsphere", name="vc", created_by="t", host="h", secret="hunter2")
    assert "hunter2" not in repr(hcs.resolve(db, "vsphere"))
    db.close()


def test_a_blank_secret_on_update_leaves_the_stored_one_alone():
    # Otherwise editing any other field through a form that does not echo the password
    # silently wipes it.
    db = _fresh()
    c = hcs.create(db, kind="vsphere", name="vc", created_by="t", host="h", secret="keep")
    hcs.update(db, c["id"], name="vc2", secret="")
    assert hcs.resolve(db, "vsphere").secret == "keep"
    db.close()


def test_options_are_allowlisted_per_kind():
    db = _fresh()
    out = hcs.create(db, kind="hyperv", name="hv", created_by="t", host="h",
                     options={"transport": "ntlm", "password": "leak", "junk": 1})
    assert out["options"] == {"transport": "ntlm"}
    db.close()


def test_option_keys_hold_nothing_credential_shaped():
    """`options` is a JSON blob on a row that also holds credentials, so this allowlist
    is what stops a password landing there by accident.

    One explicit exemption, spelled out rather than waved through by a looser pattern:
    Proxmox's ``token_id`` is the token's *name* (``root@pam!dash``) and is useless
    without ``token_secret``, which is stored as the connection's secret like every
    other credential. Anything else matching has to be argued for here.
    """
    import re
    exempt = {"token_id"}
    banned = re.compile(r"pass|secret|token|cred|key", re.I)
    for kind, keys in hcs.OPTION_KEYS.items():
        for key in keys:
            if key in exempt:
                continue
            assert not banned.search(key), f"{kind}.{key} looks credential-shaped"
    # And the exemption must stay narrow: the secret half must never be an option.
    for keys in hcs.OPTION_KEYS.values():
        assert "token_secret" not in keys and "password" not in keys


# ── agent binding ──────────────────────────────────────────────────────────────

def test_an_agent_bound_connection_holds_a_name_and_no_credential():
    db = _fresh()
    out = hcs.create(db, kind="vsphere", name="dc1", created_by="t",
                     agent_id="agt-1", agent_connection_name="dc1-vcenter",
                     host="ignored.example.com", secret="ignored")
    assert out["via_agent"] is True
    assert out["host"] == "", "an agent-bound row must not imply the dashboard dials it"
    assert out["has_secret"] is False
    assert out["agent_connection_name"] == "dc1-vcenter"
    db.close()


def test_an_agent_bound_connection_requires_the_agents_own_name_for_it():
    db = _fresh()
    try:
        hcs.create(db, kind="vsphere", name="dc1", created_by="t", agent_id="agt-1")
    except hcs.HypervisorConnectionError as exc:
        assert "connections.yaml" in str(exc)
    else:
        raise AssertionError("without the name there is nothing to join on")
    db.close()


# ── defaults and deletion ──────────────────────────────────────────────────────

def test_the_first_connection_of_a_kind_becomes_the_default():
    db = _fresh()
    out = hcs.create(db, kind="xcpng", name="only", created_by="t", host="h", secret="p")
    assert out["is_default"] is True, "a single-connection install must never hit the refusal"
    db.close()


def test_set_default_clears_its_siblings():
    db = _fresh()
    a = hcs.create(db, kind="nutanix", name="a", created_by="t", host="1.1.1.1", secret="p")
    b = hcs.create(db, kind="nutanix", name="b", created_by="t", host="2.2.2.2", secret="p")
    hcs.set_default(db, b["id"])
    rows = {r.name: r.is_default for r in db.query(HypervisorConnection).all()}
    assert rows == {"a": False, "b": True}
    assert hcs.resolve(db, "nutanix").name == "b"
    assert a["id"]
    db.close()


def test_deleting_the_default_promotes_a_survivor():
    # Otherwise the remaining connections are unreachable without an explicit id.
    db = _fresh()
    hcs.create(db, kind="nutanix", name="a", created_by="t", host="1.1.1.1", secret="p")
    b = hcs.create(db, kind="nutanix", name="b", created_by="t", host="2.2.2.2", secret="p")
    hcs.set_default(db, b["id"])
    hcs.delete(db, b["id"])
    assert hcs.resolve(db, "nutanix").name == "a"
    db.close()


def test_a_duplicate_name_within_a_kind_is_refused_but_across_kinds_is_fine():
    db = _fresh()
    hcs.create(db, kind="nutanix", name="dc1", created_by="t", host="1.1.1.1", secret="p")
    hcs.create(db, kind="proxmox", name="dc1", created_by="t", host="2.2.2.2", secret="p")
    try:
        hcs.create(db, kind="nutanix", name="dc1", created_by="t", host="3.3.3.3", secret="p")
    except hcs.HypervisorConnectionError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected a duplicate refusal")
    db.close()


def test_two_connections_to_one_host_are_allowed():
    # A read-only sync account and a privileged deploy account against one vCenter is a
    # legitimate setup, so there is deliberately no unique on (kind, host, port).
    db = _fresh()
    hcs.create(db, kind="vsphere", name="ro", created_by="t", host="vc.corp",
               username="svc-ro", secret="p")
    hcs.create(db, kind="vsphere", name="rw", created_by="t", host="vc.corp",
               username="svc-rw", secret="p")
    assert len(hcs.list_connections(db, "vsphere")) == 2
    db.close()


def test_an_unknown_kind_is_refused():
    db = _fresh()
    for call in (lambda: hcs.resolve(db, "virtualbox"),
                 lambda: hcs.create(db, kind="virtualbox", name="x",
                                    created_by="t", host="h")):
        try:
            call()
        except hcs.HypervisorConnectionError:
            pass
        else:
            raise AssertionError("expected an unknown-kind refusal")
    db.close()


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
