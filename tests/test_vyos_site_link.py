"""The VyOS site link: an on-prem AD on a GCP VPC over WireGuard.

What these pin:

- the keys: a generated pair is a valid WireGuard pair, a pasted on-prem key must be a
  44-character PUBLIC key, and the peer's private key goes to GCP Secret Manager only --
  never onto the row, the job, the Terraform variables, the rendered play or the
  on-prem commands;
- validation: only a registered on-prem AD can be linked; subnets are CIDRs, controllers
  are addresses inside an on-prem subnet, nothing overlaps the tunnel, one link per
  project; a VyOS 1.4 image is required;
- the build: Terraform is the gcp_vyos_peer module, the peer's addresses are read back,
  and the row is ``available`` ONLY when the play reports the tunnel UP --
  ``awaiting_onprem`` when it is configured but unanswered, ``failed`` with no sentinel;
  Check link re-runs the play without Terraform;
- a site link is never offered as something to join, and cannot be destroyed while a
  DNS link resolves through it; destroy deletes the stored key;
- the playbook, module, runner image and bake carry what the rest relies on.

Run: python tests/test_vyos_site_link.py   (or under pytest)
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vyos-link-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vyos-link-tests")

from web_dashboard.database import (Base, Job, ManagedDirectory, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import config_service  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402
from web_dashboard.services import secrets_backend_service as sbs  # noqa: E402
from web_dashboard.services import vyos_link_service as vls  # noqa: E402

Base.metadata.create_all(bind=engine)

_CFG = {"gcp_project_id": "proj-1", "gcp_ssh_key_secret_name": "ansible-key"}
_orig_get = config_service.get
config_service.get = lambda key, default="", workgroup=None: _CFG.get(key, default)

_STORED: dict = {}
_DELETED: list = []


def _write(backend, key, value):
    assert backend == "gcp_sm"
    ref = f"sm-{key}"
    _STORED[ref] = value
    return ref


sbs.write_sync = _write
sbs.delete_sync = lambda backend, ref: _DELETED.append((backend, ref))

ONPREM_PUB = vls.generate_keypair()[1]
IMAGE = "projects/p/global/images/vyos-cell-1-4"


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _onprem(db):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version="2.8.0", is_active=True, allowed_job_types=None,
                    created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return ds.register_onprem(
        db, name=f"c{uuid.uuid4().hex[:6]}.example.com", provider="onprem_ad",
        host=f"dc-{uuid.uuid4().hex[:6]}", agent_id=a.id, created_by="t",
        managed_account={"system_id": 11, "account_id": 22, "account_name": "svc-join"})


def _args(onprem, **kw):
    args = dict(onprem_directory_id=onprem.id, project="proj-1", zone="us-central1-a",
                network="default", subnetwork="default", image_name="vyos-cell-1-4",
                image_self_link=IMAGE, release="1.4", onprem_public_key=ONPREM_PUB,
                onprem_subnets=["10.0.0.0/24"], cloud_networks=["10.99.0.0/16"],
                dns_ips=["10.0.0.10"], created_by="t")
    args.update(kw)
    return args


def _refused(fn, needle):
    try:
        fn()
    except vls.VyosLinkError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected {needle!r}")


def _link(db, onprem, **kw):
    out = vls.provision(db, **_args(onprem, **kw))
    row = db.query(ManagedDirectory).filter(ManagedDirectory.id == out["directory_id"]).one()
    return row, out


# ── keys ──────────────────────────────────────────────────────────────────────

def test_generated_keys_are_a_wireguard_pair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv, pub = vls.generate_keypair()
    assert len(priv) == 44 and len(pub) == 44
    key = X25519PrivateKey.from_private_bytes(base64.b64decode(priv))
    raw = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    assert base64.b64encode(raw).decode() == pub
    assert vls.public_key_ok(pub)
    for bad in ("", "abc", "x" * 44, pub[:-1], "ssh-ed25519 AAAA"):
        assert not vls.public_key_ok(bad), bad


def test_private_key_lives_only_in_secret_manager():
    db = SessionLocal()
    row, out = _link(db, _onprem(db))
    cfg = vls.link_config(row)
    ref = cfg["wg_secret_ref"]
    private = _STORED[ref]
    assert len(private) == 44 and private != cfg["peer_public_key"]
    job = db.query(Job).filter(Job.id == out["job_id"]).one()
    blobs = [row.link_config, json.dumps(ds.to_dict(row)), job.extra_data or "",
             json.dumps(vls.tf_variables(row)), vls.render_playbook(row),
             vls.onprem_commands(row)]
    for blob in blobs:
        assert private not in blob
    assert ref not in json.dumps(ds.to_dict(row))      # not even the reference is shown
    db.close()


# ── validation ────────────────────────────────────────────────────────────────

def test_validation():
    db = SessionLocal()
    onprem = _onprem(db)
    _refused(lambda: vls.provision(db, **_args(onprem, onprem_directory_id="x")),
             "register the domain")
    _refused(lambda: vls.provision(db, **_args(onprem, onprem_public_key="nope")),
             "PUBLIC key")
    _refused(lambda: vls.provision(db, **_args(onprem, release="1.3")), "VyOS 1.4")
    _refused(lambda: vls.provision(db, **_args(onprem, image_name="debian-12",
                                                image_self_link="debian-12")),
             "does not look like a VyOS image")
    _refused(lambda: vls.provision(db, **_args(onprem, onprem_subnets=["10.0.0.0/33"])),
             "not CIDR")
    _refused(lambda: vls.provision(db, **_args(onprem, dns_ips=["dc01"])),
             "not IP addresses")
    _refused(lambda: vls.provision(db, **_args(onprem, dns_ips=["192.168.1.5"])),
             "not inside any on-prem subnet")
    _refused(lambda: vls.provision(db, **_args(onprem, cloud_networks=["10.255.0.0/16"])),
             "overlaps the tunnel")
    _link(db, onprem)
    _refused(lambda: vls.provision(db, **_args(onprem)), "already has a site link")
    db.close()


def test_runner_key_and_secret_manager_are_required():
    saved = dict(_CFG)
    try:
        _CFG.pop("gcp_ssh_key_secret_name")
        assert "VYOS_RUNNER_PUBKEY" in vls.provision_problem(
            image_name="vyos-cell", image_self_link=IMAGE, release="1.4",
            onprem_public_key=ONPREM_PUB)
        _CFG.clear()
        assert "Secret Manager" in vls.provision_problem(
            image_name="vyos-cell", image_self_link=IMAGE, release="1.4",
            onprem_public_key=ONPREM_PUB)
    finally:
        _CFG.clear()
        _CFG.update(saved)


# ── module, play, commands ────────────────────────────────────────────────────

def test_module_and_variables():
    db = SessionLocal()
    row, _ = _link(db, _onprem(db))
    assert ds.template_dir("gcp", vls.PROVIDER).endswith(os.path.join("directory",
                                                                      "gcp_vyos_peer"))
    tf = ds._tf_variables(row)
    assert tf["onprem_subnets"] == ["10.0.0.0/24"] and tf["cloud_networks"] == ["10.99.0.0/16"]
    assert tf["image"] == IMAGE and tf["machine_type"] == "e2-small"
    assert tf["wireguard_source_ranges"] == ["0.0.0.0/0"]
    db.close()


def test_rendered_play_carries_vars_but_no_secret():
    import yaml
    db = SessionLocal()
    row, _ = _link(db, _onprem(db))
    vls.read_outputs(row, {"public_ip": {"value": "34.1.2.3"},
                           "internal_ip": {"value": "10.99.1.5"},
                           "instance_name": {"value": "vyos-link-x"}})
    play = yaml.safe_load(vls.render_playbook(row))[0]
    v = play["vars"]
    assert v["onprem_public_key"] == ONPREM_PUB
    assert v["listen_address"] == "10.99.1.5" and v["probe_address"] == "10.0.0.10"
    assert v["ad_domain"] == row.name and v["dns_servers"] == ["10.0.0.10"]
    assert "wg_private_key" not in v
    assert v["ansible_host_key_checking"] is False
    db.close()


def test_onprem_commands():
    db = SessionLocal()
    row, _ = _link(db, _onprem(db))
    vls.read_outputs(row, {"public_ip": "34.1.2.3", "internal_ip": "10.99.1.5",
                           "instance_name": "vyos-link-x"})
    text = vls.onprem_commands(row)
    cfg = vls.link_config(row)
    assert f"public-key '{cfg['peer_public_key']}'" in text
    assert "address '34.1.2.3'" in text and "persistent-keepalive '25'" in text
    assert "set protocols static route 10.99.0.0/16 interface wg0" in text
    assert "allowed-ips '10.255.255.1/32'" in text
    assert "private-key" not in text
    db.close()


def test_parse_state_reads_the_last_sentinel():
    assert vls.parse_state('"msg": "VMDASH-VYOS-LINK:CONFIGURED"\n'
                           '"msg": "VMDASH-VYOS-LINK:UP"') == "up"
    assert vls.parse_state("VMDASH-VYOS-LINK:CONFIGURED\nVMDASH-VYOS-LINK:WAITING") == "waiting"
    assert vls.parse_state("PLAY RECAP failed=1") == ""


# ── the build ─────────────────────────────────────────────────────────────────

def _run(db, row, job_id, state, *, check_only=False):
    from web_dashboard.services import terraform
    calls = []

    async def apply(*a, **k):
        calls.append("apply")
        return {"public_ip": {"value": "34.1.2.3"}, "internal_ip": {"value": "10.99.1.5"},
                "instance_name": {"value": "vyos-link-x"}}

    async def configure(r, j):
        calls.append("configure")
        return state, f"... VMDASH-VYOS-LINK:{state.upper()} ..." if state else "boom"

    saved = terraform.apply, vls.configure
    terraform.apply, vls.configure = apply, configure
    try:
        asyncio.run(vls.run(db, row, job_id, check_only=check_only))
    finally:
        terraform.apply, vls.configure = saved
    db.refresh(row)
    return calls


def test_available_only_when_the_tunnel_answers():
    db = SessionLocal()
    row, out = _link(db, _onprem(db))
    assert _run(db, row, out["job_id"], "waiting") == ["apply", "configure"]
    assert row.status == vls.STATUS_AWAITING and "Check link" in row.error_message
    assert vls.link_config(row)["peer_internal_ip"] == "10.99.1.5"
    check = vls.start_check(db, directory_id=row.id, created_by="t")
    assert _run(db, row, check["job_id"], "up", check_only=True) == ["configure"]
    assert row.status == "available" and row.error_message is None
    db.close()


def test_no_sentinel_is_a_failure_not_a_success():
    db = SessionLocal()
    row, out = _link(db, _onprem(db))
    _run(db, row, out["job_id"], "")
    assert row.status == "failed" and "without reporting" in row.error_message
    job = db.query(Job).filter(Job.id == out["job_id"]).one()
    assert job.status == "failed"
    db.close()


def test_never_joinable_and_guarded_from_destroy():
    db = SessionLocal()
    onprem = _onprem(db)
    row, out = _link(db, onprem)
    _run(db, row, out["job_id"], "up")
    assert row.id not in [r.id for r in ds.joinable_for(db, "gcp")]
    dns = ds.provision_dns_link(db, onprem_directory_id=onprem.id, project="proj-1",
                                networks=["default"], dns_ips=["10.99.1.5"], created_by="t")
    try:
        ds.start_decommission(db, directory_id=row.id, created_by="t")
    except ds.DirectoryError as e:
        assert "DNS link" in str(e) and "10.99.1.5" in str(e)
    else:
        raise AssertionError("destroyed a peer a DNS link resolves through")
    db.query(ManagedDirectory).filter(ManagedDirectory.id == dns["directory_id"]).update(
        {"status": "deleted"})
    db.commit()
    ds.start_decommission(db, directory_id=row.id, created_by="t")
    db.close()


def test_destroy_deletes_the_key():
    db = SessionLocal()
    row, out = _link(db, _onprem(db))
    ref = vls.link_config(row)["wg_secret_ref"]
    from web_dashboard.services import terraform

    async def destroy(*a, **k):
        return None

    saved = terraform.destroy
    terraform.destroy = destroy
    try:
        dec = ds.start_decommission(db, directory_id=row.id, created_by="t")
        asyncio.run(ds.run_decommission(db, directory_id=row.id, job_id=dec["job_id"]))
    finally:
        terraform.destroy = saved
    db.refresh(row)
    assert row.status == "deleted"
    assert ("gcp_sm", ref) in _DELETED
    db.close()


# ── what the rest relies on ───────────────────────────────────────────────────

def test_playbook_shape():
    import yaml
    src = _read("web_dashboard", "services", "builtin_playbooks", "vyos-wireguard-peer.yml")
    play = yaml.safe_load(src)[0]
    tasks = {t["name"]: t for t in play["tasks"]}
    apply = tasks["Apply it, with the private key, and save"]
    assert apply.get("no_log") is True and "wg_private_key" in apply["vyos.vyos.vyos_config"]["lines"]
    assert "wg_private_key" not in tasks["Build the configuration (no secrets)"][
        "ansible.builtin.set_fact"]["_config"]
    assert play["vars"]["ansible_network_cli_ssh_type"] == "paramiko"
    for state in ("CONFIGURED", "UP", "WAITING"):
        assert state in src


def test_module_runner_image_and_bake():
    tf = _read("terraform", "directory", "gcp_vyos_peer", "main.tf")
    for needle in ("can_ip_forward = true", 'resource "google_compute_route" "onprem"',
                   "next_hop_instance", '"35.199.192.0/19"', 'output "internal_ip"',
                   'output "public_ip"'):
        assert needle in tf, needle
    assert "private_key" not in tf
    assert "metadata =" not in tf and "metadata {" not in tf   # nothing rides metadata
    assert '"paramiko>=3.4"' in _read("runners", "ansible-winrm", "Dockerfile")
    bake = _read("provisioners", "net", "vyos-cell.sh")
    assert "VYOS_RUNNER_PUBKEY" in bake and "public-keys 'runner'" in bake


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok   {_name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {_name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
