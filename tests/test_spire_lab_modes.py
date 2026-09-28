"""SPIRE lab deployment modes (vm | docker | k8s), JWKS URL registration and key refresh.

None of the three modes has run against a live host yet, so these pin the parts that
decide whether a build CAN work: which playbooks each mode runs and in what order, that
every shared play reaches the CLI through the one prefix, that the Docker server config
cannot drift from the VM one, that the Helm play verifies what it cannot know, that the
stdout PEM split actually splits, and that registration, refresh scheduling and the
TLS-name override behave.

Run: python tests/test_spire_lab_modes.py   (or under pytest)
"""
import datetime as _dt
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import http.server
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-modes-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-modes")

try:
    import cryptography  # noqa: F401
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.database import (Base, Job, SessionLocal, SpiffeTrustDomain,  # noqa: E402
                                    SpireLab, engine)
from web_dashboard.services import job_service, spire_lab_service as svc  # noqa: E402

Base.metadata.create_all(bind=engine)

_SPIRE = os.path.join(_ROOT, "examples", "playbooks", "spire")
_SHARED = ("spire-seed-entries.yml", "spire-admin-identity.yml", "spire-jwt-bundle.yml",
           "spire-k8s-entry.yml", "spire-oidc-provider.yml")


def _play(name, folder=_SPIRE):
    return yaml.safe_load(open(os.path.join(folder, name), encoding="utf-8").read())[0]


def _src(name, folder=_SPIRE):
    return open(os.path.join(folder, name), encoding="utf-8").read()


def _row(**kw) -> SpireLab:
    base = dict(id=str(uuid.uuid4()), name="lab", trust_domain="modes.test", cloud="azure",
                bind_port=8081, status="available", vm_name="spire-01",
                private_ip="10.0.0.5", public_ip="203.0.113.5",
                admin_secret_folder="spire/lab", trust_bundle_pem="-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----",
                stages_done="install,ports,seed,identity,oidc", created_by="tester")
    base.update(kw)
    return SpireLab(**base)


# ── which playbooks each mode runs ──────────────────────────────────────────

def test_each_mode_runs_its_install_then_the_shared_stages():
    assets = svc.MODE_ASSETS
    assert assets["vm"] == ("spire-server-install.yml", "spire-open-ports.yml",
                            "spire-seed-entries.yml", "spire-admin-identity.yml",
                            "spire-oidc-provider.yml")
    assert assets["docker"] == ("install-docker.yml", "spire-docker-server.yml",
                                "spire-open-ports.yml", "spire-seed-entries.yml",
                                "spire-admin-identity.yml", "spire-oidc-provider.yml")
    # No oidc stage: the chart runs the provider and spiffe-helper rotates its cert.
    assert assets["k8s"] == ("k3s-server-init.yml", "spire-helm.yml", "spire-open-ports.yml",
                             "spire-seed-entries.yml", "spire-admin-identity.yml")
    assert svc.STAGE_ASSETS == assets["vm"]


def test_every_asset_a_mode_needs_exists_in_the_repo():
    where = {"install-docker.yml": "linux", "k3s-server-init.yml": "k3s"}
    for mode, assets in svc.MODE_ASSETS.items():
        for a in assets:
            path = os.path.join(_ROOT, "examples", "playbooks", where.get(a, "spire"), a)
            assert os.path.exists(path), f"{mode}: {a} is not in the repo"


def test_an_unknown_mode_is_refused_and_null_reads_as_vm():
    assert svc.deployment_mode(_row(deployment_mode=None)) == "vm"
    assert svc.deployment_mode(_row(deployment_mode="bogus")) == "vm"
    db = SessionLocal()
    try:
        try:
            svc.provision(db, name="x", trust_domain="x.test", cloud="azure", host="h",
                          created_by="t", deployment_mode="swarm")
            raise AssertionError("an unknown mode was accepted")
        except svc.SpireLabError as exc:
            assert "deployment mode" in str(exc)
    finally:
        db.close()


# ── one CLI prefix for every shared play ─────────────────────────────────────

def test_the_cli_prefix_per_mode():
    assert svc.cli_vars(_row(deployment_mode="vm")) == {
        "spire_cli_prefix": "", "spire_mint_stdout": False}
    assert svc.cli_vars(_row(deployment_mode="docker"))["spire_cli_prefix"] == \
        "docker exec spire-server "
    k8s = svc.cli_vars(_row(deployment_mode="k8s"))
    assert k8s["spire_cli_prefix"].startswith("k3s kubectl exec -n spire-server spire-server-0")
    assert k8s["spire_mint_stdout"] is True


