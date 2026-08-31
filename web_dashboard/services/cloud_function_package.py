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

# Compiled extensions must never be vendored OUT OF SITE-PACKAGES: this image is
# built multi-arch, so a wheel installed for the build host would ship an arm64
# binary into an x86_64 function and fail at import time, in the cloud, with a
# confusing message.
#
# The exception is _VENDOR_DIR, which the Dockerfile populates with wheels fetched
# for the FUNCTION's platform (`pip install --platform manylinux2014_x86_64`). Those
# are correct by construction, so binaries from there are allowed and binaries from
# anywhere else still are not — the hazard the rule exists for stays closed.
_BINARY_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")

# Where the Dockerfile stages the function-platform wheels. Overridable so a dev
# box (or a future arm64 target) can point elsewhere.
_VENDOR_DIR = os.environ.get("FN_VENDOR_DIR", "/opt/fn-vendor/linux-x86_64")

# Distributions a workload needs inside the zip, beyond the stdlib. Only db_grant
# has any — everything else is stdlib-only by contract, which is what keeps a
# package ~30 KB and needs no build step.
#
# The SQL Server chain is the whole reason _VENDOR_DIR exists: python-tds is pure
# Python, but it does TLS through pyOpenSSL → cryptography, which is compiled. Azure
# SQL Database REQUIRES encryption, so there is no "skip TLS" escape.
_WORKLOAD_VENDOR = {
    "db_grant": ("pymysql", "pytds", "OpenSSL", "cryptography", "cffi", "_cffi_backend"),
    # Same chain, same reason: ps_dbops opens the database connection the Password
    # Safe plugin's cloud-run channel exists to make, and Cloud SQL for SQL Server
    # requires encryption. Identical to db_grant's set deliberately — the image's
    # /opt/fn-vendor leg already carries it, so this workload needs no Dockerfile
    # change and no second vendoring decision to keep in step.
    "ps_dbops": ("pymysql", "pytds", "OpenSSL", "cryptography", "cffi", "_cffi_backend"),
}

# Extra dashboard modules copied into the zip for a workload, as
# (path relative to web_dashboard/, destination name at the zip root).
#
# db_grant gets the REAL cloud_db_sql_service, not a reimplementation of it, so the
# SQL executed in the cloud is byte-identical to the SQL tests/test_db_grant_sql.py
# proves — the alternative is two copies of security-critical SQL drifting apart.
# It is safe to ship because that module is stdlib-only (re, secrets, string) and
# opens no connection; tests/test_db_grant_workload.py pins both properties.
_WORKLOAD_MODULES = {
    "db_grant": (("services/cloud_db_sql_service.py", "sqlplan.py"),),
    # Same arrangement: the dashboard's Portainer client and this adapter must not
    # disagree about which user a name refers to or whether a membership exists, so
    # they share one tested copy of the rules. Stdlib-only and I/O-free.
    "portainer_access": (("services/portainer_access_rules.py", "portainerrules.py"),),
    # The scope/role allowlists and the escalation-role refusal. Pure and I/O-free,
    # so the guards that stop this adapter becoming a privilege-escalation
    # primitive are testable without an Azure subscription.
    "azure_role_grant": (("services/azure_role_rules.py", "azureroles.py"),),
}

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


def _vendor_workload_packages(workload: str, arc_prefix: str) -> list:
    """Distributions this workload needs, taken from the build-time vendor dir.

    Returns ``[]`` for a workload with no dependencies, which is all of them but
    ``db_grant``. Raises if the vendor dir is missing or incomplete rather than
    shipping a package that imports fine locally and dies on first invocation.
    """
    wanted = _WORKLOAD_VENDOR.get(workload)
    if not wanted:
        return []
    if not os.path.isdir(_VENDOR_DIR):
        raise CloudFunctionPackageError(
            f"the {workload!r} workload needs vendored database drivers, but "
            f"{_VENDOR_DIR} does not exist. It is populated at image build time "
            "(see the FN_VENDOR_DIR step in the Dockerfile); set FN_VENDOR_DIR if "
            "you staged the wheels elsewhere.")

    def _arc(name: str) -> str:
        return f"{arc_prefix}/{name}" if arc_prefix else name

    entries = []
    missing = []
    for name in wanted:
        pkg_dir = os.path.join(_VENDOR_DIR, name)
        module_py = os.path.join(_VENDOR_DIR, name + ".py")
        if os.path.isdir(pkg_dir):
            entries.extend(_walk_tree(pkg_dir, _arc(name)))
        elif os.path.isfile(module_py):
            entries.append((_arc(name + ".py"), _read(module_py)))
        else:
            # A compiled top-level module (_cffi_backend) lands as a bare .so whose
            # filename carries the ABI tag, so match on the prefix.
            hits = [f for f in os.listdir(_VENDOR_DIR)
                    if f.startswith(name + ".") and f.endswith(_BINARY_SUFFIXES)]
            if hits:
                for filename in hits:
                    entries.append((_arc(filename),
                                    _read(os.path.join(_VENDOR_DIR, filename))))
            else:
                missing.append(name)
    if missing:
        raise CloudFunctionPackageError(
            f"vendored driver(s) {', '.join(missing)} not found in {_VENDOR_DIR} — "
            "the image's vendor step and _WORKLOAD_VENDOR have drifted apart")
    return entries


def source_tree_sha256(source_root: str = "") -> str:
    """A hash of the handler source tree — every workload and the runtime.

    This is the provenance statement that is ALWAYS available. A git commit is
    best-effort (an image can be built without the build arg, or from a tarball),
    but the source that produced a package is right there on disk, so this can be
    computed unconditionally and cannot be wrong.

    Deterministic for the same reasons ``build`` is: sorted order, content only.
    """
    root = _functions_root(source_root)
    digest = hashlib.sha256()
    entries = (_walk_tree(os.path.join(root, "fnruntime"), "fnruntime")
               + _walk_tree(os.path.join(root, "fnworkloads"), "fnworkloads")
               + _walk_tree(os.path.join(root, "fnentry"), "fnentry"))
    for arcname, payload in sorted(entries, key=lambda item: item[0]):
        digest.update(arcname.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


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

    # Dashboard modules this workload reuses verbatim. Always at the zip root —
    # they are imported by the workload, not by the platform's dependency loader.
    web_root = os.path.dirname(root)
    for module_src, module_dst in _WORKLOAD_MODULES.get(workload, ()):
        entries.append((module_dst, _read(os.path.join(web_root, module_src))))

    # Workload dependencies. AWS and GCP put the zip root on sys.path, so packages
    # go at the root; Azure's run-from-package only searches .python_packages.
    entries.extend(_vendor_workload_packages(
        workload, _AZURE_VENDOR_ROOT if cloud == "azure" else ""))
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
