"""Each dashboard tile must count what its endpoint really returns, at the scope it
really returns it.

Three tiles families were counting a contract the server does not offer. All three
rendered a *plausible number* rather than the `–` + "unavailable" a broken tile shows,
which is why none of them was ever reported.

1. **Bare list vs envelope — five hypervisor tiles read 0, always.**
   `_fetchListCount(url, listKey, pred)` was written for the `{<listKey>: [...], count: N}`
   envelope the cloud and container endpoints return. Every hypervisor listing answers
   with a **bare JSON array** instead: `api/hyperv.py`, `api/vsphere.py`, `api/xcpng.py`
   and `api/nutanix.py` all end in `return out`, and `api/proxmox.py` returns its resource
   list. `API.get` hands back `resp.json()` unwrapped (static/js/app.js), so reading an
   envelope key off an Array gives `undefined`:

       const list = r['vms'] || [];        // []      — an Array has no 'vms'
       { value: r.count ?? list.length }   // undefined ?? 0  ->  0

   The fix belongs in the fetcher, not the endpoints: all five detail pages consume the
   bare array (`(data || []).map(...)` on /hyperv and /xcpng, `this.allVms = vms` on
   /vsphere and /nutanix, `resources.filter(...)` on /proxmox — that last one would throw
   outright on an envelope).

2. **A silently-ignored query parameter — the Proxmox tile counted containers as VMs.**
   The tile fetched `/api/proxmox/resources?type=vm`, but `get_resources` declares only
   `connection_id` and `node`. FastAPI drops unknown query params without complaint, so
   the "VMs" count included LXC containers. Proxmox rows carry the discriminator
   themselves (`type` is `"qemu"` or `"lxc"`), so the narrowing belongs in the predicate.

3. **A phantom scope parameter — the Portainer tile multiplied by workgroup count.**
   `_fetchPortainer` issued one request per workgroup with `?workgroup=<name>` and
   **summed** the results. `list_endpoints` has no `workgroup` parameter and
   `api/containers.py` has no per-row workgroup filtering anywhere — its only gate is
   `require_permission("containers", "read")`. So every request returned the same full
   list: a user in three workgroups saw 3x the real endpoint count, and the home page
   spent three requests to get it wrong.

Every assertion below is written from BOTH sides where it can be, so whichever half
drifts first is what the failure names. Templates and routers are read as text/AST — no
DOM, no app import, no node. CI runs `tests/test_*.py` only (see
.github/workflows/tests.yml), so a `.js` guard here would never execute.

Run: python tests/test_dashboard_tile_contracts.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("web_dashboard", "templates", "dashboard.html")
CONTAINERS_API = _read("web_dashboard", "api", "containers.py")

# (api module, route path, the envelope key the tile asks _fetchListCount for)
BARE_LIST_ENDPOINTS = [
    ("hyperv.py",  "/vms",       "vms"),
    ("vsphere.py", "/vms",       "vms"),
    ("xcpng.py",   "/vms",       "vms"),
    ("nutanix.py", "/vms",       "vms"),
    ("proxmox.py", "/resources", "resources"),
]

# The provider pages that consume those bare arrays directly. Listed so the "fix the
# client, not the endpoint" decision is re-checkable rather than remembered.
BARE_LIST_CONSUMERS = [
    ("hyperv",  "index.html", "/api/hyperv/vms"),
    ("xcpng",   "index.html", "/api/xcpng/vms"),
    ("vsphere", "index.html", "/api/vsphere/vms"),
    ("nutanix", "index.html", "/api/nutanix/vms"),
    ("proxmox", "index.html", "/api/proxmox/resources"),
]


def _route_decorator(module, path):
    """The (FunctionDef, decorator Call) for @router.get(<path>) in web_dashboard/api/."""
    tree = ast.parse(_read("web_dashboard", "api", module))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute) and dec.func.attr == "get"
                    and dec.args and isinstance(dec.args[0], ast.Constant)
                    and dec.args[0].value == path):
                return node, dec
    raise AssertionError(f"{module}: no @router.get({path!r}) found")


def _js_method(name):
    """A method body lifted straight out of dashboard.html, so the real code is the
    subject rather than a paraphrase of it. Same approach as tests/*_check.js's extract()."""
    m = re.search(r"\n[ \t]*(?:async[ \t]+)?" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                  DASHBOARD)
    assert m, f"dashboard.html: definition of {name} not found"
    open_brace = DASHBOARD.index("{", m.start())
    depth, end = 0, -1
    for j in range(open_brace, len(DASHBOARD)):
        if DASHBOARD[j] == "{":
            depth += 1
        elif DASHBOARD[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    assert end != -1, f"dashboard.html: unbalanced braces in {name}"
    return DASHBOARD[m.start():end + 1]


def _code_only(js):
    """``js`` with ``//`` line comments and ``/* */`` blocks removed.

    An "X is absent" assertion over raw source also matches the comment explaining why X
    was removed, so a correct fix fails its own test. The cost-cache tests were bitten by
    exactly this. Strip the prose, then assert.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in js.splitlines())