def test_every_shared_play_reaches_the_cli_only_through_the_prefix():
    for name in _SHARED:
        src = _src(name)
        bare = re.findall(r"(?<!\}\})\{\{ spire_root \}\}/bin/spire-server", src)
        assert not bare, f"{name}: a spire-server call bypasses spire_cli_prefix"
        assert "spire_cli_prefix" in _play(name)["vars"], f"{name} does not declare it"


def test_every_stage_that_runs_a_shared_play_passes_the_cli_vars():
    row = _row(deployment_mode="docker")
    for stage in (svc._SEED, svc._IDENTITY, svc.JWT_BUNDLE_STAGE, svc.OIDC_BUILD_STAGE):
        v = stage["vars_for"](row)
        assert v.get("spire_cli_prefix") == "docker exec spire-server ", stage["asset"]
    assert svc._k8s_entry_vars(row)["spire_cli_prefix"] == "docker exec spire-server "
    assert svc._oidc_vars(row)["oidc_runtime"] == "docker"
    assert svc._oidc_vars(_row(deployment_mode="vm"))["oidc_runtime"] == "systemd"


# ── Docker: the server config cannot drift from the VM one ───────────────────

def _server_conf(name):
    for t in _play(name)["tasks"]:
        copy = t.get("ansible.builtin.copy") or {}
        if str(copy.get("dest", "")).endswith("server.conf"):
            return copy["content"]
    raise AssertionError(f"{name} writes no server.conf")


def test_the_docker_server_config_matches_the_vm_install():
    vm, docker = _server_conf("spire-server-install.yml"), _server_conf("spire-docker-server.yml")
    norm = lambda c: [ln.strip() for ln in c.splitlines() if ln.strip()]  # noqa: E731
    assert norm(vm) == norm(docker), (
        "the Docker server.conf drifted from the VM install's. Everything after the install "
        "(admin_ids for the plugin, ca_ttl, the datastore) assumes they are the same.")


def test_the_docker_play_refuses_a_host_running_the_vm_server():
    names = [t["name"] for t in _play("spire-docker-server.yml")["tasks"]]
    assert "Refuse to run beside the VM-mode server" in names
    assert "profiles" in _src("spire-docker-server.yml"), (
        "the provider service must wait for its certificate (compose profile)")


# ── k8s: the Helm play verifies what it cannot know ──────────────────────────

def test_the_helm_play_reads_back_admin_ids_and_fetches_the_discovery_document():
    src = _src("spire-helm.yml")
    names = [t["name"] for t in _play("spire-helm.yml")["tasks"]]
    assert "Confirm admin_ids reached the server configuration" in names
    assert "Fetch the discovery document through the published port" in names
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "--cacert" in code and " -k " not in code, "the check must not skip TLS verification"
    for key in ("trustDomain", "jwtIssuer", "adminIDs", "domains", "helm_values_extra"):
        assert key in src, f"the Helm values no longer set {key}"
    assert "spire-server-lab" in src and "spire-oidc-lab" in src


def test_the_helm_stage_passes_pinnable_chart_versions():
    v = svc._helm_vars(_row(deployment_mode="k8s"))
    for key in ("spire_chart_version", "spire_crds_chart_version", "helm_values_extra",
                "oidc_domain", "trust_domain"):
        assert key in v
    assert v["oidc_domain"] == "oidc.modes.test"


# ── the stdout PEM split, run for real ───────────────────────────────────────

def _pem(label, body="QUJD"):
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"


def _split(stdout):
    code = _play("spire-admin-identity.yml")["vars"]["_pem_split"]
    d = tempfile.mkdtemp(prefix="pem-split-")
    r = subprocess.run([sys.executable, "-c", code, d], input=stdout, text=True,
                       capture_output=True)
    return r, d


def test_the_pem_split_goes_by_position_whatever_the_headers():
    out = "\n".join(["X509-SVID:", _pem("CERTIFICATE", "SVID"), "Private key:",
                     _pem("PRIVATE KEY", "KEY"), "Root CAs:", _pem("CERTIFICATE", "ROOT1"),
                     _pem("CERTIFICATE", "ROOT2")])
    r, d = _split(out)
    assert r.returncode == 0, r.stderr
    assert "SVID" in open(os.path.join(d, "svid.pem"), encoding="utf-8").read()
    assert "KEY" in open(os.path.join(d, "key.pem"), encoding="utf-8").read()
    bundle = open(os.path.join(d, "bundle.pem"), encoding="utf-8").read()
    assert "ROOT1" in bundle and "ROOT2" in bundle
    assert oct(os.stat(os.path.join(d, "key.pem")).st_mode & 0o777) == "0o600"


