"""Every Terraform module a service drives must actually reach the image.

The failure this prevents is nasty because it is INVISIBLE IN DEV: the module is
right there on disk, so a local run works, and only the published image fails —
at deploy time, inside a background job, with ``terraform._materialize`` raising
"No such file or directory: /app/terraform/<module>". The repo has branches named
`fix/dockerfile-clouddb-cloud-modules` and `fix/dockerignore-clouddb-modules` from
hitting it twice.

Two files must agree for a module to ship:

  Dockerfile     a COPY line for the module (or a parent of it)
  .dockerignore  a `!terraform/<top-level>` re-include, because `terraform/*`
                 excludes everything otherwise — and a COPY whose source resolves
                 to nothing FAILS THE BUILD

This parses the source rather than importing it, so it runs with no dependencies
installed and cannot be broken by an unrelated import error.
"""
import os
import re
import sys

_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_SERVICES_DIR = os.path.join(_REPO_ROOT, "web_dashboard", "services")

# os.path.join(_REPO_ROOT, "terraform", "a"[, "b"...])
_MODULE_RE = re.compile(
    r'os\.path\.join\(\s*_REPO_ROOT\s*,\s*["\']terraform["\']((?:\s*,\s*["\'][^"\']+["\'])+)\s*\)')
_SEGMENT_RE = re.compile(r'["\']([^"\']+)["\']')

# Created at runtime per job (terraform/deployments/<job_id>), never shipped.
_RUNTIME_DIRS = {"deployments"}


def _declared_modules() -> dict:
    """``{"terraform/db_mysql": "cloud_database_service.py", ...}``."""
    modules = {}
    for filename in sorted(os.listdir(_SERVICES_DIR)):
        if not filename.endswith(".py"):
            continue
        with open(os.path.join(_SERVICES_DIR, filename), "r", encoding="utf-8") as handle:
            source = handle.read()
        for match in _MODULE_RE.finditer(source):
            segments = _SEGMENT_RE.findall(match.group(1))
            if not segments or segments[0] in _RUNTIME_DIRS:
                continue
            modules.setdefault("terraform/" + "/".join(segments), filename)
    return modules


def _dockerfile_copy_sources() -> list:
    path = os.path.join(_REPO_ROOT, "Dockerfile")
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    sources = []
    for line in lines:
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()[1:]
        if len(parts) >= 2 and parts[0].startswith("terraform/"):
            sources.append(parts[0].rstrip("/"))
    return sources


def _dockerignore_reincludes() -> set:
    path = os.path.join(_REPO_ROOT, ".dockerignore")
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    return {
        line.strip()[1:].rstrip("/")
        for line in lines
        if line.strip().startswith("!terraform/")
    }


def test_declared_modules_exist_on_disk():
    missing = [
        f"{module} (declared in {service})"
        for module, service in _declared_modules().items()
        if not os.path.isdir(os.path.join(_REPO_ROOT, module.replace("/", os.sep)))
    ]
    assert not missing, "service points at a module that does not exist: " + "; ".join(missing)


def test_every_declared_module_is_copied_into_the_image():
    copies = _dockerfile_copy_sources()
    missing = []
    for module, service in sorted(_declared_modules().items()):
        # A COPY of a parent directory covers its children.
        covered = any(module == src or module.startswith(src + "/") for src in copies)
        if not covered:
            missing.append(f"{module} (driven by {service})")
    assert not missing, (
        "no Dockerfile COPY line ships these modules — the published image will fail "
        "at terraform._materialize: " + "; ".join(missing))


def test_every_copied_module_is_reincluded_in_the_dockerignore():
    """`terraform/*` excludes everything; without the re-include the COPY source
    resolves to nothing and the BUILD fails."""
    reincludes = _dockerignore_reincludes()
    missing = []
    for source in sorted(set(_dockerfile_copy_sources())):
        top_level = "/".join(source.split("/")[:2])     # terraform/<name>
        if top_level not in reincludes:
            missing.append(f"{source} → needs !{top_level}")
    assert not missing, (
        ".dockerignore excludes a module the Dockerfile COPYs: " + "; ".join(missing))


def test_the_scan_actually_found_something():
    """Guards against the regex silently matching nothing after a refactor, which
    would make every assertion above vacuously true."""
    modules = _declared_modules()
    assert len(modules) >= 10, f"only found {len(modules)} modules — has the pattern changed?"
    assert "terraform/db_postgres" in modules


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
