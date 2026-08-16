"""Build the deployable zip for a cloud function — pure, deterministic, no I/O
beyond reading the source tree.

The packaging strategy is one line: **build one zip, upload it to an object store
in the same cloud as the function, and have Terraform reference it by bucket + key
+ content hash.** GCP forces that shape (``cloudfunctions2``'s ``build_config.source``
accepts only ``storage_source`` or ``repo_source`` — there is no inline option), so
AWS and Azure match it and the transport stops being a per-cloud concern. What is
left is the zip LAYOUT, which is the ``_LAYOUT`` table below.

**Determinism is not a nicety.** Terraform decides whether to redeploy by comparing
the content hash, so if a build timestamp or a directory-walk order leaks into the
zip, every ``apply`` redeploys every function forever — on GCP that is a 60-120 s
Cloud Build each time. Hence: a fixed 1980 timestamp on every entry, sorted
insertion order, fixed permissions, fixed compression level, and no ``__pycache__``.
``tests/test_function_package.py`` asserts two consecutive builds are byte-identical.

Azure is the only cloud that needs vendored dependencies, because
``WEBSITE_RUN_FROM_PACKAGE`` mounts the zip read-only and never runs ``pip install``.
Thanks to the stdlib-only rule in ``web_dashboard/functions/``, that vendor set is
exactly one pure-Python wheel: ``azure-functions``.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import zipfile

# Repo-relative root of the deployable source (web_dashboard/functions).
_FUNCTIONS_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "functions"))

VALID_CLOUDS = ("aws", "azure", "gcp")

# ZIP epoch. The format cannot store anything earlier, and a constant is the whole
# point — an mtime here is what makes builds non-reproducible across the app
# container and the jobs-worker container.
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644 << 16
_COMPRESS_LEVEL = 9

_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".DS_Store")

# Compiled extensions must never be vendored: the dashboard image is multi-arch, so
# a wheel built on the arm64 image would ship an arm64 binary into an x86_64 Lambda
# and fail at import time, in the cloud, with a confusing message.
_BINARY_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")

# Per-cloud zip layout. Each entry:
#   entry   (source path under functions/, destination name at the zip root)
#   extra   additional (source, destination) pairs
# The destination names for GCP and Azure are FIXED BY THE PLATFORM — the GCP
# Python buildpack looks for main.py, and the Azure v2 programming model requires
# function_app.py at the root with no way to point elsewhere.
_LAYOUT = {
    "aws": {
        "entry": ("fnentry/aws_entry.py", "aws_entry.py"),
        "extra": (),
    },
    "gcp": {
        "entry": ("fnentry/gcp_entry.py", "main.py"),
        "extra": (("fnentry/requirements.gcp.txt", "requirements.txt"),),
    },
    "azure": {
        "entry": ("fnentry/azure_entry.py", "function_app.py"),
        "extra": (("fnentry/host.json", "host.json"),),
    },
}

# Where Azure expects vendored dependencies inside a run-from-package zip.
_AZURE_VENDOR_ROOT = ".python_packages/lib/site-packages"

# What Terraform declares as the handler, per cloud. Lives here (next to the entry
# filenames it must agree with) rather than in the service, so the two cannot drift.
HANDLERS = {
    "aws": "aws_entry.lambda_handler",
    "gcp": "main",
    "azure": "",           # discovered by the host; not declarable in Terraform
}


class CloudFunctionPackageError(Exception):
    """Raised when a package cannot be built (unknown cloud/workload, missing
    vendor dependency, unsafe binary artifact)."""


def _functions_root(source_root: str = "") -> str:
    return source_root or _FUNCTIONS_ROOT


def available_workloads(source_root: str = "") -> tuple:
    """Workload names discoverable on disk, sorted. The filesystem is the source of
    truth; the service's VALID_WORKLOADS is asserted against this in tests."""
    workload_dir = os.path.join(_functions_root(source_root), "fnworkloads")
    if not os.path.isdir(workload_dir):
        return ()
    names = []
    for entry in os.listdir(workload_dir):
        if entry.endswith(".py") and not entry.startswith("_"):
            names.append(entry[:-3])
    return tuple(sorted(names))