def test_the_pem_split_refuses_output_it_does_not_understand():
    r, _ = _split(_pem("CERTIFICATE"))                      # no key at all
    assert r.returncode != 0
    r, _ = _split("\n".join([_pem("PRIVATE KEY"), _pem("CERTIFICATE")]))   # key first
    assert r.returncode != 0


def test_both_mint_plays_carry_the_same_split():
    a = _play("spire-admin-identity.yml")["vars"]["_pem_split"]
    b = _play("spire-oidc-provider.yml")["vars"]["_pem_split"]
    assert a == b


# ── the host firewall and teardown ───────────────────────────────────────────

def test_the_host_firewall_stage_opens_8443_too():
    assert svc._ports_vars(_row())["extra_ports"] == [svc.OIDC_PORT]
    assert "product(_ports)" in _src("spire-open-ports.yml")


def test_the_remove_play_covers_every_mode_and_deletes_the_ca_key():
    src = _src("spire-remove.yml")
    for needle in ("deployment_mode == 'vm'", "deployment_mode == 'docker'",
                   "deployment_mode == 'k8s'", "helm uninstall", "down -v"):
        assert needle in src, needle
    assert svc._remove_vars(_row(deployment_mode="k8s")) == {"deployment_mode": "k8s"}


def test_the_k3s_link_skips_its_oidc_stage_when_the_build_published_one():
    assert svc.oidc_published(_row(deployment_mode="k8s", stages_done=""))
    assert svc.oidc_published(_row(deployment_mode="vm"))
    assert not svc.oidc_published(_row(deployment_mode="vm", stages_done="install,ports"))


# ── JWKS URL registration ────────────────────────────────────────────────────

def _save(row):
    db = SessionLocal()
    db.add(row)
    db.commit()
    db.refresh(row)
    return db, row


def test_registration_points_at_the_labs_provider_by_address_and_name():
    db, row = _save(_row(trust_domain="reg.test", deployment_mode="docker"))
    try:
        out = svc.register_trust_domain(db, row)
        assert out["jwks_url"] == "https://203.0.113.5:8443/keys"
        rec = db.query(SpiffeTrustDomain).filter_by(trust_domain="reg.test").one()
        assert rec.tls_server_name == "oidc.reg.test"
        assert rec.ca_pem == row.trust_bundle_pem and rec.spire_lab_id == row.id
        # Another lab with the same trust domain does not steal it...
        _, other = _save(_row(trust_domain="reg.test"))
        assert svc.register_trust_domain(db, other) == {}
        # ...and cannot remove it on teardown; only the owner can.
        assert svc.unregister_trust_domain(db, other) is False
        assert svc.unregister_trust_domain(db, row) is True
        assert db.query(SpiffeTrustDomain).filter_by(trust_domain="reg.test").first() is None
    finally:
        db.close()


def test_a_lab_without_a_provider_or_a_bundle_is_not_registered():
    db, row = _save(_row(trust_domain="noreg.test", stages_done="install,ports"))
    try:
        assert svc.register_trust_domain(db, row) == {}
        row.stages_done = "install,ports,seed,identity,oidc"
        row.trust_bundle_pem = ""
        db.commit()
        assert svc.register_trust_domain(db, row) == {}
    finally:
        db.close()


# ── scheduled refresh ────────────────────────────────────────────────────────

def test_durations_parse():
    assert svc._duration_seconds("168h") == 168 * 3600
    assert svc._duration_seconds("7d") == 7 * 86400
    assert svc._duration_seconds("90m") == 5400
    assert svc._duration_seconds("junk") == 168 * 3600


