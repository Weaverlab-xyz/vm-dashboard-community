"""Invariants for remote-agent job metadata — what keeps code out of the customer LAN.

``ansible_run_meta`` guards a boundary into the *database*, so its rule is "refs, never
values". This module guards a boundary into a *network the dashboard does not own*, so
its rule is stricter: no field may carry anything the agent could execute. A discovery
payload is scalars, enums and network addresses — no command, no script, no URL, no
filename. That is what makes a compromised dashboard unable to do more than ask an
agent to scan a range it was already allowed to scan.

The assertions pin the three things that would break it:

  * the closed allowlist, so a field added later trips this test instead of quietly
    becoming an execution channel;
  * the round-trip, so a payload written by the endpoint is read back under the same
    name (a dropped `max_hosts` would silently restore the default and widen a sweep);
  * the clamps, because they are the only thing standing between an operator typo — or
    a hostile dashboard — and a /8 sweep of someone's production network.

Pure module, stdlib only. Runs under pytest, or standalone:
    python tests/test_agent_job_meta.py
"""
import importlib.util
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "agent_job_meta.py")
_spec = importlib.util.spec_from_file_location("agent_job_meta", _PATH)
ajm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ajm)


def _payload(**over):
    """A request-shaped object. discover_meta reads with getattr, so a namespace
    behaves exactly like the pydantic model."""
    base = dict(scan_kind="all", cidrs=["10.20.0.0/24"], hostnames=["pve01.lab.local"],
                # Every family, because normalize() fills in the ones you omit — a
                # partial dict here would fail the round-trip for the right reason.
                ports={"vmware": [443], "proxmox": [8006], "nutanix": [9440],
                       "xcpng": [443], "winrm": [5985]},
                timeout_s=3, max_hosts=256, concurrency=16)
    base.update(over)
    return types.SimpleNamespace(**base)


# ── the allowlist ─────────────────────────────────────────────────────────────

def test_metadata_keys_are_a_closed_set():
    """A new field must fail here and be considered, not ride along silently."""
    meta = ajm.discover_meta(_payload(), description="d")
    assert set(meta) == set(ajm.DISCOVER_META_KEYS) | {"description"}


def test_no_field_can_carry_executable_content():
    """The property this module exists for. A discovery payload names *where* to look,
    never *what to run*, so any key shaped like a command, a script, a fetchable URL or
    a path is banned outright — there is no reference-suffix exemption here, unlike
    ansible_run_meta, because there is no legitimate use for one."""
    banned = re.compile(
        r"(command|cmd|script|shell|exec|playbook|manifest|url|uri|path|file|"
        r"image|entrypoint|args|env)", re.I)
    offenders = [k for k in ajm.DISCOVER_META_KEYS if banned.search(k)]
    assert not offenders, f"execution-shaped key in the discovery allowlist: {offenders}"


def test_no_credential_shaped_key_is_persisted():
    """Discovery never authenticates, so it never needs a credential — and the job row
    is written to the database, where one must never land."""
    banned = re.compile(r"(password|passwd|token|secret|credential|private_key)", re.I)
    offenders = [k for k in ajm.DISCOVER_META_KEYS if banned.search(k)]
    assert not offenders, f"credential-shaped key in the discovery allowlist: {offenders}"


def test_the_execution_guard_actually_rejects_things():
    """Guard the guard: a regex that matches nothing would make the test above pass
    forever."""
    banned = re.compile(
        r"(command|cmd|script|shell|exec|playbook|manifest|url|uri|path|file|"
        r"image|entrypoint|args|env)", re.I)
    for bad in ("command", "playbook_b64", "download_url", "script_path",
                "manifest", "container_image", "extra_args"):
        assert banned.search(bad), f"{bad!r} should be rejected by the execution guard"
    for ok in ("scan_kind", "cidrs", "hostnames", "ports", "timeout_s", "max_hosts"):
        assert not banned.search(ok), f"{ok!r} is a legitimate discovery field"


# ── the round-trip ────────────────────────────────────────────────────────────

