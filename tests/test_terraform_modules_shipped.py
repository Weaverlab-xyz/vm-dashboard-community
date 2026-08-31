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


# ── the provider mirror ───────────────────────────────────────────────────────
#
# Providers are installed from a read-only filesystem mirror baked into the image.
# That is not a performance choice: a writable TF_PLUGIN_CACHE_DIR is SYMLINKED into
# .terraform/providers, so a running apply/destroy executes the cached binary while a
# concurrent init rewrites it — "Error while installing hashicorp/google v5.45.2: ...
# text file busy", which killed a live clouddb_decommission (job cc8743c3). A mirror is
# only ever read. These pin the three halves that have to agree for that to hold.

def _dockerfile_text() -> str:
    with open(os.path.join(_REPO_ROOT, "Dockerfile"), "r", encoding="utf-8") as handle:
        return handle.read()


def _python_sources() -> list:
    """(path, source) for every .py under web_dashboard/."""
    out = []
    for root, _dirs, files in os.walk(os.path.join(_REPO_ROOT, "web_dashboard")):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                out.append((os.path.relpath(path, _REPO_ROOT), handle.read()))
    return out


def test_the_image_installs_providers_from_a_read_only_mirror():
    text = _dockerfile_text()
    assert "ENV TF_PROVIDER_MIRROR_DIR=" in text, (
        "the Dockerfile no longer declares TF_PROVIDER_MIRROR_DIR — the pre-cached "
        "providers have nowhere to live")
    assert "ENV TF_CLI_CONFIG_FILE=" in text, (
        "nothing points terraform at a CLI config, so the mirror is never consulted and "
        "every init falls back to downloading from registry.terraform.io")
    assert "provider_installation" in text and "filesystem_mirror" in text, (
        "no provider_installation/filesystem_mirror block is written into the image")
    assert re.search(r"direct \{.*exclude", text), (
        "the mirror config does not exclude `direct` installation — terraform will still "
        "reach registry.terraform.io, and an install it decides to do there re-downloads "
        "over a provider binary a running apply is executing (ETXTBSY)")

    # The config must NAME the mirror via ${TF_PROVIDER_MIRROR_DIR}, so the directory the
    # pre-cache populates and the directory the config serves cannot drift apart — and
    # the file it is written to must be the one TF_CLI_CONFIG_FILE names.
    written = re.search(r'"\$\{TF_PROVIDER_MIRROR_DIR\}"\s*>\s*(\S+)', text)
    assert written, (
        "the tfrc hardcodes a mirror path instead of expanding ${TF_PROVIDER_MIRROR_DIR}; "
        "it can now silently point at an empty directory")
    configured = re.search(r"ENV TF_CLI_CONFIG_FILE=(\S+)", text)
    assert configured and configured.group(1) == written.group(1), (
        f"TF_CLI_CONFIG_FILE points at {configured.group(1) if configured else None} but "
        f"the mirror config is written to {written.group(1)}")


def test_the_image_does_not_set_a_runtime_plugin_cache():
    """TF_PLUGIN_CACHE_DIR is a BUILD-time detail (it is how the providers get
    downloaded). Left set at run time and pointed at the mirror, terraform tries to
    install the mirror over itself: "cannot install existing provider directory ... to
    itself" — every init, every job."""
    offenders = [
        line.strip()
        for line in _dockerfile_text().splitlines()
        if line.strip().startswith("ENV ") and "TF_PLUGIN_CACHE_DIR" in line
    ]
    assert not offenders, (
        "TF_PLUGIN_CACHE_DIR is set as an image-scope ENV and will leak into every "
        f"run-time terraform: {offenders}")


def test_no_service_sets_the_plugin_cache_in_a_subprocess_env():
    """Same failure, from the other side: the PRA / Password-Safe / Entitle services
    build their own env dicts, and each used to re-assert the cache path."""
    offenders = [
        f"{path}:{i + 1}"
        for path, src in _python_sources()
        for i, line in enumerate(src.splitlines())
        if re.search(r'\[["\']TF_PLUGIN_CACHE_DIR["\']\]\s*=|'
                     r'["\']TF_PLUGIN_CACHE_DIR["\']\s*:', line)
    ]
    assert not offenders, (
        "these set TF_PLUGIN_CACHE_DIR for a terraform subprocess; with the image's "
        "read-only mirror that makes every init fail with 'cannot install existing "
        f"provider directory ... to itself': {offenders}")


def test_the_build_proves_the_mirror_can_serve_every_shipped_module():
    """A pre-cache leg that no longer satisfies a module's constraint must fail the
    BUILD. Without this check the miss surfaces months later as a customer's provision
    dying in a background job with 'was not found in any of the search locations'."""
    text = _dockerfile_text()
    assert "provider mirror MISS" in text and "/app/terraform" in text, (
        "the Dockerfile no longer verifies the mirror against the shipped modules")


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