def test_refresh_is_due_after_a_third_of_ca_ttl_and_never_twice_at_once():
    db, row = _save(_row(trust_domain="due.test"))
    try:
        svc.register_trust_domain(db, row)
        now = _dt.datetime.utcnow()
        assert not svc.refresh_due(db, row, now), "just registered, nothing is due"
        later = now + _dt.timedelta(hours=57)          # > 168h / 3
        assert svc.refresh_due(db, row, later)

        def mine(ids):
            # The test DB is shared: other files' labs may be due too.
            return [j for j in ids
                    if db.query(Job).filter(Job.id == j).one().metadata_dict.get("lab_id") == row.id]

        first = mine(svc.enqueue_refresh_if_due(db, later))
        assert len(first) == 1
        # Active, and recent: a second tick enqueues nothing for this lab.
        assert mine(svc.enqueue_refresh_if_due(db, later)) == []
        job = db.query(Job).filter(Job.id == first[0]).one()
        assert job.job_type == svc.JWT_BUNDLE_JOB_TYPE
        # Finished but recent still blocks (the gunicorn-twin case).
        job.status = "completed"
        job.created_at = later
        db.commit()
        assert mine(svc.enqueue_refresh_if_due(db, later)) == []
        # Finished and old: due again.
        job.created_at = later - _dt.timedelta(hours=2)
        db.commit()
        assert len(mine(svc.enqueue_refresh_if_due(db, later))) == 1
    finally:
        db.close()


def test_a_lab_that_publishes_no_provider_is_never_refreshed():
    db, row = _save(_row(trust_domain="old.test", stages_done="install,ports,seed,identity"))
    try:
        assert not svc.refresh_due(db, row, _dt.datetime.utcnow() + _dt.timedelta(days=30))
    finally:
        db.close()


# ── the TLS-name override, against a real TLS server ─────────────────────────