def test_round_trip_preserves_every_field():
    p = _payload()
    kwargs = ajm.discover_kwargs(ajm.discover_meta(p, description="d"))
    for key in ajm.DISCOVER_META_KEYS:
        assert kwargs[key] == getattr(p, key), f"{key} did not survive the round-trip"


def test_missing_keys_fall_back_to_defaults():
    """A job queued by an older build predates some fields; resuming it the way that
    build would have is better than refusing to run it."""
    kwargs = ajm.discover_kwargs({"scan_kind": "proxmox"})
    assert kwargs["scan_kind"] == "proxmox"
    assert kwargs["max_hosts"] == ajm._DEFAULTS["max_hosts"]
    assert kwargs["ports"] == ajm._DEFAULTS["ports"]


def test_defaults_are_never_handed_out_by_reference():
    """A caller that mutated a returned default would poison every later
    reconstruction in the same process."""
    first = ajm.discover_kwargs({})
    first["cidrs"].append("10.0.0.0/8")
    first["ports"]["vmware"].append(9999)
    second = ajm.discover_kwargs({})
    assert second["cidrs"] == []
    assert 9999 not in second["ports"]["vmware"]


# ── the clamps ────────────────────────────────────────────────────────────────

def test_host_and_concurrency_caps_are_enforced():
    meta = ajm.normalize({"max_hosts": 10 ** 6, "concurrency": 10 ** 4, "timeout_s": 9999})
    assert meta["max_hosts"] == ajm.MAX_HOSTS_CEILING
    assert meta["concurrency"] == ajm.MAX_CONCURRENCY
    assert meta["timeout_s"] == ajm.MAX_TIMEOUT_S


def test_lower_bounds_are_enforced():
    """Zero or negative would mean "no limit" to a naive range()/semaphore."""
    meta = ajm.normalize({"max_hosts": 0, "concurrency": -5, "timeout_s": 0})
    assert meta["max_hosts"] >= 1
    assert meta["concurrency"] >= 1
    assert meta["timeout_s"] >= 1


def test_garbage_falls_back_instead_of_raising():
    """Normalize runs on a payload that already reached the database; raising here
    would strand the job rather than run a safe subset of it."""
    meta = ajm.normalize({"max_hosts": "lots", "concurrency": None,
                          "timeout_s": [], "scan_kind": "everything",
                          "cidrs": "10.0.0.0/8", "ports": "all"})
    assert meta["max_hosts"] == ajm._DEFAULTS["max_hosts"]
    assert meta["concurrency"] == ajm._DEFAULTS["concurrency"]
    assert meta["scan_kind"] == "all"
    assert meta["cidrs"] == []          # a bare string is not a list of CIDRs
    assert meta["ports"] == ajm._DEFAULTS["ports"]


def test_invalid_ports_are_dropped_and_empty_falls_back():
    meta = ajm.normalize({"ports": {"vmware": [443, 70000, "x", -1], "proxmox": []}})
    assert meta["ports"]["vmware"] == [443]
    assert meta["ports"]["proxmox"] == ajm._DEFAULTS["ports"]["proxmox"]


def test_scan_kind_is_an_enum():
    for kind in ajm.VALID_SCAN_KINDS:
        assert ajm.normalize({"scan_kind": kind})["scan_kind"] == kind
    assert ajm.normalize({"scan_kind": "  PROXMOX  "})["scan_kind"] == "proxmox"


def test_normalize_is_applied_at_enqueue():
    """The stored row must already be valid — the agent normalizes again on arrival,
    but a row that reached the database unclamped is a row someone can read and
    believe."""
    meta = ajm.discover_meta(_payload(max_hosts=10 ** 6), description="d")
    assert meta["max_hosts"] == ajm.MAX_HOSTS_CEILING


# ── the result, on its way back out ───────────────────────────────────────────
# The trip in is guarded so a compromised dashboard cannot run code on the LAN. The trip
# out is guarded because a finding is what an *unidentified host* said about itself, and
# it is about to be rendered in an operator's browser.

