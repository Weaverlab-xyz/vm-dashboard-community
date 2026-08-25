"""Regression tests for the Entitle External-Access SA-token readback (k8s).

The bug (job 0818974d, 2026-08-25): ``register_cluster_in_entitle`` applied the
RBAC manifest, then polled the token Secret through a separate runner call and
base64-decoded the runner's RAW combined output. On a GCP Cloud Run runner that
output can be a platform log line instead of the jsonpath value — Cloud Logging
ingests the varlog/system entry "Container called exit(0)." faster than container
stdout — and lenient b64decode (non-alphabet chars silently discarded) turns that
line into bytes ``0a 89 ed …``, so the job died with::

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0x89 in position 1

Fix shape, asserted here:

* the register path mints via the sentinel-wrapped one-shot command (the pattern
  ``_mint_pra_sa_token`` proved live) and never b64decodes raw runner output;
* the shared builder renders the PRA command byte-identically (refactor guard for
  a live-proven flow);
* marker extraction survives output polluted with the exact platform line;
* the GCP k8s-runner log fetch is restricted to container stdout/stderr so
  platform entries can neither satisfy the ingestion wait nor pollute the text.

Pure source inspection + exec of extracted defs — no app imports. Runs under
pytest, or standalone: python tests/test_k8s_entitle_external_token.py
"""
import ast
import os
import shlex

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_K8S_PATH = os.path.join(_ROOT, "web_dashboard", "services", "k8s_service.py")
_GCP_PATH = os.path.join(_ROOT, "web_dashboard", "services", "gcp_service.py")

with open(_K8S_PATH, encoding="utf-8") as fh:
    _K8S_SRC = fh.read()
with open(_GCP_PATH, encoding="utf-8") as fh:
    _GCP_SRC = fh.read()


def _fn_code(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name}() not found")


def _exec_fn(src: str, name: str, glb: dict):
    """Execute one extracted def and return the callable — no app imports."""
    code = _fn_code(src, name)
    ns = dict(glb)
    exec(compile(code, f"<extracted {name}>", "exec"), ns)  # noqa: S102 - test-only
    return ns[name]


# ── the register path ───────────────────────────────────────────────────────────

def test_register_no_longer_decodes_raw_runner_output():
    code = _fn_code(_K8S_SRC, "register_cluster_in_entitle")
    assert "base64.b64decode" not in code, \
        "register_cluster_in_entitle b64decodes runner output again — on a cloud " \
        "runner that output can be a platform log line, not the jsonpath value " \
        "(UnicodeDecodeError 0x89, job 0818974d)"
    assert "_mint_entitle_sa_token" in code, \
        "external mode must mint through the sentinel-wrapped one-shot command"


def test_entitle_mint_flags_the_short_lived_tokenrequest_fallback():
    code = _fn_code(_K8S_SRC, "_mint_entitle_sa_token")
    assert "entitle-token-source-tokenrequest" in code, \
        "the mint must be able to tell a TokenRequest fallback apart from the " \
        "long-lived Secret token — Entitle stores this credential indefinitely, " \
        "and a cluster-capped TokenRequest token dies within hours-to-days"
    assert "logger.warning" in code, \
        "getting the short-lived fallback must at least warn"


# ── the shared one-shot builder ─────────────────────────────────────────────────

def _old_pra_command(ns: str, sa: str, secret: str) -> str:
    """The literal _mint_pra_sa_token built before the refactor (live-proven)."""
    q_ns, q_sa, q_sec = shlex.quote(ns), shlex.quote(sa), shlex.quote(secret)
    return (
        "kubectl apply -f - 1>&2 && { tok=''; "
        "for i in $(seq 1 10); do "
        f"v=$(kubectl -n {q_ns} get secret {q_sec} -o jsonpath='{{.data.token}}' 2>/dev/null || true); "
        "if [ -n \"$v\" ]; then tok=$(printf '%s' \"$v\" | base64 -d 2>/dev/null); break; fi; sleep 2; done; "
        f"if [ -z \"$tok\" ]; then tok=$(kubectl -n {q_ns} create token {q_sa} --duration=24h 2>/dev/null || true); fi; "
        "if [ -z \"$tok\" ]; then echo pra-token-unavailable 1>&2; exit 3; fi; "
        "printf 'BTKN<%s>BTKN\\n' \"$tok\"; }"
    )