def test_the_jwks_fetch_verifies_the_lab_name_while_connecting_by_address():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from web_dashboard.services import spiffe_assertion as sa

    d = tempfile.mkdtemp(prefix="sni-")
    now = _dt.datetime.utcnow()
    cak = ec.generate_private_key(ec.SECP256R1())
    can = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lab CA")])
    ca = (x509.CertificateBuilder().subject_name(can).issuer_name(can)
          .public_key(cak.public_key()).serial_number(1).not_valid_before(now)
          .not_valid_after(now + _dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(cak, hashes.SHA256()))
    lk = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([])).issuer_name(can)
            .public_key(lk.public_key()).serial_number(2).not_valid_before(now)
            .not_valid_after(now + _dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("oidc.lab.test")]),
                           critical=True)
            .sign(cak, hashes.SHA256()))
    open(os.path.join(d, "leaf.pem"), "wb").write(leaf.public_bytes(serialization.Encoding.PEM))
    open(os.path.join(d, "key.pem"), "wb").write(lk.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("Host")
            body = json.dumps({"keys": [{"kid": "k", "use": "sig"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(d, "leaf.pem"), os.path.join(d, "key.pem"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    try:
        port = srv.server_address[1]
        url = f"https://127.0.0.1:{port}/keys"
        assert sa._fetch_jwks(url, ca_pem, "oidc.lab.test")["keys"][0]["kid"] == "k"
        assert seen["host"] == f"oidc.lab.test:{port}", "the provider checks Host"
        for args in ((url, ca_pem, ""), (url, ca_pem, "evil.test"), (url, "", "oidc.lab.test")):
            try:
                sa._fetch_jwks(*args)
                raise AssertionError(f"accepted {args[1:]!r}")
            except AssertionError:
                raise
            except Exception:  # noqa: BLE001 -- refused, as it must be
                pass
    finally:
        srv.shutdown()


def test_a_failing_url_falls_back_to_a_stored_bundle():
    from web_dashboard.services import spiffe_assertion as sa
    bundle = json.dumps({"keys": [{"kty": "EC", "use": "jwt-svid", "kid": "b1"}]})
    rec = SpiffeTrustDomain(trust_domain="fb.test", jwks_url="https://127.0.0.1:1/keys",
                            bundle_json=bundle)
    orig = sa._fetch_jwks
    sa._fetch_jwks = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    try:
        sa.clear_state()
        assert [k["kid"] for k in sa.keys_for(rec)] == ["b1"]
        rec.bundle_json = None
        try:
            sa.keys_for(rec)
            raise AssertionError("no bundle to fall back to, yet keys came back")
        except sa.AssertionError_:
            pass
    finally:
        sa._fetch_jwks = orig
        sa.clear_state()



# ── least privilege: nothing runs as root ────────────────────────────────────
# The Docker mode was run end to end against images built from the 1.15.3 release
# binaries the way upstream's Dockerfile builds them (uid 1000, scratch): server, seed,
# the OIDC provider as 1001 with group 1000 reaching the 0770 socket, and a JWT-SVID
# verified by the dashboard through the JWKS URL. These pin what made that work.

def _rendered(play_name, task_name, **extra):
    import jinja2
    play = _play(play_name)
    v = dict(play["vars"])
    v.update(extra)
    for _ in range(3):
        v = {k: (jinja2.Template(x).render(**v) if isinstance(x, str) else x) for k, x in v.items()}
    task = next(t for t in play["tasks"] if t.get("name") == task_name)
    return jinja2.Template(task["ansible.builtin.copy"]["content"]).render(**v)


def test_the_containers_run_unprivileged_and_locked_down():
    compose = yaml.safe_load(_rendered("spire-docker-server.yml", "Write the compose file",
                                       spire_version="1.15.3"))
    server = compose["services"]["spire-server"]
    oidc = compose["services"]["oidc-discovery-provider"]
    assert server["user"] == "1000:1000", "the server must run as the image's own non-root user"
    assert oidc["user"] == "1001:1001", "the provider gets its own uid"
    assert oidc["group_add"] == ["1000"], "the provider reaches the 0770 socket through the group"
    for name, svc_ in (("spire-server", server), ("oidc-discovery-provider", oidc)):
        assert svc_["read_only"] is True, f"{name}: root filesystem must be read-only"
        assert svc_["cap_drop"] == ["ALL"], f"{name}: every capability must be dropped"
        assert "no-new-privileges:true" in svc_["security_opt"], name
        assert not str(svc_["user"]).startswith("0"), f"{name} runs as root"
    assert any(v.endswith(":/tmp/spire-server/private") for v in server["volumes"]), (
        "the socket must be a host directory the play owns, not a named volume")
    assert oidc["depends_on"]["spire-server"]["condition"] == "service_healthy"


def test_the_docker_play_owns_the_files_to_match_the_container_uids():
    play = _play("spire-docker-server.yml")
    dirs = next(t for t in play["tasks"] if t.get("name") == "Create the SPIRE directories")
    by_path = {i["path"]: i for i in dirs["loop"]}
    assert by_path["{{ spire_data }}"]["mode"] == "0700"
    assert by_path["{{ spire_data }}"]["owner"] == "{{ spire_uid }}"
    assert by_path["{{ socket_dir }}"]["owner"] == "{{ spire_uid }}"
    assert by_path["{{ spire_root }}/oidc"]["group"] == "{{ oidc_gid }}"


def test_the_systemd_units_run_as_service_users_in_a_sandbox():
    server = _rendered("spire-server-install.yml", "Install the systemd unit")
    oidc = _rendered("spire-oidc-provider.yml", "Install the systemd unit")
    for name, unit in (("spire-server", server), ("spire-oidc-provider", oidc)):
        for line in ("NoNewPrivileges=yes", "CapabilityBoundingSet=\n", "ProtectSystem=strict",
                     "ProtectHome=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                     "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM"):
            assert line in unit + "\n", f"{name} lacks {line!r}"
    assert "User=spire\n" in server and "ReadWritePaths=/opt/spire/data /tmp/spire-server" in server
    assert "User=spire-oidc" in oidc and "SupplementaryGroups=spire" in oidc
    assert "PrivateTmp" not in oidc.replace("# PrivateTmp", ""), (
        "a private /tmp would hide the server's API socket from the provider")
    assert "ReadWritePaths" not in oidc, "the provider writes nothing"


def test_the_socket_directory_is_declared_for_the_server_user():
    tmpfiles = _rendered("spire-server-install.yml", "Declare the socket directory")
    assert "d /tmp/spire-server         0750 spire spire -" in tmpfiles
    assert "d /tmp/spire-server/private 0750 spire spire -" in tmpfiles


def test_the_provider_key_is_readable_by_the_provider_group_only():
    play = _play("spire-oidc-provider.yml")
    t = next(t for t in play["tasks"] if t.get("name") == "Secure the minted key")
    f = t["ansible.builtin.file"]
    assert (f["owner"], f["group"], f["mode"]) == ("root", "{{ _oidc_group }}", "0640")


def test_the_loopback_checks_bypass_any_egress_proxy():
    for name in ("spire-oidc-provider.yml", "spire-helm.yml"):
        assert "curl -sS --noproxy '*' --cacert" in _src(name), (
            f"{name}: an HTTPS_PROXY on the host would intercept the loopback check")

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
