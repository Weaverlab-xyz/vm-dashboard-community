"""The dashboard's Workstation tile — one on-prem VM band, and the source it counts.

VMware Workstation is a hypervisor connection like the other five (`kind == "workstation"`
in `HypervisorConnection`, and the first entry in the on-prem group in `_nav_links.html`),
but on the dashboard it used to get a two-tile "On-Premises" band of its own — Total VMs
and Running — sitting directly above a Hypervisors band whose tiles count the same kind of
thing for Proxmox/vSphere/Hyper-V/Nutanix/XCP-ng. Two bands, one idea.

Three things are pinned here:

  * **there is exactly one on-prem VM band.** No second section may carry a `/vms` tile.
    Stated as a rule over every section rather than a check on the old ids, so the band
    can't reappear under a new name.
  * **the tile counts `/api/vms`, not `/api/vms/dashboard-stats`.** The stats endpoint
    reads `VMStateCache` alone, and that table is written only by the two PowerShell
    paths: on a host with no local PowerShell it 500s, and even served it could never
    count the Workstation rows a remote agent syncs. `/api/vms` merges both sources —
    it is what the page behind the tile renders.
  * **the workgroup badges still get their counts.** `workgroup_counts` came from that
    same stats call, so deleting the call is what would silently strand the badges on
    "…" — a break in markup nowhere near the diff that caused it.

Templates are read as text — Alpine expressions are the subject here — so no DOM needed.

Run: python tests/test_dashboard_workstation_tile.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")


def _read(*parts):
    with open(os.path.join(_TEMPLATES, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("dashboard.html")

# One tile literal: { key: '...', title: '...', href: '...', flag: '...', ... }
TILE = re.compile(r"\{\s*key:\s*'(?P<key>[a-z_]+)'[^}]*\}")


def _tiles():
    """Every tile literal in dashboard.html, as {key: raw source}."""
    return {m.group("key"): m.group(0) for m in TILE.finditer(DASHBOARD)}


def _field(tile_src, name):
    m = re.search(rf"{name}:\s*'([^']*)'", tile_src)
    return m.group(1) if m else None


with open(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"),
          encoding="utf-8") as _f:
    DASHBOARD_API = _f.read()


def _fetcher_body():
    """The body of api/dashboard.py's `_workstation` builder.

    The tile moved server-side: the page reads every tile it can from
    GET /api/dashboard/stats, so the Workstation count and the per-workgroup badge counts
    are built here, from one query, instead of by the client looping the full /api/vms
    payload. What this file guards is unchanged — that the count comes from the MERGED
    rows and not from /api/vms/dashboard-stats, which counts a PowerShell-only table.
    """
    marker = "    def _workstation():"
    assert marker in DASHBOARD_API, (
        "api/dashboard.py has no _workstation() builder — the Workstation tile would read "
        "'unavailable' and the workgroup badges would have no counts")
    body = DASHBOARD_API[DASHBOARD_API.index(marker):]
    return body[:body.index('    _safe("workstation_vms"')]


def _sections():
    """{section id: raw source of its tile list}. A section literal runs from its own
    `id:` to the next one's, so a tile matched in that slice belongs to it."""
    ids = [(m.group(1), m.start()) for m in re.finditer(r"id: '([a-z]+)',", DASHBOARD)]
    assert ids, "the tileSections catalog moved or changed shape"
    out = {}
    for i, (sid, start) in enumerate(ids):
        end = ids[i + 1][1] if i + 1 < len(ids) else len(DASHBOARD)
        out[sid] = DASHBOARD[start:end]
    return out


def test_the_workstation_tile_lives_in_the_hypervisors_section():
    """Workstation is a hypervisor connection kind and the first on-prem entry in the
    nav; the dashboard should say the same thing."""
    tiles = _tiles()
    assert "workstation_vms" in tiles, (
        "the dashboard lost its Workstation tile — /vms has no count on the home page")

    section = _sections()["hypervisors"]
    assert "key: 'workstation_vms'" in section, (
        "the Workstation tile is no longer under the Hypervisors section")

    tile = tiles["workstation_vms"]
    assert _field(tile, "href") == "/vms", "the tile must link to the Workstation page"
    # Same flag as the nav link, the page route and the API — the tile can't offer a
    # surface the rest of the app has switched off.
    assert _field(tile, "flag") == "vmware", (
        "the tile must gate on the same flag as the /vms route and its nav link")
    assert _field(tile, "secondaryLabel") == "running", (
        "one tile carries total + running, like every other hypervisor tile; splitting "
        "them back into two tiles is what made this a section of its own")


def test_only_one_section_counts_on_prem_vms():
    """The rule behind the consolidation: a second band of /vms tiles reads as a second
    inventory. Written over every section so it can't come back under a new id."""
    for sid, src in _sections().items():
        if sid == "hypervisors":
            continue
        for m in TILE.finditer(src):
            href = _field(m.group(0), "href") or ""
            assert href != "/vms", (
                f"the {m.group('key')} tile puts a /vms count in the '{sid}' section as "
                f"well — the Hypervisors section already has one")


def test_the_tile_counts_the_merged_list_not_the_powershell_only_stats():
    """`/api/vms` merges the local VMX scan with the rows an agent syncs;
    /api/vms/dashboard-stats counts `VMStateCache`, which only PowerShell writes."""
    assert '_safe("workstation_vms"' in DASHBOARD_API, (
        "workstation_vms is not registered with _safe(), so it never reaches the response "
        "and the tile reads 'unavailable'")

    body = _fetcher_body()
    assert "_agent_workstation_vms" in body, (
        "the tile must count the MERGED rows — api/vms.py::_agent_workstation_vms is the "
        "same source the /vms page lists from")
    assert "dashboard-stats" not in body and "dashboard_stats(" not in body, (
        "/api/vms/dashboard-stats counts VMStateCache alone — a table written only by the "
        "two PowerShell paths, so on a host with no local PowerShell it 500s and its "
        "total can never include an agent's Workstation VMs")
    assert "is_running" in body, (
        "the running count comes from is_running on the merged rows; the /running "
        "endpoint is PowerShell-only")


def test_the_workgroup_badges_still_get_their_counts():
    """They were fed by `workgroup_counts` from the stats call that this tile replaced."""
    assert re.search(r"wg\.count = ", DASHBOARD), (
        "nothing assigns wg.count any more, so every workgroup badge renders '…' forever")

    assert "by_workgroup" in _fetcher_body(), (
        "the badge counts must be built from the same rows as the tile. They used to come "
        "from the client looping the full /api/vms payload; the server now sends "
        "by_workgroup on the workstation tile, so the badges cannot disagree with the "
        "number above them")
    assert "by_workgroup" in DASHBOARD, (
        "the page no longer reads by_workgroup off the snapshot, so the badges have no "
        "source at all")

    # The premise: the badge block is gated on the same flag as the tile, so the fetcher
    # that fills it runs whenever the block is on screen.
    assert 'x-show="features.vmware && workgroups.length > 0"' in DASHBOARD, (
        "the workgroup block no longer gates on `vmware`, so it can now render while the "
        "Workstation tile — the only thing that counts its badges — is hidden")


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