def workload_source_path(workload: str, source_root: str = "") -> str:
    """Absolute path to a workload module. Raises if it does not exist."""
    if not workload or "/" in workload or "\\" in workload or workload.startswith("."):
        raise CloudFunctionPackageError(f"invalid workload name {workload!r}")
    path = os.path.join(_functions_root(source_root), "fnworkloads", f"{workload}.py")
    if not os.path.isfile(path):
        raise CloudFunctionPackageError(
            f"unknown workload {workload!r} "
            f"(available: {', '.join(available_workloads(source_root)) or 'none'})")
    return path


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _walk_tree(root: str, arc_prefix: str) -> list:
    """``(arcname, bytes)`` for every shippable file under ``root``."""
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(_SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            collected.append((f"{arc_prefix}/{rel}", _read(full)))
    return collected


def _vendor_azure_functions() -> list:
    """The ``azure.functions`` package, for Azure's read-only run-from-package.

    Raises rather than degrading: a Function App missing this dependency deploys
    successfully, boots, serves the default landing page, and registers zero
    functions — a failure that costs far more to diagnose than to prevent.
    """
    try:
        import azure.functions as azure_functions
    except ImportError as exc:
        raise CloudFunctionPackageError(
            "azure-functions is not installed, so an Azure package cannot be built. "
            "It is a pure-Python wheel pinned in web_dashboard/requirements.txt; "
            "run pip install -r requirements.txt.") from exc

    package_dir = os.path.dirname(os.path.abspath(azure_functions.__file__))
    entries = _walk_tree(package_dir, f"{_AZURE_VENDOR_ROOT}/azure/functions")
    for arcname, _payload in entries:
        if arcname.endswith(_BINARY_SUFFIXES):
            raise CloudFunctionPackageError(
                f"refusing to vendor the compiled artifact {arcname!r}: the dashboard "
                "image is multi-arch, so a binary vendored here can be built for the "
                "wrong architecture and fail at import time in the cloud. Use a "
                "pure-Python library instead.")
    # `azure` is a PEP 420 namespace package (no __init__.py), so shipping only the
    # `functions` subpackage is correct and importing `azure.functions` still works.
    return entries


def collect_entries(*, cloud: str, workload: str, source_root: str = "") -> list:
    """Every ``(arcname, bytes)`` that goes into the zip, unsorted."""
    if cloud not in _LAYOUT:
        raise CloudFunctionPackageError(
            f"unsupported cloud {cloud!r} (supported: {', '.join(VALID_CLOUDS)})")
    root = _functions_root(source_root)
    layout = _LAYOUT[cloud]

    entries = _walk_tree(os.path.join(root, "fnruntime"), "fnruntime")
    if not entries:
        raise CloudFunctionPackageError(f"no runtime source found under {root}")

    # Exactly one workload per function, always at the same name, so the entry
    # shims can `import workload` with no per-function generation.
    entries.append(("workload.py", _read(workload_source_path(workload, source_root))))

    entry_src, entry_dst = layout["entry"]
    entries.append((entry_dst, _read(os.path.join(root, entry_src))))
    for extra_src, extra_dst in layout["extra"]:
        entries.append((extra_dst, _read(os.path.join(root, extra_src))))

    if cloud == "azure":
        entries.extend(_vendor_azure_functions())
    return entries


def _zip(entries: list) -> bytes:
    """Deterministic zip. See the module docstring for why each rule matters."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=_COMPRESS_LEVEL) as archive:
        for arcname, payload in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(filename=arcname, date_time=_FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _FILE_MODE
            info.create_system = 3          # Unix, regardless of the build host
            archive.writestr(info, payload)
    return buffer.getvalue()


def build(*, cloud: str, workload: str, source_root: str = "") -> tuple:
    """``(zip_bytes, sha256_hex, sha256_base64)``.

    ``sha256_hex`` names the object (``function-packages/<fn_id>/<sha256>.zip``);
    ``sha256_base64`` is what ``aws_lambda_function.source_code_hash`` wants.
    Identical inputs produce byte-identical output, so an unchanged function is a
    Terraform no-op.
    """
    blob = _zip(collect_entries(cloud=cloud, workload=workload, source_root=source_root))
    digest = hashlib.sha256(blob).digest()
    return blob, digest.hex(), base64.b64encode(digest).decode("ascii")


def object_key(fn_id: str, sha256_hex: str) -> str:
    """Where the artifact lives in the object store.

    The hash is IN THE KEY on purpose. Azure's ``WEBSITE_RUN_FROM_PACKAGE`` is a
    plain app-setting string, so overwriting a fixed-name blob would leave
    Terraform seeing no diff while the app keeps serving the old code. Keying by
    content sidesteps that on all three clouds and makes rollback a key change.
    """
    return f"function-packages/{fn_id}/{sha256_hex}.zip"
