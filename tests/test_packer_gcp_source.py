"""The GCP Packer build's source image: a family, or a literal image name.

The googlecompute builder takes these as two different fields, and the
dashboard emitted only one of them — `source_image_family`. That is right for
the stock catalogue (debian-12, rocky-linux-9) and impossible for an image you
imported yourself, which as a rule carries no family at all. A bake against an
imported image therefore had no expressible source: the build ran, resolved the
family to whatever public Debian it named, and the provisioner failed a minute
in on the wrong OS — which is exactly how the vyos-cell bake failed.

So the invariants worth pinning are the ones that cost a whole build to notice:

  * a literal image name reaches the template as `source_image`, not as a
    family — and the two are never emitted together, since Packer would take
    the literal and silently ignore the family;
  * `source_image_project_id` is a LIST and appears only when asked for —
    emitted blind it would narrow the search away from the public projects that
    make `debian-12` resolve at all;
  * the request model still demands a source, now that neither field is
    individually required;
  * both fields are inlined into HCL, so the charset is pinned at the model.

packer_service imports only stdlib at module level and models/packer.py only
pydantic, so both are exercised for real rather than asserted on as source text.

Run: python tests/test_packer_gcp_source.py
"""
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load(*parts):
    """Load one module by path, with sibling relative imports resolvable.

    Loading by path rather than importing `web_dashboard.models.packer` is the point
    of this harness: it proves the module stands up on pydantic alone, without
    dragging in the app. But a bare `spec_from_file_location` gives the module no
    package, so a relative import of a SIBLING model (`from .schedule import …`)
    raises "attempted relative import with no known parent package".

    So the parent packages are registered as empty namespace modules whose `__path__`
    points at the real directories. A relative import then finds the real sibling
    file, and `web_dashboard/__init__.py` is still never executed — which is the
    property that keeps this test dependency-light.
    """
    import types

    pkg_parts = parts[:-1]
    for i in range(1, len(pkg_parts) + 1):
        name = ".".join(pkg_parts[:i])
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.__path__ = [os.path.join(_ROOT, *pkg_parts[:i])]
            sys.modules[name] = stub

    path = os.path.join(_ROOT, *parts)
    full_name = ".".join(pkg_parts + (parts[-1][:-3],))
    spec = importlib.util.spec_from_file_location(full_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load("web_dashboard", "services", "packer_service.py")
pm = _load("web_dashboard", "models", "packer.py")


def _tpl(**kw):
    args = dict(
        source_image="debian-12",
        machine_type="e2-medium",
        ssh_username="packer",
        image_name="vyos-cell",
        project_id="my-project",
        zone="us-central1-a",
        has_provisioner=False,
    )
    args.update(kw)
    return ps.generate_gcp_template(**args)


# ── family vs literal, and never both ────────────────────────────────────────

def test_a_family_is_emitted_as_a_family():
    t = _tpl(source_image="debian-12")
    assert 'source_image_family = "debian-12"' in t
    assert not re.search(r"^\s*source_image\s*=", t, re.M), \
        "a family must not also be emitted as a literal image name"


def test_a_literal_image_name_is_emitted_as_source_image():
    t = _tpl(source_image_name="vyos-1-4-rolling")
    assert re.search(r'^\s*source_image = "vyos-1-4-rolling"$', t, re.M)


def test_a_literal_image_name_wins_over_a_family():
    """Both can arrive — an untouched family default plus a name. Packer would
    take the literal and ignore the family, so the template must not carry a
    family the build did not use."""
    t = _tpl(source_image="debian-12", source_image_name="vyos-1-4-rolling")
    assert "source_image_family" not in t, "the unused family must not be emitted"
    assert '"vyos-1-4-rolling"' in t and '"debian-12"' not in t


# ── the image project is opt-in, and a list ──────────────────────────────────

def test_no_image_project_is_emitted_by_default():
    """Unset, the builder searches the build project and then the standard
    public image projects — which is what makes a bare `debian-12` resolve.
    Emitting this blind would narrow the search and break every stock build."""
    assert "source_image_project_id" not in _tpl()
    assert "source_image_project_id" not in _tpl(source_image_name="vyos-1-4-rolling")


def test_the_image_project_is_a_list_when_set():
    t = _tpl(source_image_name="vyos-1-4-rolling", source_image_project="shared-images")
    assert 'source_image_project_id = ["shared-images"]' in t, \
        "the plugin's field is a list of projects, not a string"


# ── the runner carries both the whole way ────────────────────────────────────

def test_the_runner_passes_both_source_fields_to_the_generator():
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def _run_gcp_build\(.*?\n(.*?)\nasync def ", src, re.S).group(1)
    assert "source_image_name=req.source_image_name" in body
    assert "source_image_project=req.source_image_project" in body


def test_the_job_log_names_the_source_it_used():
    """The failed bake's only record of which image it built from was Packer's
    own 'Using image:' line. The template step names it up front."""
    src = _read("web_dashboard", "services", "packer_build_service.py")
    body = re.search(r"async def _run_gcp_build\(.*?\n(.*?)\nasync def ", src, re.S).group(1)
    assert "req.source_image_name" in body and "src_desc" in body


def test_the_api_reports_whichever_source_was_used():
    api = _read("web_dashboard", "api", "packer.py")
    body = re.search(r"def build_gcp_image\(.*?\n(.*?)\n# ── OCI build", api, re.S).group(1)
    assert "source = req.source_image_name or req.source_image" in body
    # The audit row and the queued message must not report a family the build
    # did not use.
    assert 'details={"image_name": req.image_name, "source_image": source}' in body


# ── the request model ────────────────────────────────────────────────────────

def _req(**kw):
    args = dict(image_name="vyos-cell")
    args.update(kw)
    return pm.GCPPackerBuildRequest(**args)


def test_a_source_is_still_required():
    try:
        _req()
    except Exception:
        return
    raise AssertionError("a build with neither a family nor an image name must be rejected")


def test_either_source_alone_is_accepted():
    assert _req(source_image="debian-12").source_image == "debian-12"
    assert _req(source_image_name="vyos-1-4-rolling").source_image_name == "vyos-1-4-rolling"


def test_the_source_fields_are_charset_pinned():
    """Both are inlined into the generated HCL, so a quote or a newline in
    either would break out of the string it lands in."""
    for bad in ['vyos"; shell_command = "x', "vyos\nimage_name = evil", "Vyos_Rolling"]:
        for field in ("source_image", "source_image_name"):
            try:
                _req(**{field: bad})
            except Exception:
                continue
            raise AssertionError(f"{field}={bad!r} must be rejected")


def test_the_image_project_is_charset_pinned():
    try:
        _req(source_image_name="vyos-1-4-rolling", source_image_project='p"]\n  x = ["y')
    except Exception:
        return
    raise AssertionError("an image project that breaks out of the HCL list must be rejected")


def test_a_domain_scoped_project_is_accepted():
    """Domain-scoped project ids carry a colon and dots — rejecting them would
    lock out every org that has one."""
    r = _req(source_image_name="vyos-1-4-rolling", source_image_project="example.com:my-project")
    assert r.source_image_project == "example.com:my-project"


# ── the form sends one source, not two ───────────────────────────────────────

def test_the_form_blanks_the_source_the_mode_did_not_pick():
    """The family input keeps its debian-12 default while the custom input is
    shown. Sending both would let a stale value decide the build — the literal
    wins server-side — which is the same class of failure this change fixes."""
    html = _read("web_dashboard", "templates", "gcp", "index.html")
    body = re.search(r"async submitGcpPackerBuild\(\) \{(.*?)\n    \},", html, re.S).group(1)
    assert "source_image:         custom ? '' : this.gcpPackerForm.source_image" in body
    assert "source_image_name:    custom ? this.gcpPackerForm.source_image_name : ''" in body


def test_the_form_warns_before_baking_a_network_cell_from_a_family():
    """The one mistake that costs a whole build: the vyos-cell provisioner
    loaded against the untouched debian-12 default."""
    html = _read("web_dashboard", "templates", "gcp", "index.html")
    assert "/vyos/i.test(gcpPackerForm.provisioner_script)" in html


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
