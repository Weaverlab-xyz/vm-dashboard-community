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
    assert "spire-server-lab" in src and "spire-oidc-lab" in src


def _helm_values():
    return yaml.safe_load(_rendered(
        "spire-helm.yml", "Write the chart values", trust_domain="lab.test",
        oidc_domain="oidc.lab.test", _issuer="https://oidc.lab.test:8443",
        _admin_id="spiffe://lab.test/password-safe/admin"))


def test_the_helm_values_use_the_pinned_charts_key_names():
    """Checked by rendering chart 0.30.2 with these values (`helm template`): a misspelt
    key is silently ignored, so these are pinned by exact path."""
    v = _helm_values()
    g = v["global"]["spire"]
    assert g["trustDomain"] == "lab.test" and g["jwtIssuer"] == "https://oidc.lab.test:8443"
    server = v["spire-server"]
    assert server["adminIDs"] == ["spiffe://lab.test/password-safe/admin"]
    assert server["caTTL"] == "168h"
    assert server["defaultX509SvidTTL"] == "1h" and server["defaultJwtSvidTTL"] == "5m", (
        "the chart spells these defaultX509SvidTTL / defaultJwtSvidTTL")
    assert "defaultJWTSVIDTTL" not in server and "defaultX509SVIDTTL" not in server
    oidc = v["spiffe-oidc-discovery-provider"]["config"]
    assert oidc["additionalDomains"] == ["oidc.lab.test"], "config.domains is not a chart key"
    csid = server["controllerManager"]["identities"]["clusterSPIFFEIDs"]
    assert csid["oidc-discovery-provider"]["dnsNameTemplates"] == ["oidc.lab.test"], (
        "the chart names the provider's certificate oidc-discovery.<td>; the dashboard "
        "verifies oidc.<td>")


def test_the_helm_values_turn_on_upstreams_hardening():
    g = _helm_values()["global"]["spire"]
    assert g["recommendations"]["enabled"] is True, (
        "the chart's security contexts, and the spire-server namespace the CLI prefix "
        "execs into, both hang off recommendations.enabled")
    subj = g["caSubject"]
    assert subj["commonName"] == "lab.test"
    assert subj["country"] != "ARPA" and subj["organization"] != "Example", (
        "strict mode refuses the chart's placeholder CA subject")


def test_the_helm_stage_passes_pinned_chart_versions():
    v = svc._helm_vars(_row(deployment_mode="k8s"))
    for key in ("helm_values_extra", "oidc_domain", "trust_domain"):
        assert key in v
    assert v["oidc_domain"] == "oidc.modes.test"
    assert v["spire_chart_version"] == svc.SPIRE_CHART_VERSION == "0.30.2"
    assert v["spire_crds_chart_version"] == svc.SPIRE_CRDS_CHART_VERSION == "0.6.1"
    play = _play("spire-helm.yml")["vars"]
    assert play["spire_chart_version"] == svc.SPIRE_CHART_VERSION
    assert play["spire_crds_chart_version"] == svc.SPIRE_CRDS_CHART_VERSION
    # 0.30.2 ships SPIRE 1.15.3: the three modes run the same server.
    assert _play("spire-server-install.yml")["vars"]["spire_version"] == "1.15.3"
    assert _play("spire-docker-server.yml")["vars"]["spire_version"] == "1.15.3"


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
    # Pinned, as tests/test_agentcell_cert_episode.py does: PROTOCOL_TLS_SERVER permits
    # TLSv1/1.1 by contract, and the provider this stands in for should not.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
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
                                       spire_version="1.15.3", **_images("1.15.3")))
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


# ── every download is pinned, and an unpinned one is refused ─────────────────
# The tarball checksums were checked against upstream's own *_sha256sum.txt, and the
# image digests read from ghcr.io and checked against the index bodies (multi-arch:
# amd64 + arm64). These tests hold the plays and the service to the same version.

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _set_fact(play_name, task_name, **env):
    """Evaluate a play's set_fact expressions with plain Jinja (they use no Ansible-only
    filters), so the resolution logic itself is under test rather than restated."""
    import jinja2
    play = _play(play_name)
    v = dict(play["vars"])
    v.update(env)
    task = next(t for t in play["tasks"] if t.get("name") == task_name)
    return {k: jinja2.Template(str(x)).render(**v).strip()
            for k, x in task["ansible.builtin.set_fact"].items()}


def _assert_holds(play_name, task_name, **env):
    import jinja2
    e = jinja2.Environment()
    e.filters["bool"] = lambda x: str(x).strip().lower() in ("1", "true", "yes")
    play = _play(play_name)
    v = dict(play["vars"])
    v.update(env)
    task = next(t for t in play["tasks"] if t.get("name") == task_name)
    return all(e.from_string("{{ (" + c + ") }}").render(**v) == "True"
               for c in task["ansible.builtin.assert"]["that"])