def test_builder_renders_the_pra_command_byte_identically():
    build = _exec_fn(_K8S_SRC, "_sa_token_oneshot_command", {"shlex": shlex})
    got = build("pra-access", "pra-access", "pra-access-token",
                marker="BTKN", fallback_duration="24h",
                unavailable="pra-token-unavailable")
    assert got == _old_pra_command("pra-access", "pra-access", "pra-access-token"), \
        "the shared builder changed the PRA one-shot command — that flow is " \
        "live-proven (GKE k8s_tunnel vault-inject); keep it byte-identical"


def test_builder_emits_the_fallback_notice_only_when_asked():
    build = _exec_fn(_K8S_SRC, "_sa_token_oneshot_command", {"shlex": shlex})
    ent = build("entitle", "entitle-access", "entitle-access-token",
                marker="ETKN", fallback_duration="8760h",
                unavailable="entitle-token-unavailable",
                fallback_notice="entitle-token-source-tokenrequest")
    assert "echo entitle-token-source-tokenrequest 1>&2; tok=$(kubectl" in ent, \
        "the notice must be printed before the TokenRequest attempt (stderr)"
    assert "--duration=8760h" in ent
    assert "ETKN<%s>ETKN" in ent
    pra = build("pra-access", "pra-access", "pra-access-token",
                marker="BTKN", fallback_duration="24h",
                unavailable="pra-token-unavailable")
    assert "entitle-token-source-tokenrequest" not in pra


# ── marker extraction vs polluted output ────────────────────────────────────────

def test_marker_extraction_survives_the_platform_line_that_broke_the_job():
    extract = _exec_fn(_K8S_SRC, "_extract_marker_token", {})
    jwt = "eyJhbGciOiJSUzI1NiIs.FAKE.JWT"
    polluted = (
        "namespace/entitle created\n"
        "serviceaccount/entitle-access created\n"
        f"ETKN<{jwt}>ETKN\n"
        "Container called exit(0)."
    )
    assert extract(polluted, "ETKN") == jwt
    # No marker → "" (callers raise a clear error), never the raw output.
    assert extract("Container called exit(0).", "ETKN") == ""
    assert extract("", "ETKN") == ""
    assert extract(None, "ETKN") == ""
    # Two runs' logs in one capture → the NEWEST (last) token wins.
    two = f"BTKN<old.token.value>BTKN\nBTKN<{jwt}>BTKN"
    assert extract(two, "BTKN") == jwt


# ── the GCP log fetch ───────────────────────────────────────────────────────────

def test_gcp_k8s_runner_log_fetch_is_restricted_to_container_output():
    fetch = _fn_code(_GCP_SRC, "_fetch_cloud_run_job_logs")
    assert "container_only" in fetch and "run.googleapis.com%2Fstdout" in fetch \
           and "run.googleapis.com%2Fstderr" in fetch, \
        "_fetch_cloud_run_job_logs must be able to scope to the container's own " \
        "stdout/stderr logNames — audit entries (no textPayload) and varlog/system " \
        "lines both match resource.type=cloud_run_job and ingest FASTER than stdout"
    k8s_sync = _fn_code(_GCP_SRC, "_run_cloud_run_k8s_sync")
    assert "container_only=True" in k8s_sync, \
        "the k8s runner parses its output as a VALUE (minted SA tokens) — it must " \
        "not see platform log lines, and the ingestion wait must hold out for real " \
        "container output"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("all passed")
