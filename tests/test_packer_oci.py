"""Invariants for the OCI (oracle-oci) Packer builder and the OCI storage backend.

Everything here would otherwise only surface on a live Oracle tenancy, inside a
container, ten minutes into a build:

  * the OCI signing key is a TENANCY-WIDE credential (compute, Vault, Object
    Storage, OKE), so the one thing that must never regress is it appearing in
    the generated template or the archived copy of it;
  * `key` (inline PEM) landed in plugin v1.1.2, so the required_plugins
    constraint and the plugin the Docker image pre-caches have to agree — a
    mismatch is a `packer init` failure on the worker, never in CI;
  * OCI rejects a shape_config on a fixed shape and requires one on a Flex
    shape, and the build instance needs a public IP because Packer runs outside
    the VCN;
  * the storage backend is dispatched through six hand-maintained tables, and a
    missed entry is a silent KeyError at the END of a multi-GB build.

packer_service imports only stdlib at module level, so the generator is
exercised for real rather than asserted on as source text.

Run: python tests/test_packer_oci.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load(*parts):
    """Load a module straight off disk. packer_service is stdlib-only, so this
    works without the app's dependency set installed (same trick as
    test_oci_freetier.py)."""
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(parts[-1][:-3] + "_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load("web_dashboard", "services", "packer_service.py")

_FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----"


def _tpl(**kw):
    args = dict(
        base_image_ocid="ocid1.image.oc1.iad.aaaaaaaa",
        shape="VM.Standard.E2.1.Micro",
        availability_domain="Uocm:PHX-AD-1",
        compartment_ocid="ocid1.compartment.oc1..aaaa",
        subnet_ocid="ocid1.subnet.oc1.iad.aaaa",
        ssh_username="opc",
        image_name="my image",
        has_provisioner=False,
    )
    args.update(kw)
    return ps.generate_oci_template(**args)


# ── the credential never reaches the template ────────────────────────────────

def test_the_signing_key_is_a_variable_reference_not_a_literal():
    t = _tpl()
    assert "key          = var.oci_key" in t
    assert "BEGIN RSA PRIVATE KEY" not in t and _FAKE_PEM not in t
    assert 'variable "oci_key"' in t and "sensitive = true" in t


def test_the_key_variable_is_typed_string():
    """PKR_VAR_ values are taken literally only for string/number variables; for
    any other (or inferred) type Packer parses the value as an HCL expression,
    which a multi-line PEM is not."""
    decl = re.search(r'variable "oci_key" \{(.*?)\}', _tpl(), re.S).group(1)
    assert "type      = string" in decl, "oci_key must be explicitly typed string"


def test_provisioner_secrets_are_variable_references_not_values():
    """The sensitive-var path carries env-var name → Packer variable NAME; the
    resolved secret reaches the build only as PKR_VAR_* on the subprocess env.
    Asserted explicitly because a reader (and CodeQL's name heuristic) can
    reasonably suspect the opposite from the dict flowing into the template."""
    t = _tpl(has_provisioner=True,
             provisioner_env={"BT_ADMIN_USER": "adminuser"},
             provisioner_sensitive_var_names={"TOKEN": "penv_0"})
    assert 'variable "penv_0"' in t and "sensitive = true" in t
    assert '"TOKEN=${var.penv_0}"' in t, "the secret must be a variable reference"
    assert "hunter2" not in t  # nothing resembling a value is ever passed in
    # The plain literal IS inlined — that is the documented difference between
    # the two paths, and the reason the secret-ref toggle exists.
    assert '"BT_ADMIN_USER=adminuser"' in t


def test_the_runner_passes_the_key_only_through_the_env():
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def _run_oci_build\(.*?\n(.*?)\n# ── Shared helpers", src, re.S).group(1)
    assert 'env["PKR_VAR_oci_key"] = private_key' in body
    assert "write_text(private_key)" not in body, "the PEM must never touch disk"
    assert "private_key=" not in body, "the PEM must not be a generate_oci_template kwarg"


# ── the plugin the image caches satisfies the template's constraint ──────────

def test_the_dockerfile_precaches_the_oracle_plugin():
    loop = re.search(r"for plugin in ([a-z ]+); do", _read("Dockerfile")).group(1).split()
    assert "oracle" in loop, "packer init has no network access on the worker"


def test_the_inline_key_constraint_is_declared():
    t = _tpl()
    assert 'source  = "github.com/hashicorp/oracle"' in t
    assert 'version = ">= 1.1.2"' in t, "`key` (inline PEM) landed in plugin v1.1.2"


# ── shape_config is emitted exactly when OCI wants it ────────────────────────

def test_flex_shape_emits_shape_config():
    t = _tpl(shape="VM.Standard.A1.Flex", ocpus=2, memory_gb=12)
    assert "shape_config {" in t
    assert "ocpus = 2.0" in t and "memory_in_gbs = 12.0" in t


def test_fixed_shape_emits_no_shape_config():
    assert "shape_config" not in _tpl(shape="VM.Standard.E2.1.Micro")


def test_the_build_instance_always_gets_a_public_ip():
    """Packer runs in the worker container, outside the VCN, and SSHes to the
    instance's public address. assign_public_ip is *bool in the plugin — unset
    means 'let the subnet decide', which is not a decision to defer here."""
    t = _tpl()
    assert "create_vnic_details {" in t and "assign_public_ip = true" in t


def test_boot_volume_uses_the_option_the_plugin_actually_spells():
    """The Go field is BootVolumeSizeInGBs but the HCL option is `disk_size`."""
    assert "disk_size" in _tpl(boot_volume_gb=100)
    assert "disk_size" not in _tpl()          # omitted → base image default
    assert "boot_volume_size_in_gbs" not in _tpl(boot_volume_gb=100)


def test_the_transient_instance_is_tagged_for_orphan_hunting():
    t = _tpl()
    assert "instance_tags" in t and 'purpose  = "packer-build"' in t


# ── name sanitization ────────────────────────────────────────────────────────

def test_safe_oci_name():
    assert ps._safe_oci_name("my image!") == "my-image-"
    assert ps._safe_oci_name("ok.name_1-2") == "ok.name_1-2"
    assert len(ps._safe_oci_name("x" * 400)) == 200
    assert "-{{timestamp}}" in _tpl(image_name="a/b")


def test_inlined_values_are_hcl_escaped():
    """Every inlined literal goes through _hcl_escape — three of them come from
    the operator-typed config store rather than a validated request model."""
    assert '\\"' in _tpl(availability_domain='a"b')


# ── the build is wired end to end ────────────────────────────────────────────

def test_the_build_is_registered_at_every_point():
    api = _read("web_dashboard", "api", "packer.py")
    assert '@router.post("/oci/build"' in api and 'job_type="packer_oci_build"' in api
    assert 'require_permission("oci", "write")' in api

    worker = _read("web_dashboard", "jobs_worker.py")
    # Membership in the two places that matter, rather than a count of mentions: the job
    # types are now ALSO listed in jobs_worker's concurrency tier tuples, so "appears
    # exactly twice" stopped meaning "registered in both places". Claimable but
    # undispatchable is a job that goes `running` and dies to the stale reconciler;
    # dispatchable but unclaimable is dead code that leaves the job `pending` forever.
    handled = re.search(r"^HANDLED_TYPES = \((.*?)^\)", worker, re.S | re.M).group(1)
    dispatch = re.search(r'job_type in \("packer_aws_build".*?\)', worker, re.S).group(0)
    export = re.search(r'job_type in \("aws_export_image".*?\)', worker, re.S).group(0)
    assert '"packer_oci_build"' in handled, "packer_oci_build is not claimable"
    assert '"packer_oci_build"' in dispatch, "packer_oci_build has no dispatch branch"
    assert '"oci_export_image"' in handled, "oci_export_image is not claimable"
    assert '"oci_export_image"' in export, "oci_export_image has no dispatch branch"

    svc = _read("web_dashboard", "services", "packer_build_service.py")
    # Whitespace-tolerant: these dict entries are column-aligned with their siblings.
    for key, value in (("packer_oci_build", "OCIPackerBuildRequest"),
                       ("packer_oci_build", "_run_oci_build"),
                       ("oci_export_image", "run_export_oci")):
        assert re.search(rf'"{key}":\s*{value}\b', svc), (
            f'packer_build_service is missing "{key}": {value}')


def test_the_free_tier_gate_matches_the_deploy_path():
    """The build form uses the same warn-and-confirm contract as the deploy form
    (code=free_tier_exceeded), so the client can key off it identically."""
    api = _read("web_dashboard", "api", "packer.py")
    assert "oci_freetier.evaluate(" in api
    assert '"code": "free_tier_exceeded"' in api
    assert "acknowledge_charges" in api


def test_the_page_posts_what_the_model_reads():
    page = _read("web_dashboard", "templates", "oci", "index.html")
    assert "/api/packer/oci/build" in page
    for field in ("base_image_ocid", "shape", "subnet_ocid", "availability_domain",
                  "ssh_username", "boot_volume_gb", "acknowledge_charges",
                  "bt_epml_source"):
        assert field in page, f"the OCI build form does not send {field}"
    assert "packer_oci_build" in _read("web_dashboard", "templates", "jobs", "detail.html")


def test_the_build_form_only_offers_public_subnets():
    """A subnet with prohibit_public_ip can never work — Packer reaches the build
    instance over the internet."""
    page = _read("web_dashboard", "templates", "oci", "index.html")
    assert "packerBuildSubnets()" in page
    assert "prohibit_public_ip" in page


# ── the storage backend is registered in every dispatch table ────────────────
#
# storage_service imports config_service at call time only, but the module-level
# tables are plain literals — read them out of the AST so this runs without the
# app's dependency set.

def _storage_src():
    return _read("web_dashboard", "services", "storage_service.py")


def _literal_table(src: str, name: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
    raise AssertionError(f"{name} not found in storage_service")


def _table_keys(src: str, name: str) -> set:
    node = _literal_table(src, name)
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    if isinstance(node, ast.Tuple):
        return {e.value for e in node.elts if isinstance(e, ast.Constant)}
    raise AssertionError(f"{name} is neither a dict nor a tuple literal")


OCI_BACKEND = "oci_object_storage"


def test_the_oci_backend_is_in_every_storage_dispatch_table():
    src = _storage_src()
    assert OCI_BACKEND in _table_keys(src, "BACKENDS")
    for table in ("_BACKEND_OPS", "_IMAGE_OPS", "_ASSET_PREFIX_FN"):
        assert OCI_BACKEND in _table_keys(src, table), f"{OCI_BACKEND} missing from {table}"


def test_every_backend_implements_every_op():
    """A backend present in BACKENDS but missing an op is a KeyError at the end
    of a multi-GB build. No such parity check existed before this one."""
    src = _storage_src()
    backends = _table_keys(src, "BACKENDS")
    for table, required in (
        ("_BACKEND_OPS", {"list", "fetch", "upload", "delete"}),
        ("_IMAGE_OPS", {"upload", "download", "delete", "head", "copy", "presign"}),
    ):
        node = _literal_table(src, table)
        got = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        assert got == backends, f"{table} keys {sorted(got)} != BACKENDS {sorted(backends)}"
        for key, val in zip(node.keys, node.values):
            ops = {k.value for k in val.keys if isinstance(k, ast.Constant)}
            assert required <= ops, f"{table}['{key.value}'] is missing {sorted(required - ops)}"


def test_backend_configured_probe_covers_every_backend():
    """_backend_configured is an if-chain, not a dict — a missing branch returns
    False and the backend silently never activates. Nothing raises, ever."""
    src = _storage_src()
    body = re.search(r"def _backend_configured\(.*?\n(.*?)\ndef ", src, re.S).group(1)
    for backend in _table_keys(src, "BACKENDS"):
        assert f'backend == "{backend}"' in body, (
            f"_backend_configured has no branch for '{backend}' — it would never activate")


def test_image_url_covers_every_backend():
    """image_url raises StorageError for an unknown backend, and it is called
    AFTER the export completes — the worst possible time to find out."""
    src = _storage_src()
    body = re.search(r"def image_url\(.*?\n(.*?)\n# ──", src, re.S).group(1)
    for backend in _table_keys(src, "BACKENDS"):
        assert f'backend == "{backend}"' in body, f"image_url has no branch for '{backend}'"


def test_the_api_backend_tables_match_the_service():
    """api/storage.py keys _REQUIRED_FIELDS and labels by BACKENDS with a bare
    subscript — a missing entry is a KeyError on GET /api/storage/backends,
    which breaks the Storage page and the Settings prereq gate."""
    api = _read("web_dashboard", "api", "storage.py")
    backends = _table_keys(_storage_src(), "BACKENDS")
    for table in ("_REQUIRED_FIELDS", "_BACKEND_KEYS"):
        block = re.search(rf"{table} = \{{(.*?)\n\}}", api, re.S).group(1)
        for backend in backends:
            assert f'"{backend}"' in block, f"{backend} missing from api/storage.py {table}"
    labels = re.search(r"labels = \{(.*?)\n    \}", api, re.S).group(1)
    for backend in backends:
        assert f'"{backend}"' in labels, f"{backend} has no label in api/storage.py"


def test_oci_is_blocked_as_the_active_backend():
    """It can host the image hub, but the ACTIVE backend also decides where
    Terraform state lives and terraform.py has no OCI state mapping — it would
    silently fall through to ephemeral local state and break `destroy`."""
    src = _storage_src()
    assert OCI_BACKEND in _table_keys(src, "ACTIVE_BACKEND_EXCLUSIONS")
    api = _read("web_dashboard", "api", "storage.py")
    assert "ACTIVE_BACKEND_EXCLUSIONS" in api, (
        "the PATCH handler must reject an excluded backend as active")


def test_terraform_state_tables_exclude_oci():
    """The flip side of the rule above: no OCI entry in the state tables, because
    OCI can never be the active backend that owns Terraform state."""
    src = _storage_src()
    for table in ("_STATE_KEYS", "_STATE_GET", "_STATE_PUT"):
        assert OCI_BACKEND not in _table_keys(src, table), (
            f"{OCI_BACKEND} must not appear in {table} — it is hub-only")


def test_the_hub_url_round_trips():
    """image_url writes it, _parse_hub_url reads it back. A mismatch strands
    every promote from an OCI-built image."""
    reg = _read("web_dashboard", "services", "image_registry_service.py")
    body = re.search(r"def _parse_hub_url\(.*?\n(.*?)\nasync def ", reg, re.S).group(1)
    assert 'url.startswith("oci://")' in body
    assert f'return ("{OCI_BACKEND}", key)' in body
    assert 'oci://' in _storage_src(), "image_url does not emit an oci:// URL"


def test_the_export_defaults_to_vhd():
    """VHD, not OCI's default QCOW2: it is the hub's canonical format, so the
    result needs no conversion and Azure (vhd-only) can consume it."""
    svc = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def export_and_register_oci\(.*?\n(.*?)\nasync def ", svc, re.S).group(1)
    assert 'export_format="VHD"' in body
    assert 'artefact_format="vhd"' in body
    assert 'source_cloud="oci"' in body


def test_the_export_waits_on_the_object_not_the_image_state():
    """export_image is async server-side and the image is ALREADY 'AVAILABLE'
    when it returns, so wait_until(lifecycle_state == AVAILABLE) satisfies itself
    instantly and hands back an object that was never written."""
    src = _read("web_dashboard", "services", "oci_service.py")
    body = re.search(r"def _export_image_sync\(.*?\n(.*?)\nasync def ", src, re.S).group(1)
    assert "head_object" in body, "the export wait must poll the destination object"
    assert "EXPORTING" in body


def test_a_missing_bucket_skips_the_export_instead_of_failing_the_build():
    """Same degradation contract as the other three clouds: a build that produced
    a real image must not be reported as failed because the hub isn't set up."""
    svc = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def export_and_register_oci\(.*?\n(.*?)\nasync def ", svc, re.S).group(1)
    assert 'result["export_skipped"]' in body
    assert "storage_oci_bucket" in body


# ── the launch placement is checked before a build starts ────────────────────
#
# LaunchInstance answers "shape not offered in this region", "image doesn't
# support this shape" and a real policy denial with the same unattributed
# 404 NotAuthorizedOrNotFound. The default shape VM.Standard.E2.1.Micro is
# Always-Free but only exists in the older regions — us-chicago-1 offers no E2
# shape at all — so the out-of-the-box build dies in one second with an error
# naming no field. These pin the precheck that turns that into a sentence.


def _oci_service():
    """oci_service does `from . import oci_freetier`, so it needs a package to be
    relative to. Stub one over services/ — both modules import the oci SDK lazily
    (inside _require_oci), so this loads without it."""
    import types
    pkg_name = "_oci_probe_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]
        sys.modules[pkg_name] = pkg
    name = pkg_name + ".oci_service"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, "web_dashboard", "services", "oci_service.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _placement(usable, shape="VM.Standard.E2.1.Micro"):
    """Run the precheck with a canned launchable-shape list. Returns the error
    message, or None when the placement was accepted."""
    mod = _oci_service()
    if isinstance(usable, Exception):
        def fake(*a, **k):
            raise usable
    else:
        def fake(*a, **k):
            return list(usable)
    mod._launchable_shapes_sync = fake
    mod._cfg = lambda key: "us-chicago-1" if key == "oci_region" else ""
    try:
        mod._check_launch_placement_sync(
            availability_domain="wYeM:US-CHICAGO-1-AD-1",
            image_ocid="ocid1.image.oc1.us-chicago-1.aaaa",
            shape=shape, compartment_id="ocid1.compartment.oc1..aaaa")
    except mod.OCIError as e:
        return str(e)
    return None


_CHICAGO_USABLE = ["VM.Standard.E5.Flex", "VM.Standard3.Flex",
                   "BM.Standard.E5.192", "BM.Standard3.64"]


def test_a_shape_the_region_does_not_offer_is_refused_by_name():
    msg = _placement(_CHICAGO_USABLE)
    assert msg, "E2.1.Micro in a region with no E2 shape was allowed through"
    assert "VM.Standard.E2.1.Micro" in msg, "the error must name the rejected shape"
    assert "wYeM:US-CHICAGO-1-AD-1" in msg, "the error must name the placement"
    assert "VM.Standard.E5.Flex" in msg, "the error must list what would work"


def test_a_launchable_shape_is_accepted():
    assert _placement(_CHICAGO_USABLE, shape="VM.Standard.E5.Flex") is None


def test_the_precheck_fails_open_when_the_lookup_breaks():
    """A listing call that can't reach OCI is not evidence the placement is
    wrong, and must never be the reason a build refuses to start."""
    assert _placement(RuntimeError("connection reset")) is None
    assert _placement([]) is None, "an empty intersection tells us nothing"


def test_the_no_free_shape_case_points_at_the_ampere_alternative():
    """In a region with no E2, nothing an x86 image can launch on is free — say
    so, because the free-tier gate upstream just approved E2.1.Micro as free."""
    msg = _placement(_CHICAGO_USABLE)
    assert "None of them are Always-Free" in msg
    assert "A1.Flex" in msg and "aarch64" in msg
    # ...and stay quiet about it when a free shape *is* among the usable ones.
    # (A1.Flex still appears there, as the shape to switch to — that's the list,
    # not the note.)
    assert "None of them are Always-Free" not in (_placement(["VM.Standard.A1.Flex"]) or "")


def test_both_entry_points_precheck_the_placement():
    """The route gives the operator the error at submit time; the runner covers a
    re-queued job and the route's fail-open path.

    The two OCI *deploy* endpoints run the same check for the same reason — that
    wiring, and the per-image bulk case, is pinned in test_oci_deploy_placement.py."""
    api = _read("web_dashboard", "api", "packer.py")
    assert "check_launch_placement(" in api
    assert '"code": "shape_not_launchable"' in api
    # Before the free-tier prompt: a shape that cannot launch must not be waved
    # through an "acknowledge charges" dialog first.
    assert api.index("check_launch_placement(") < api.index("oci_freetier.evaluate("), \
        "the placement gate must precede the free-tier prompt"
    svc = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def _run_oci_build\(.*?\n(.*?)\nasync def ", svc, re.S).group(1)
    assert "check_launch_placement(" in body
    # Surfaced as a PackerError, else the job reports it as "Unexpected error".
    assert "raise PackerError(str(exc))" in body


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