def _images(version, **env):
    return _set_fact("spire-docker-server.yml", "Resolve the pinned images",
                     spire_version=version, **env)


def test_the_tarball_pins_agree_across_plays_and_with_the_default_version():
    default = svc._install_vars(_row())["spire_version"]
    spire = [_play(n)["vars"]["spire_checksums"]
             for n in ("spire-server-install.yml", "spire-agent-install.yml")]
    assert spire[0] == spire[1], "the server and agent plays pin different tarballs"
    extras = _play("spire-oidc-provider.yml")["vars"]["spire_extras_checksums"]
    digests = _play("spire-docker-server.yml")["vars"]["spire_image_digests"]
    for table in (spire[0], extras, digests):
        assert default in table, f"SPIRE {default} (the lab's default) has no pin"
    for table in (spire[0], extras):
        for arch in ("amd64", "arm64"):
            assert _HEX64.match(table[default][arch]), (table, arch)
    for role in ("server", "oidc"):
        assert re.match(r"^sha256:[0-9a-f]{64}$", digests[default][role])
    for name in ("spire-server-install.yml", "spire-agent-install.yml",
                 "spire-oidc-provider.yml", "spire-docker-server.yml"):
        assert _play(name)["vars"]["spire_version"] == default, name


def test_the_pinned_checksum_is_the_one_the_download_uses():
    got = _set_fact("spire-server-install.yml",
                    "Resolve the pinned checksum for this version and architecture",
                    spire_version="1.15.3", _arch="arm64")
    assert got["_spire_sha256"] == "a9982b3ca7de489def22265fd4586d8e13091ecb6fddf6adcea9291313b18886"
    over = _set_fact("spire-server-install.yml",
                     "Resolve the pinned checksum for this version and architecture",
                     spire_version="1.15.3", _arch="amd64", spire_sha256="ab" * 32)
    assert over["_spire_sha256"] == "ab" * 32, "an explicit spire_sha256 must win"
    for name in ("spire-server-install.yml", "spire-agent-install.yml"):
        dl = next(t for t in _play(name)["tasks"] if t.get("name") == "Download the SPIRE release")
        assert "_spire_sha256" in dl["ansible.builtin.get_url"]["checksum"], name


def test_an_unpinned_version_is_refused_unless_explicitly_allowed():
    task = "Refuse an unverified download"
    for name, fact in (("spire-server-install.yml", "_spire_sha256"),
                       ("spire-agent-install.yml", "_spire_sha256"),
                       ("spire-oidc-provider.yml", "_extras_sha256")):
        assert not _assert_holds(name, task, **{fact: ""}), f"{name} took an unpinned download"
        assert _assert_holds(name, task, **{fact: "", "spire_allow_unpinned": True})
        assert _assert_holds(name, task, **{fact: "ab" * 32})
    # A host that already has the release downloads nothing, so it is not refused.
    refuse = next(t for t in _play("spire-server-install.yml")["tasks"] if t.get("name") == task)
    assert "not in _installed" in str(refuse["when"])


def test_the_images_run_by_digest_and_an_unpinned_version_is_refused():
    img = _images("1.15.3")
    assert img["_server_image"] == ("ghcr.io/spiffe/spire-server:1.15.3@sha256:"
                                    "4082f30d3e0ddc4000a171392c4ea174345ee44d161ee917c70b97b2ecfba141")
    assert img["_oidc_image"].startswith("ghcr.io/spiffe/oidc-discovery-provider:1.15.3@sha256:")
    task = "Refuse an image that is not pinned by digest"
    assert _assert_holds("spire-docker-server.yml", task, **img)
    loose = _images("9.9.9")
    assert "@" not in loose["_server_image"]
    assert not _assert_holds("spire-docker-server.yml", task, **loose)
    assert _assert_holds("spire-docker-server.yml", task, spire_allow_unpinned=True, **loose)


def test_the_service_passes_the_opt_out_only_when_configured():
    orig = svc._cfg
    try:
        svc._cfg = lambda key, default="": default
        for fn in (svc._install_vars, svc._oidc_vars, svc._agent_vars, svc._docker_server_vars,
                   svc._auth_vars):
            assert fn(_row())["spire_allow_unpinned"] is False, fn.__name__
        svc._cfg = lambda key, default="": "true" if key == "spire_lab_allow_unpinned" else default
        assert svc._docker_server_vars(_row())["spire_allow_unpinned"] is True
        svc._cfg = lambda key, default="": "ab" * 32 if key == "spire_lab_helm_sha256" else default
        assert svc._helm_vars(_row(deployment_mode="k8s"))["helm_sha256"] == "ab" * 32
    finally:
        svc._cfg = orig