def _tile_branch(key):
    """The `if (k === '<key>') return this.X(...)` line out of _fetchTile."""
    body = _js_method("_fetchTile")
    m = re.search(r"k === '" + re.escape(key) + r"'\s*\)\s*return\s+this\.(\w+)\(",
                  body)
    assert m, f"no dispatch branch for tile {key!r} in _fetchTile"
    # Everything from the call's open paren to the terminating semicolon.
    start = m.end()
    end = body.index(";", start)
    return m.group(1), body[start:end]


# ── 1. bare list vs envelope ──────────────────────────────────────────────────

def test_hypervisor_endpoints_still_return_a_bare_list():
    # The premise the client fix rests on. Asserted so that an endpoint growing an
    # envelope shows up here, rather than silently turning the Array branch below into
    # dead code nobody dares delete.
    for module, path, list_key in BARE_LIST_ENDPOINTS:
        fn, dec = _route_decorator(module, path)

        assert not any(kw.arg == "response_model" for kw in dec.keywords), (
            f"{module} {path} now declares a response_model. If it grew a "
            f"{{{list_key}, count}} envelope, update this file and re-check whether "
            "_fetchListCount still needs its bare-Array branch")

        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
        assert returns, f"{module} {path}: no return statement found"
        for r in returns:
            if isinstance(r.value, ast.Dict):
                keys = [k.value for k in r.value.keys if isinstance(k, ast.Constant)]
                assert list_key not in keys, (
                    f"{module} {path} now returns a {{{list_key}: ...}} envelope — see "
                    "the response_model note above")


def test_provider_pages_consume_the_bare_list_directly():
    # Why the fix went in the client: migrating these five endpoints to an envelope
    # means migrating five pages too, and /proxmox calls .filter() straight on the
    # response, which would throw rather than degrade.
    for folder, page, url in BARE_LIST_CONSUMERS:
        src = _read("web_dashboard", "templates", folder, page)
        assert url in src, f"{folder}/{page} no longer fetches {url}"
        assert not re.search(re.escape(url) + r"'\s*\)\s*\)?\s*\.\s*(vms|resources)\b", src), (
            f"{folder}/{page} now unwraps an envelope key from {url}; if the endpoint "
            "moved to an envelope, this file's premise is stale")


def test_fetch_list_count_reads_a_bare_list_as_well_as_an_envelope():
    body = _js_method("_fetchListCount")

    assert "Array.isArray" in body, (
        "_fetchListCount understands only the {listKey, count} envelope. Every "
        "hypervisor endpoint returns a bare array, so r[listKey] is undefined and "
        "`r.count ?? list.length` collapses to 0 — all five hypervisor tiles report 0 "
        "VMs, and 0 renders as a real number rather than 'unavailable'")

    assert "r[listKey]" in body, (
        "the envelope branch is gone — every cloud, container and managed-service tile "
        "passes a listKey and would now count nothing")

    assert "r.count" in body, (
        "a server-sent count is the only correct total for a paged endpoint")