def _finished(**over):
    """A job row's metadata after set_completed merged the agent's result into the scan
    request — which is what discover_findings actually reads."""
    meta = ajm.discover_meta(_payload(), description="d")
    meta.update({
        "findings": [{"kind": "hypervisor", "product": "proxmox", "host": "10.20.0.5",
                      "port": 8006, "endpoint": "https://10.20.0.5:8006",
                      "server_version": "", "confidence": "confirmed",
                      "tls_cn": "pve1", "suggested_name": "pve-10-20-0-5",
                      "already_registered": False}],
        "scanned": 384, "hosts": 128, "refused": 12, "truncated": False,
        "audit_mode": False,
    })
    meta.update(over)
    return meta


def test_findings_projection_keeps_the_reportable_fields():
    out = ajm.discover_findings(_finished())
    assert out["scanned"] == 384 and out["hosts"] == 128 and out["refused"] == 12
    finding = out["findings"][0]
    assert finding["endpoint"] == "https://10.20.0.5:8006"
    assert finding["product"] == "proxmox" and finding["port"] == 8006
    assert finding["already_registered"] is False


def test_findings_projection_never_leaks_the_rest_of_the_job_row():
    """The reason this is a projection and not `return meta`. A job row's metadata holds
    Terraform variables, resolved config and log-stream ids for other job types, and the
    endpoint serving this is keyed on nothing but a job id."""
    meta = _finished()
    meta.update({"tf_variables": {"secret": "s3cret"}, "description": "d",
                 "ecs_log_stream": "stream/abc"})
    out = ajm.discover_findings(meta)
    assert set(out) == {"findings"} | set(ajm.COUNTER_KEYS)


def test_a_finding_cannot_smuggle_extra_keys():
    """A field the agent invented — or one an attacker who owns the probed host talked it
    into reporting — must not reach the browser just because it was in the dict."""
    meta = _finished(findings=[{"kind": "hypervisor", "host": "10.0.0.1", "port": 8006,
                                "kubeconfig": "apiVersion: v1\nusers:\n- token: abc",
                                "command": "rm -rf /"}])
    finding = ajm.discover_findings(meta)["findings"][0]
    assert set(finding) <= set(ajm.FINDING_KEYS)
    assert "kubeconfig" not in finding and "command" not in finding


def test_target_controlled_strings_are_sanitised():
    """server_version is a pre-auth banner and tls_cn is a certificate CN: both are
    whatever an unknown host chose to send. ANSI escapes would otherwise be replayed into
    the operator's browser, and into their terminal the moment they copy a name."""
    meta = _finished(findings=[{
        "kind": "hypervisor", "product": "xcpng", "host": "10.0.0.9", "port": 443,
        "server_version": "8.4.0\x1b[31m\x07evil\x00",
        "suggested_name": "a" * (ajm.MAX_FINDING_CHARS + 50),
    }])
    finding = ajm.discover_findings(meta)["findings"][0]
    assert finding["server_version"] == "8.4.0[31mevil"
    assert len(finding["suggested_name"]) == ajm.MAX_FINDING_CHARS


def test_counters_survive_a_result_with_no_findings():
    """The whole point of returning the counters. "Probed 0 of 384, 384 refused by policy"
    is the only thing that tells an operator why a green scan found nothing — without it a
    refused sweep and a clean network are the same empty table."""
    out = ajm.discover_findings(_finished(findings=[], scanned=0, refused=384))
    assert out["findings"] == []
    assert out["scanned"] == 0 and out["refused"] == 384


def test_a_missing_or_junk_result_degrades_to_empty():
    """A queued job has no result at all, and a failed one may have a partial row."""
    for meta in ({}, None, {"findings": "lots"}, {"findings": [None, 7, "x"]},
                 {"scanned": "many", "refused": None}):
        out = ajm.discover_findings(meta)
        assert out["findings"] == []
        assert all(isinstance(out[k], (int, bool)) for k in ajm.COUNTER_KEYS)


def test_findings_are_capped():
    meta = _finished(findings=[{"kind": "hypervisor", "host": f"10.0.0.{i % 250}", "port": 443}
                               for i in range(ajm.MAX_FINDINGS + 25)])
    assert len(ajm.discover_findings(meta)["findings"]) == ajm.MAX_FINDINGS


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