def test_helm_is_verified_before_it_is_unpacked():
    tasks = _play("spire-helm.yml")["tasks"]
    names = [t.get("name") for t in tasks]
    assert names.index("Download helm") < names.index("Unpack helm")
    dl = tasks[names.index("Download helm")]["ansible.builtin.get_url"]
    assert "helm_sha256" in dl["checksum"]
    assert "get.helm.sh" not in str(tasks[names.index("Unpack helm")]), (
        "unarchive straight from a URL has nowhere to check a checksum")


# ── the key fetch leaves a private address out of the egress proxy ──────────

def test_a_private_address_bypasses_the_proxy_and_a_name_does_not():
    from web_dashboard.services import spiffe_assertion as sa
    assert sa._bypass_proxy("https://10.1.2.3:8443/keys")
    assert sa._bypass_proxy("https://192.168.0.5:8443/keys")
    assert sa._bypass_proxy("https://127.0.0.1:8443/keys")
    assert sa._bypass_proxy("https://[fd00::1]:8443/keys")
    assert not sa._bypass_proxy("https://20.1.2.3:8443/keys"), "a public address is egress"
    assert not sa._bypass_proxy("https://idp.example.com/keys"), "a name is egress"


def test_the_fetch_ignores_proxy_variables_for_a_private_address():
    from web_dashboard.services import spiffe_assertion as sa
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": []}

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Resp()

    orig = sa.httpx.Client
    sa.httpx.Client = _Client
    try:
        sa._fetch_jwks("https://10.0.0.4:8443/keys")
        assert seen["trust_env"] is False
        sa._fetch_jwks("https://idp.example.com/keys")
        assert seen["trust_env"] is True
    finally:
        sa.httpx.Client = orig


# ── upgrades: a new version replaces the running one ─────────────────────────
# Run here against real release tarballs: the provider tasks took a host from the old
# plain-file layout to 1.15.3, upgraded it to 1.15.2 (install beside, re-point, prune),
# changed nothing on a re-run, and refused the unpinned version without the opt-out.

def test_the_provider_installs_per_version_behind_a_symlink():
    tasks = {t.get("name"): t for t in _play("spire-oidc-provider.yml")["tasks"]}
    detect = tasks["Detect this version's provider binary"]["ansible.builtin.stat"]
    assert detect["path"] == "{{ oidc_dir }}/bin/{{ spire_version }}/oidc-discovery-provider", (
        "guarding on the binary merely existing is what left upgrades as a no-op")
    unpack = tasks["Unpack the provider binary"]["ansible.builtin.unarchive"]
    assert unpack["dest"] == "{{ oidc_dir }}/bin/{{ spire_version }}"
    link = tasks["Point the provider at this version"]
    assert link["ansible.builtin.file"]["state"] == "link"
    assert link["ansible.builtin.file"]["force"] is True, "an old plain file must be replaceable"
    assert link["notify"] == "Restart spire-oidc-provider"
    unit = _rendered("spire-oidc-provider.yml", "Install the systemd unit")
    assert "ExecStart=/opt/spire/oidc/oidc-discovery-provider " in unit, "the unit runs the link"


def test_old_provider_versions_are_pruned_only_after_the_new_one_serves():
    names = [t.get("name") for t in _play("spire-oidc-provider.yml")["tasks"]]
    assert names.index("Confirm the issuer the document advertises") < \
        names.index("Prune superseded provider versions"), (
            "pruning before the new version has served would leave nothing to roll back to")
    find = next(t for t in _play("spire-oidc-provider.yml")["tasks"]
                if t.get("name") == "Find superseded provider versions")["ansible.builtin.find"]
    assert find["excludes"] == ["{{ spire_version }}"] and find["file_type"] == "directory"


def test_a_new_server_or_agent_binary_restarts_the_service():
    for name, handler in (("spire-server-install.yml", "Restart spire-server"),
                          ("spire-agent-install.yml", "Restart spire-agent")):
        tasks = _play(name)["tasks"]
        unpack = next(t for t in tasks if t.get("name") == "Unpack SPIRE")
        assert unpack.get("notify") == handler, (
            f"{name}: a version bump unpacks the new binary and leaves the old process running")
        names = [t.get("name") for t in tasks]
        flush = [i for i, t in enumerate(tasks)
                 if t.get("ansible.builtin.meta") == "flush_handlers"]
        assert flush and flush[0] > names.index("Unpack SPIRE"), (
            f"{name}: the restart must land before the play probes the service")

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
