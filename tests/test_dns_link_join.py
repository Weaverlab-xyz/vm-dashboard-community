"""GCP DNS link to an on-prem AD, and the agent-driven join of a GCE Windows server.

What these pin:

- a DNS link extends only a registered on-prem AD that has an agent; it needs a project,
  a network and IPv4 DNS addresses; its Terraform module is the forwarding-zone one and
  receives no secret; a built link becomes available with no administrator step;
- the join is an agent_ansible directory run over WinRM on 5986 to the server's private
  address, using the built-in join playbook, naming the deploy job, not a credential;
- its bundle logs on as the server's stored local administrator and joins as the
  directory's Password Safe account, and scrubs both;
- the first-boot metadata opens WinRM and carries no secret;
- the built-in playbook and the example copy are the same file; an unknown built-in is refused.

Run: python tests/test_dns_link_join.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dns-link-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dns-link-tests")

from web_dashboard.database import (Base, Job, ManagedDirectory, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import agent_ansible_bundle as aab  # noqa: E402
from web_dashboard.services import agent_ansible_meta as aam  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402
from web_dashboard.services import domain_join_service as djs  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402

Base.metadata.create_all(bind=engine)
BIND_PW, LOCAL_PW = "J0in!Account", "L0cal!Admin"


def _onprem(db, version="2.8.0"):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=True, allowed_job_types=None,
                    created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return ds.register_onprem(
        db, name=f"c{uuid.uuid4().hex[:6]}.example.com", provider="onprem_ad",
        host=f"dc-{uuid.uuid4().hex[:6]}", agent_id=a.id, created_by="t",
        managed_account={"system_id": 11, "account_id": 22, "account_name": "svc-join"})


def _refused(fn, needle):
    try:
        fn()
    except (ds.DirectoryError, djs.DomainJoinError) as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected {needle!r}")


def _link(db, onprem):
    out = ds.provision_dns_link(db, onprem_directory_id=onprem.id, project="proj-1",
                                networks=["default"], dns_ips=["10.0.0.10"], created_by="t")
    return db.query(ManagedDirectory).filter(ManagedDirectory.id == out["directory_id"]).one(), out


def test_link_validation_and_module():
    db = SessionLocal()
    onprem = _onprem(db)
    args = dict(onprem_directory_id=onprem.id, project="proj-1", networks=["default"],
                dns_ips=["10.0.0.10"], created_by="t")
    _refused(lambda: ds.provision_dns_link(db, **dict(args, onprem_directory_id="x")),
             "register the domain")
    _refused(lambda: ds.provision_dns_link(db, **dict(args, dns_ips=["dc01"])),
             "not addresses: dc01")
    row, _ = _link(db, onprem)
    _refused(lambda: ds.provision_dns_link(db, **args), "already has a DNS link")
    assert row.cloud == "gcp" and row.provider == "dns_link"
    assert row.linked_directory_id == onprem.id
    assert ds.template_dir(row.cloud, row.provider).endswith(
        os.path.join("directory", "gcp_dns_forward"))
    tf = ds._tf_variables(row)
    assert tf == {"project": "proj-1", "domain_name": onprem.name, "dns_ips": ["10.0.0.10"],
                  "networks": ["projects/proj-1/global/networks/default"],
                  "directory_row_id": row.id}
    db.close()


def test_link_build_needs_no_administrator():
    db = SessionLocal()
    onprem = _onprem(db)
    row, out = _link(db, onprem)
    from web_dashboard.services import terraform

    async def apply(*a, **k):
        return {"zone_name": {"value": "ad-link-abc"}}

    async def no_admin(*a, **k):
        raise AssertionError("a DNS link has no administrator to set")

    saved = terraform.apply, ds.set_admin_password
    terraform.apply, ds.set_admin_password = apply, no_admin
    try:
        asyncio.run(ds.run_provision_apply(db, directory_id=row.id, job_id=out["job_id"]))
    finally:
        terraform.apply, ds.set_admin_password = saved
    db.expire_all()
    row = db.query(ManagedDirectory).filter(ManagedDirectory.id == row.id).one()
    assert row.status == "available" and row.resource_name == "ad-link-abc"
    assert db.query(Job).filter(Job.id == out["job_id"]).one().status == "completed"
    assert row.id in [r.id for r in ds.joinable_for(db, "gcp")]
    db.close()


def _deploy_job(db):
    job = job_service.create_job(db, "gce_deploy", "t", metadata={"instance_name": "win1"})
    job_service.set_completed(db, job.id, {"admin_username": "gcpadmin",
                                           "admin_password_backend": "gcp_sm",
                                           "admin_password_ref": "win-admin-win1",
                                           "private_ip": "10.8.0.5"})
    return job.id


def test_join_is_queued_as_a_winrm_directory_run():
    db = SessionLocal()
    onprem = _onprem(db)
    link, _ = _link(db, onprem)
    deploy = _deploy_job(db)
    jid = djs.queue_agent_join(db, deploy_job_id=deploy, link_row=link, vm_name="win1",
                               private_ip="10.8.0.5", ou="OU=Servers,DC=c,DC=example,DC=com",
                               created_by="t")
    job = db.query(Job).filter(Job.id == jid).one()
    meta = job.metadata_dict
    assert job.job_type == "agent_ansible" and job.agent_id == onprem.agent_id
    assert meta["run_kind"] == "directory" and meta["transport"] == "winrm"
    assert meta["target_host"] == "10.8.0.5" and meta["target_port"] == 5986
    assert meta["asset"] == "builtin:ad-join-computer"
    assert meta["join_deploy_job_id"] == deploy and meta["target_id"] == onprem.id
    assert meta["extra_vars"] == {"ad_join_ou": "OU=Servers,DC=c,DC=example,DC=com"}
    assert aam.check(meta) == ""
    assert set(aam.envelope_payload(meta)) == {"run_kind", "transport", "target_host",
                                               "target_port"}
    assert "Admin" not in json.dumps(meta) and "password" not in json.dumps(meta).lower()
    db.close()


def test_join_refusals():
    db = SessionLocal()
    onprem = _onprem(db)
    link, _ = _link(db, onprem)
    _refused(lambda: djs.queue_agent_join(db, deploy_job_id="d", link_row=link, vm_name="w",
                                          private_ip="", ou="", created_by="t"),
             "no private address")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == onprem.agent_id).one()
    agent.allowed_job_types = json.dumps(["agent_discover"])
    db.commit()
    _refused(lambda: djs.onprem_for_link(db, link), "not granted Config Management")
    assert "join a deployed server" in aam.check(
        {"run_kind": "directory", "transport": "local", "target_host": "h", "target_port": 636,
         "target_id": "x", "join_deploy_job_id": "d"})
    db.close()


def test_join_bundle_logs_on_as_the_server_and_joins_as_the_directory():
    db = SessionLocal()
    onprem = _onprem(db)
    link, _ = _link(db, onprem)
    deploy = _deploy_job(db)
    jid = djs.queue_agent_join(db, deploy_job_id=deploy, link_row=link, vm_name="win1",
                               private_ip="10.8.0.5", ou="", created_by="t")
    job = db.query(Job).filter(Job.id == jid).one()
    from web_dashboard.services import btapi_service, windows_admin_secret

    async def checkout(system_id, account_id, duration_min=60, **kw):
        return "req-1", BIND_PW

    reads = []
    saved = btapi_service.get_ps_credential_with_request, windows_admin_secret.read
    btapi_service.get_ps_credential_with_request = checkout
    windows_admin_secret.read = lambda b, r: reads.append((b, r)) or LOCAL_PW
    try:
        bundle, scrub = asyncio.run(aab.build(db, job=job, agent=None))
    finally:
        btapi_service.get_ps_credential_with_request, windows_admin_secret.read = saved
    assert reads == [("gcp_sm", "win-admin-win1")]
    assert bundle["login_user"] == "gcpadmin" and bundle["login_password"] == LOCAL_PW
    assert bundle["directory"]["dir_bind_dn"] == f"svc-join@{onprem.name}"
    assert bundle["directory"]["dir_bind_password"] == BIND_PW
    assert BIND_PW in scrub and LOCAL_PW in scrub
    assert bundle["winrm"]["scheme"] == "https"
    assert "microsoft.ad.membership" in bundle["playbook"]
    db.close()


def test_first_boot_metadata_opens_winrm_and_holds_no_secret():
    md = djs.agent_join_metadata()
    script = md["sysprep-specialize-script-ps1"]
    assert "5986" in script and "Transport HTTPS" in script
    assert "LocalAccountTokenFilterPolicy" in script
    assert "password" not in script.lower()


def test_builtin_playbook_matches_the_example_and_is_closed():
    example = open(os.path.join(_ROOT, "examples", "playbooks", "directory",
                                "ad-join-computer.yml"), encoding="utf-8").read()
    assert aab.builtin_playbook("builtin:ad-join-computer") == example
    assert aab.builtin_playbook("site.yml") == ""
    try:
        aab.builtin_playbook("builtin:../../etc/passwd")
    except aab.BundleError:
        pass
    else:
        raise AssertionError("an unknown built-in was served")


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
