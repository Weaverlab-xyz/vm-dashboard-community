"""Every user-facing surface the product ships is named somewhere in ``docs/``.

The other docs tests check that what IS written is well-formed: links resolve
(test_docs_anchors, test_docs_relative_links, test_app_docs_links), every page carries its
header and every folder its index (test_docs_conventions). None of them notices what is
NOT written, and that is the direction docs actually rot in. A September audit found:

  * **``/schedules``** -- the Scheduled Changes page, in the nav for every user -- named
    by no doc at all. The feature was documented, thoroughly, under "Change Windows"; an
    operator looking for "scheduling" had no way to know that.
  * ``/desktops`` and ``/workgroups`` likewise unnamed, and seven ``*_enabled`` feature
    flags (every on-prem hypervisor among them) never mentioned, so a Settings toggle had
    no page an operator could search for by its key.
  * Four schedulable request models -- bulk deploys, on-prem bulk power -- missing from
    the "Where you can book one" table in change-windows.md, which still said "only the
    cloud pages" after the hypervisor pages gained the control.

Each rule below turns one of those into a failure that names the missing item and where it
most likely belongs. There is deliberately **no grandfather list**: the audit closed every
gap, so a new entry here is a new undocumented surface, not old debt.

  1. Every HTML page route in ``main.py`` is named in a doc, as a route token --
     ``/schedules`` counts, ``/api/schedules`` and ``docs/schedules.md`` do not.
  2. Every nav link is named in a doc (rule 1 for pages registered outside main.py).
  3. Every feature flag in ``services/feature_flags.py`` is named in a doc.
  4. Every request model that mixes ``ScheduleRequestMixin`` -- i.e. every surface that
     accepts a booking -- has a row, naming its platform, in change-windows.md's
     "Where you can book one" table. ``_SCHEDULABLE`` below maps each one; an unmapped
     class fails, and so does a mapped class that no longer exists.
  5. The hub pages the audit created stay linked from the docs index.

The PR-level companion is ``scripts/ci/docs_gate.sh``, which asks the coarser question
"code changed -- did docs change?". This file asks the precise one, and only for surfaces
it can enumerate.

Filesystem and regex only -- no app import -- so it never skips.

Run: python tests/test_docs_coverage.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOCS = os.path.join(_ROOT, "docs")
_WEB = os.path.join(_ROOT, "web_dashboard")
_MAIN = os.path.join(_WEB, "main.py")
_NAV = os.path.join(_WEB, "templates", "_nav_links.html")
_FLAGS = os.path.join(_WEB, "services", "feature_flags.py")
_CHANGE_WINDOWS = os.path.join(_DOCS, "change-windows.md")
_INDEX = os.path.join(_DOCS, "README.md")

# Page routes that are not a feature an operator reads about. Each needs a reason.
_ROUTE_EXEMPT = {
    "/": "the dashboard home; every page links back to it",
    "/login": "the sign-in form; covered by the SSO integration pages without a route name",
    "/setup": "first-run wizard; ONBOARDING.md walks it by screen, not by URL",
}

# Where an undocumented page or flag most likely belongs -- only used to make the failure
# message actionable. A miss here just falls back to the index.
_HINTS = {
    "hyperv": "docs/integrations/hyperv.md", "proxmox": "docs/integrations/proxmox.md",
    "vsphere": "docs/integrations/vsphere.md", "nutanix": "docs/integrations/nutanix.md",
    "xcpng": "docs/integrations/xcpng.md", "schedule": "docs/scheduling.md",
    "inventory": "docs/inventory.md", "desktop": "docs/virtual-desktops.md",
    "workgroup": "docs/permissions.md", "notification": "docs/notifications.md",
    "ansible": "docs/config-management.md",
}

# (relpath, class) -> (row label in the "Where you can book one" table, word that row must
# contain). The label is matched against the row's first cell by prefix, the word anywhere
# in the row -- so adding Nutanix power means saying "Nutanix" in the power row, not just
# adding a line here.
_CLOUDS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}
_HYPERVISORS = {"proxmox": "Proxmox", "vsphere": "vSphere", "hyperv": "Hyper-V",
                "xcpng": "XCP-ng", "vms": "Workstation"}
_SCHEDULABLE = {}
for _m, _word in _CLOUDS.items():
    _SCHEDULABLE[(f"web_dashboard/api/{_m}.py", "BulkPowerRequest")] = ("Bulk power — cloud", _word)
for _m, _word in _HYPERVISORS.items():
    _SCHEDULABLE[(f"web_dashboard/api/{_m}.py", "BulkPowerRequest")] = ("Bulk power — on-premises", _word)
for _cls, _word in (("DeployRequest", "AWS"), ("AzureDeployRequest", "Azure"),
                    ("GCPDeployRequest", "GCP"), ("OCIDeployRequest", "OCI")):
    _SCHEDULABLE[(f"web_dashboard/models/{_word.lower()}.py", _cls)] = ("Cloud VM deploys", _word)
for _cls, _word in (("BulkDeployRequest", "AWS"), ("AzureBulkDeployRequest", "Azure"),
                    ("GCPBulkDeployRequest", "GCP"), ("OCIBulkDeployRequest", "OCI")):
    _SCHEDULABLE[(f"web_dashboard/models/{_word.lower()}.py", _cls)] = ("Bulk cloud VM deploys", _word)
for _word in ("AWS", "Azure", "GCP", "OCI"):
    _SCHEDULABLE[("web_dashboard/models/packer.py", f"{_word}PackerBuildRequest")] = ("Packer image builds", _word)
for _m in ("aws", "azure", "gcp"):
    _SCHEDULABLE[(f"web_dashboard/api/{_m}.py", "ExportImageRequest")] = ("Image export", _CLOUDS[_m])
for _path, _cls, _word in (("web_dashboard/models/aws.py", "CreateImageRequest", "AWS"),
                           ("web_dashboard/models/azure.py", "AzureCreateImageRequest", "Azure"),
                           ("web_dashboard/models/gcp.py", "GCPCreateImageRequest", "GCP"),
                           ("web_dashboard/models/aws.py", "CopyAMIRequest", "AMI copy")):
    _SCHEDULABLE[(_path, _cls)] = ("Image export", _word)

_HUBS = ("scheduling.md", "inventory.md", "change-windows.md")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


_CORPUS = None


def _docs_text():
    """Every .md under docs/, concatenated. Cached: several tests scan it."""
    global _CORPUS
    if _CORPUS is None:
        parts = []
        for dp, _, files in os.walk(_DOCS):
            for f in sorted(files):
                if f.endswith(".md"):
                    parts.append(_read(os.path.join(dp, f)))
        _CORPUS = "\n".join(parts)
    return _CORPUS


def _route_named(route, text):
    # Not preceded by a word char, '/' or '.' (so /api/schedules and docs/schedules.md
    # don't count) and not followed by a word char or '-' (so /schedules-old doesn't).
    return re.search(r"(?<![\w/.])" + re.escape(route) + r"(?![\w-])", text) is not None


def _hint(name):
    for key, page in _HINTS.items():
        if key in name:
            return page
    return "the page describing it, and docs/README.md if it is a new area"


def _page_routes():
    src = _read(_MAIN)
    routes = re.findall(r'@app\.get\(\s*"(/[^"]*)"[^)]*?response_class=HTMLResponse', src)
    # include_in_schema=False pages that set no response_class (e.g. /cert-lab)
    routes += re.findall(r'@app\.get\(\s*"(/[a-z][^"]*)",\s*include_in_schema=False', src)
    return sorted({r for r in routes
                   if "{" not in r and not r.startswith(("/api/", "/docs", "/openapi"))
                   and r != "/swagger"})


def _nav_routes():
    return sorted(set(re.findall(r'data-nav="[^"]+"[^>]*?href="(/[^"#?]*)"', _read(_NAV))))


def _feature_flags():
    return sorted(set(re.findall(r'"([a-z0-9_]+_enabled)"', _read(_FLAGS))))


def _schedulable_classes():
    found = set()
    for dp, _, files in os.walk(_WEB):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, _ROOT).replace("\\", "/")
            # Top-level class statements only: models/schedule.py shows one in a docstring.
            for cls in re.findall(r"^class (\w+)\([^)]*\bScheduleRequestMixin\b", _read(p), re.M):
                found.add((rel, cls))
    return found


def _booking_table_rows():
    """Rows of the "Where you can book one" table, as lists of cells."""
    text = _read(_CHANGE_WINDOWS)
    start = text.index("**Where you can book one**")
    rows = []
    for line in text[start:].splitlines()[1:]:
        if not line.strip():
            if rows:
                break
            continue
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and not set(cells[0]) <= set("-: "):
            rows.append(cells)
    return rows[1:]  # drop the header row


# ── tests ────────────────────────────────────────────────────────────────────


def test_route_matcher_is_word_bounded():
    # Pin the matcher itself, so a loosened regex can't turn every rule vacuous.
    assert _route_named("/schedules", "open `/schedules` to see them")
    assert _route_named("/schedules", "the [page](/schedules).")
    assert not _route_named("/schedules", "GET /api/schedules")
    assert not _route_named("/schedules", "see docs/schedules.md")
    assert not _route_named("/jobs", "the /jobs-archive page")


def test_parsers_find_something():
    # A parser that silently matches nothing would pass every test below.
    assert len(_page_routes()) >= 30, _page_routes()
    assert len(_nav_routes()) >= 20, _nav_routes()
    assert len(_feature_flags()) >= 20, _feature_flags()
    assert len(_schedulable_classes()) >= 20, _schedulable_classes()
    assert "/schedules" in _page_routes() and "/inventory" in _nav_routes()


def test_every_page_route_is_documented():
    text = _docs_text()
    missing = [r for r in _page_routes() if r not in _ROUTE_EXEMPT and not _route_named(r, text)]
    assert not missing, "page routes no doc names:\n" + "\n".join(
        f"  {r}  -> add it to {_hint(r)}" for r in missing)


def test_every_nav_link_is_documented():
    text = _docs_text()
    missing = [r for r in _nav_routes() if r not in _ROUTE_EXEMPT and not _route_named(r, text)]
    assert not missing, "nav links no doc names:\n" + "\n".join(
        f"  {r}  -> add it to {_hint(r)}" for r in missing)


def test_route_exemptions_are_still_routes():
    stale = set(_ROUTE_EXEMPT) - set(_page_routes()) - {"/"}
    assert not stale, f"_ROUTE_EXEMPT names routes main.py no longer serves: {sorted(stale)}"


def test_every_feature_flag_is_documented():
    text = _docs_text()
    missing = [f for f in _feature_flags() if not re.search(r"\b" + f + r"\b", text)]
    assert not missing, "feature flags no doc names:\n" + "\n".join(
        f"  {f}  -> name it on {_hint(f)}" for f in missing)


def test_every_schedulable_request_is_mapped():
    found = _schedulable_classes()
    unmapped = sorted(found - set(_SCHEDULABLE))
    assert not unmapped, (
        "request models accept a booking (ScheduleRequestMixin) but are not in "
        "tests/test_docs_coverage.py::_SCHEDULABLE:\n"
        + "\n".join(f"  {p}::{c}" for p, c in unmapped)
        + "\nAdd a row for the surface to the 'Where you can book one' table in "
          "docs/change-windows.md (and docs/scheduling.md's matrix), then map it here.")
    gone = sorted(set(_SCHEDULABLE) - found)
    assert not gone, ("_SCHEDULABLE maps classes that no longer take a booking -- drop them "
                      "and check the docs table still tells the truth:\n"
                      + "\n".join(f"  {p}::{c}" for p, c in gone))


def test_every_schedulable_surface_has_a_booking_row():
    rows = _booking_table_rows()
    assert rows, "could not find the 'Where you can book one' table in change-windows.md"
    problems = []
    for (path, cls), (label, word) in sorted(_SCHEDULABLE.items()):
        matches = [r for r in rows if r[0].startswith(label)]
        if not matches:
            problems.append(f"  no row starting '{label}' (for {path}::{cls})")
        elif not any(word in " | ".join(r) for r in matches):
            problems.append(f"  row '{label}' does not name {word} (for {path}::{cls})")
    assert not problems, ("docs/change-windows.md 'Where you can book one' is behind the code:\n"
                          + "\n".join(problems))


def test_hub_pages_exist_and_are_indexed():
    index = _read(_INDEX)
    for hub in _HUBS:
        assert os.path.isfile(os.path.join(_DOCS, hub)), f"docs/{hub} is missing"
        assert f"]({hub})" in index, f"docs/README.md does not link {hub}"


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
