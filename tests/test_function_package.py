"""Cloud Functions: the deterministic packager.

Byte-stability is the load-bearing property. If a timestamp or a walk order leaks
into the zip, the content hash changes on every apply and every function redeploys
forever — 60-120 s of Cloud Build each time on GCP.

Runs with nothing installed; the Azure vendoring assertions skip cleanly when
azure-functions is absent (it is in requirements.txt, so CI exercises them).
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import cloud_function_package as pkg


def _names(blob: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return sorted(archive.namelist())


def _azure_available() -> bool:
    try:
        import azure.functions  # noqa: F401
        return True
    except ImportError:
        return False


# ── Determinism ───────────────────────────────────────────────────────────────

def test_two_builds_are_byte_identical():
    first, hex_a, b64_a = pkg.build(cloud="aws", workload="echo_diag")
    second, hex_b, b64_b = pkg.build(cloud="aws", workload="echo_diag")
    assert first == second, "package is not reproducible — terraform will redeploy forever"
    assert hex_a == hex_b and b64_a == b64_b


def test_every_entry_uses_the_fixed_timestamp_and_mode():
    blob, _hex, _b64 = pkg.build(cloud="aws", workload="echo_diag")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), info.filename
            assert info.external_attr == (0o644 << 16), info.filename
            assert info.create_system == 3, info.filename


def test_entries_are_sorted():
    blob, _hex, _b64 = pkg.build(cloud="aws", workload="echo_diag")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        listed = archive.namelist()
    assert listed == sorted(listed), "insertion order is not deterministic"


def test_hashes_agree_with_the_bytes():
    import base64
    import hashlib
    blob, hex_digest, b64_digest = pkg.build(cloud="aws", workload="echo_diag")
    digest = hashlib.sha256(blob).digest()
    assert hex_digest == digest.hex()
    assert b64_digest == base64.b64encode(digest).decode("ascii")


def test_different_workloads_hash_differently():
    _a, hex_a, _ = pkg.build(cloud="aws", workload="echo_diag")
    _b, hex_b, _ = pkg.build(cloud="aws", workload="entitle_webhook_echo")
    assert hex_a != hex_b


# ── Per-cloud layout ──────────────────────────────────────────────────────────

def test_aws_layout():
    names = _names(pkg.build(cloud="aws", workload="echo_diag")[0])
    assert "aws_entry.py" in names
    assert "workload.py" in names
    assert "fnruntime/contract.py" in names
    assert "requirements.txt" not in names, "AWS needs no dependencies"


def test_gcp_entry_must_be_main_py():
    """The GCP Python buildpack looks for that exact filename; nothing in the
    Terraform module can point elsewhere."""
    names = _names(pkg.build(cloud="gcp", workload="echo_diag")[0])
    assert "main.py" in names
    assert "requirements.txt" in names
    assert "gcp_entry.py" not in names, "must be renamed, not copied verbatim"


def test_azure_entry_must_be_function_app_py():
    if not _azure_available():
        print("SKIP: azure-functions not installed")
        return
    names = _names(pkg.build(cloud="azure", workload="echo_diag")[0])
    assert "function_app.py" in names
    assert "host.json" in names
    assert any(n.startswith(".python_packages/lib/site-packages/azure/functions/")
               for n in names), "azure-functions was not vendored"


def test_azure_vendor_tree_contains_no_compiled_artifacts():
    """The dashboard image is multi-arch — a vendored .so can be built for the
    wrong architecture and fail at import time, in the cloud. This assertion is
    what stops someone later vendoring psycopg2-binary the same way."""
    if not _azure_available():
        print("SKIP: azure-functions not installed")
        return
    names = _names(pkg.build(cloud="azure", workload="echo_diag")[0])
    bad = [n for n in names if n.endswith((".so", ".pyd", ".dll", ".dylib"))]
    assert not bad, f"compiled artifacts in the package: {bad}"


def test_azure_build_fails_loudly_without_the_vendor_dependency():
    """A silent skip would produce an app that boots, serves the default landing
    page and registers zero functions."""
    if _azure_available():
        print("SKIP: azure-functions is installed; the failure path can't be exercised")
        return
    try:
        pkg.build(cloud="azure", workload="echo_diag")
    except pkg.CloudFunctionPackageError as exc:
        assert "azure-functions" in str(exc)
    else:
        raise AssertionError("expected CloudFunctionPackageError")


# ── Hygiene ───────────────────────────────────────────────────────────────────

def test_no_bytecode_or_cache_is_packaged():
    for cloud in ("aws", "gcp"):
        for name in _names(pkg.build(cloud=cloud, workload="echo_diag")[0]):
            assert "__pycache__" not in name, name
            assert not name.endswith((".pyc", ".pyo")), name


def test_the_runtime_is_complete():
    names = _names(pkg.build(cloud="aws", workload="echo_diag")[0])
    for module in ("contract", "adapters", "auth", "logs", "dispatch", "__init__"):
        assert f"fnruntime/{module}.py" in names, module


def test_handlers_agree_with_the_packaged_entry_filenames():
    """HANDLERS is what Terraform declares; a drift here deploys a function whose
    handler points at a file that isn't in the zip."""
    assert pkg.HANDLERS["aws"].split(".")[0] + ".py" == pkg._LAYOUT["aws"]["entry"][1]
    assert pkg._LAYOUT["gcp"]["entry"][1] == "main.py"
    assert pkg.HANDLERS["gcp"] == "main"


# ── Errors + helpers ──────────────────────────────────────────────────────────

def test_unknown_workload_and_cloud_raise():
    for kwargs in ({"cloud": "aws", "workload": "nope"},
                   {"cloud": "digitalocean", "workload": "echo_diag"}):
        try:
            pkg.build(**kwargs)
        except pkg.CloudFunctionPackageError:
            pass
        else:
            raise AssertionError(f"expected an error for {kwargs}")


def test_workload_name_cannot_traverse_the_filesystem():
    for evil in ("../../etc/passwd", "..\\secrets", "/etc/passwd", ".hidden", ""):
        try:
            pkg.workload_source_path(evil)
        except pkg.CloudFunctionPackageError:
            pass
        else:
            raise AssertionError(f"path traversal accepted: {evil!r}")


def test_available_workloads_finds_the_catalog():
    found = pkg.available_workloads()
    assert "echo_diag" in found and "entitle_webhook_echo" in found
    assert not any(name.startswith("_") for name in found)


def test_object_key_embeds_the_hash():
    """Azure's WEBSITE_RUN_FROM_PACKAGE is a plain string setting: a fixed blob
    name would leave terraform seeing no diff while serving stale code."""
    key = pkg.object_key("fn-123", "abc123")
    assert key == "function-packages/fn-123/abc123.zip"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