# ── 2. the ignored query parameter ────────────────────────────────────────────

def test_proxmox_tile_does_not_pass_a_parameter_the_endpoint_ignores():
    _, args = _tile_branch("proxmox_vms")

    assert "type=" not in args, (
        "the Proxmox tile passes ?type= but get_resources (api/proxmox.py) declares only "
        "connection_id and node. FastAPI drops unknown query params silently, so the "
        "tile counted LXC containers as VMs")

    # Confirm from the router side that no such parameter appeared in the meantime.
    fn, _ = _route_decorator("proxmox.py", "/resources")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "type" not in params, (
        "api/proxmox.py /resources now accepts `type` — the tile may pass it again, and "
        "this assertion should move to checking that it does")

    # …and that the narrowing happens where it can actually take effect.
    assert "qemu" in args, (
        "with ?type=vm gone, the tile must narrow on the row's own discriminator "
        "(proxmox_service sets type = 'qemu' | 'lxc') or it counts containers as VMs")


def test_a_narrowed_tile_stops_trusting_the_server_count():
    body = _js_method("_fetchListCount")
    assert "totalPredicate" in body, (
        "the Proxmox tile counts a SUBSET of the response, so _fetchListCount needs a "
        "total-narrowing predicate")
    # `r.count` describes the whole collection; using it for a subset silently restores
    # the very overcount the type filter removes.
    # Anchor on the assignment, not the first `value:` in the function — that one is the
    # `return { value: -1 }` unavailable guard, which would match and prove nothing.
    m = re.search(r"const out\s*=\s*\{\s*value:\s*([^\n]+)", body)
    assert m and "narrowed" in m.group(1), (
        "value still reads a server-sent count unconditionally. For a narrowed tile "
        "r.count describes the WHOLE collection, so the LXC rows come straight back")


# ── 3. the phantom scope parameter ────────────────────────────────────────────

def test_portainer_tile_makes_one_call_and_does_not_sum_workgroups():
    """The tile moved into the collector, and must still cost exactly one call.

    It originally lived in the template as `_fetchPortainer`, issuing one request per
    workgroup with `?workgroup=<name>` and SUMMING them — but list_endpoints has no
    workgroup parameter, so every request returned the same list and a user in three
    workgroups saw 3x the count. It is now collected server-side; the overcount must not
    come back with it.
    """
    src = _read("web_dashboard", "services", "dashboard_collect.py")
    body = src[src.index("async def _portainer_endpoints():"):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
    # Comments stripped: the code's own note about the old per-workgroup fan-out contains
    # the word, so an absence check over raw source fails the very fix it describes.
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())

    assert "workgroup" not in code, (
        "the Portainer collector scopes by workgroup, but list_endpoints "
        "(api/containers.py) has no workgroup parameter — every call returns the same full "
        "list, so summing them reports endpoints x workgroups")
    assert "for " not in code, (
        "the collector loops. With one unscoped endpoint there is nothing to iterate, and "
        "any loop over calls is an overcount")
    assert code.count("list_endpoints(") == 1, (
        "the Portainer tile should cost exactly one call; it previously cost one per "
        "workgroup for a list that is not workgroup-scoped")


def test_containers_api_really_has_no_workgroup_scoping():
    # The premise. If containers.py ever grows per-row workgroup filtering, the tile
    # needs revisiting and this test is where that conversation starts.
    assert "workgroup" not in CONTAINERS_API, (
        "api/containers.py now mentions workgroup. If /endpoints became workgroup-scoped, "
        "_fetchPortainer may need to scope again — but it must still not SUM overlapping "
        "responses")

    fn, _ = _route_decorator("containers.py", "/endpoints")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "workgroup" not in params, (
        "/endpoints now accepts workgroup — see the note above")


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
