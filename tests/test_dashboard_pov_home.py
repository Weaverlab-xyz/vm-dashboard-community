"""The home page on a POV instance leads with the POVs, and pays for it once.

On a POV install every other band of the dashboard either hides itself or answers a
question about an estate that does not exist -- so the landing page was an overview of
nothing, while the one thing an SE needed was a click away on /pov. This band fixes that,
and the ways it could quietly stop working are all invisible:

  * **The gate is the PROFILE, not a persona.** A persona may only ever reorder; the
    profile is the axis that subtracts and already 404s whole pages. A persona key
    appearing in this band would let the page and services/personas disagree, and would
    have a persona hiding something -- the one thing that layer must never do.
  * **One request, three consumers.** The band, the POV tiles' links and the POV rows in
    Needs attention all read one fetch. A second call for any of them is a second answer
    to one question, and this endpoint costs a handful of indexed queries PER POV, on
    every poll, in every open tab.
  * **The POV read is authenticated and silent.** This app sets no auth cookie anywhere,
    so a bare fetch would be an anonymous request. And every way the call can fail is a
    non-event -- an estate instance 404s the whole router, a narrowed user gets 403 --
    none of which is a reason to paint an error over bands that loaded fine.
  * **The auto-delete timer is NOT re-derived here.** inventory_service.collect queries
    PovEnvironment unconditionally, so a POV nearing expiry is already one of the
    inventory-sourced items in that panel. A second would mean two rows per POV with
    separate dismissals, and dismissing one would silently leave the other.
  * **The ladder's states come from the server.** They are rendered on more than one
    surface now, so the colour map lives in one place. A second copy drifts silently the
    day a seventh state is added -- which is the failure the ladder itself exists to stop
    somewhere else.
  * **A settled list is not polled.** A POV lives for weeks. Re-reading it every 20s from
    every open tab is the DB-pool burst this page was rebuilt to remove.
  * **No tile here may link to a page this profile 404s.** That is the shape the profile
    page group was added to fix, and the OT tile carried it in by a third route.

Reads the template, the script and the API module as text, plus two imports. No DOM, no
app, no cloud.

Runs under pytest, or standalone:
    python tests/test_dashboard_pov_home.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-dashboard-pov-home")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


DASHBOARD = _read("web_dashboard", "templates", "dashboard.html")
POV_INDEX = _read("web_dashboard", "templates", "pov", "index.html")
APP_JS = _read("web_dashboard", "static", "js", "app.js")
STATS = _read("web_dashboard", "api", "dashboard.py")

# The band's own marker, and the comment the persona band is located by. Both are
# load-bearing for the slices below.
_BAND_MARK = "<!-- The POVs, on a POV instance."
_PERSONA_BAND_MARK = "<!-- Use cases for the active persona"


def _js(name):
    """A method body lifted out of the template, braces balanced."""
    m = re.search(r"\n[ \t]*(?:async[ \t]+)?" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                  DASHBOARD)
    assert m, "dashboard.html: %s not found" % name
    depth, i = 0, m.end() - 1
    while True:
        if DASHBOARD[i] == "{":
            depth += 1
        elif DASHBOARD[i] == "}":
            depth -= 1
            if depth == 0:
                return DASHBOARD[m.end():i]
        i += 1


def _band():
    """The POV band's markup, from its marker to the next top-level comment."""
    assert _BAND_MARK in DASHBOARD, "the POV band is gone"
    return DASHBOARD.split(_BAND_MARK, 1)[1].split("\n  <!-- ", 1)[0]


def _tilesections():
    """The tile catalog, located the way the collector-parity suite locates it."""
    block = DASHBOARD[DASHBOARD.index("tileSections:"):]
    return block[:block.index("\n      ],")]


# -- the gate is the profile, never a persona --------------------------------

def test_the_band_is_gated_on_the_profile():
    assert '<template x-if="isPov">' in DASHBOARD, (
        'the POV band is not wrapped in x-if="isPov" -- on an estate instance it would '
        "be in the document, merely hidden")


def test_ispov_reads_the_features_map():
    """Not /api/persona, which also carries install_profile. visibleTiles already reads
    this.features, and gating bands off one source and tiles off another is two answers
    to one question waiting to disagree."""
    m = re.search(r"get isPov\(\)\s*\{([^}]*)\}", DASHBOARD)
    assert m, "dashboard.html no longer declares get isPov()"
    assert "this.features.install_profile" in m.group(1), \
        "isPov does not read the features map"


def test_the_band_names_no_persona_key():
    """A persona may only reorder. A persona key inside a band that HIDES things would
    make this the one place the persona layer subtracts."""
    from web_dashboard.services import personas as P
    band = _band()
    for key in P.VALID_PERSONAS:
        assert "'%s'" % key not in band and '"%s"' % key not in band, \
            "the POV band names the persona key %r" % key


def test_the_persona_band_is_suppressed_on_pov_but_still_reachable():
    """Reframed, not removed. On a POV instance /api/persona serves the ESTATE catalog,
    so that band reads "Not available on this instance" card after card -- but the link
    to the catalog has to survive, or the profile would have made it unreachable."""
    assert '<template x-if="!isPov">' in DASHBOARD, \
        "the persona use-case band is not suppressed on a POV instance"
    assert 'href="/use-cases"' in DASHBOARD, (
        "nothing on the page links to the catalog any more -- the POV checklist lives "
        "there, so suppressing the band without it would make the catalog unreachable")


def test_the_estate_instance_still_gets_the_persona_band_unchanged():
    """The wrapper must not disturb what the guard suite for that band measures: it
    splits on the band's marker comment and wants the persona gate close behind it. That
    failure reports a missing gate, which is not what would have happened."""
    band = DASHBOARD.split(_PERSONA_BAND_MARK, 1)
    assert len(band) == 2, "the persona band's marker comment is gone"
    assert "persona.persona &&" in band[1][:500], (
        "the persona gate is now more than 500 characters past its marker comment -- "
        "the guard suite for that band measures exactly this, and its message will "
        "blame the gate rather than the preamble that pushed it out")


# -- one request, three consumers --------------------------------------------

def _uncommented(src):
    """The script with // line comments stripped, so a guard counting call sites
    is not confused by a comment that names the same endpoint."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def test_the_pov_list_is_fetched_exactly_once():
    assert _uncommented(DASHBOARD).count("/api/pov/managed") == 1, (
        "the POV list is requested from more than one place. The band, the tiles' links "
        "and the attention rows must all read one fetch -- this endpoint costs a handful "
        "of indexed queries per POV, on every poll, in every open tab")


def test_the_attention_rows_make_no_request_of_their_own():
    body = _js("loadAttention")
    assert "/api/pov" not in body, \
        "loadAttention fetches the POVs itself; it must derive them from this.povs"
    assert body.count("API.get(") == 6, (
        "loadAttention now makes %d calls, not the six it composed before -- the POV "
        "rows are meant to be free" % body.count("API.get("))


def test_the_pov_read_is_authenticated():
    """This app authenticates off the Authorization header and sets no cookie anywhere,
    so a bare fetch is an ANONYMOUS request that 401s."""
    body = _js("loadPovs")
    assert "API.get(" in body, "loadPovs does not go through window.API"
    assert "fetch('/api/pov" not in DASHBOARD and 'fetch("/api/pov' not in DASHBOARD


def test_a_failed_pov_read_paints_nothing():
    body = _js("loadPovs")
    assert "catch" in body, (
        "loadPovs has no catch -- an estate instance 404s this router and a narrowed "
        "user gets 403, and neither is a reason to break a page whose other bands loaded")
    assert "this.error" not in body, \
        "loadPovs paints a page-level error on what is a non-event"


def test_a_settled_pov_list_is_not_polled():
    """A POV lives for weeks; the estate surfaces beside it change by the minute."""
    assert "povsBusy()" in DASHBOARD, "there is no busy predicate to gate the poll on"
    init = _js("init")
    m = re.search(r"setInterval\(\(\) => \{(.*?)\}, 20000\)", init, re.S)
    assert m, "the 20s poll moved or changed shape"
    assert "povsBusy()" in m.group(1), (
        "the poll re-reads the POV list unconditionally. That is a handful of indexed "
        "queries per POV every 20 seconds per open tab, for a list that changes weekly")


def test_an_estate_instance_never_calls_the_pov_router():
    """Not an optimisation. The whole POV router 404s on an estate instance, so an
    ungated call puts a guaranteed-failing round trip in front of every page load for the
    profile that has no POVs to show -- and buries a permanent 404 in the network tab of
    the profile most people run."""
    init = _js("init")
    calls = re.findall(r"[^\n]*loadPovs\(\)[^\n]*", init)
    assert calls, "init no longer loads the POVs at all"
    for line in calls:
        assert "isPov" in line, (
            "this call site loads the POV list without checking the profile: %r"
            % line.strip())


def test_the_pov_load_is_not_in_the_first_awaited_pair():
    """loadFeatures + loadPersona are awaited together before the first paint, and a
    guard suite pins that line literally. A third call there fails it for a reason with
    nothing to do with the persona."""
    init = _js("init")
    assert "Promise.all([this.loadFeatures(), this.loadPersona()])" in init, \
        "the first awaited pair changed shape"
    first = init.index("Promise.all([this.loadFeatures()")
    assert "loadPovs" not in init[first:init.index("\n", first)]


# -- the attention rows ------------------------------------------------------

def _attention_rules():
    """Just the POV half of loadAttention."""
    body = _js("loadAttention")
    return body[body.index("const povOut = ["):body.index("// error before warn")]


def _pov_attention_ids():
    body = _js("loadAttention")
    return re.findall(r"id: '(pov[^']*)'", body)


def test_every_pov_attention_id_is_namespaced():
    """Two reasons pointing the same way. One POV can raise several items, and the ids
    are what the per-browser dismissal remembers -- so a shared id would have dismissing
    one silently hide another. And a guard suite finds the fanned-out tile section by
    scanning this whole file for an id property whose value is bare lowercase letters, so
    an attention id of that shape would enter that scan as if it were a section."""
    ids = _pov_attention_ids()
    assert ids, "no POV attention ids found -- the rules are gone or were renamed"
    for i in ids:
        assert ":" in i, (
            "the POV attention id %r carries no colon" % i)
    bare = re.findall(r"id: '(pov[a-z]*)',", _js("loadAttention"))
    assert not bare, (
        "these POV attention ids are bare lowercase and would be scanned as tile-section "
        "ids: %s" % sorted(bare))


def test_the_expiry_timer_is_not_re_derived_for_povs():
    """inventory_service.collect queries PovEnvironment unconditionally, so a POV nearing
    its auto-delete is ALREADY an inventory-sourced item in this panel. A second one
    means two rows per POV with separate dismissals."""
    ids = _pov_attention_ids()
    offenders = [i for i in ids if "expiry" in i or "expires" in i]
    assert not offenders, (
        "the POV rules re-derive the auto-delete timer (%s), which the inventory source "
        "already reports. Two rows per POV, two dismissals, and dismissing one leaves "
        "the other" % sorted(offenders))


def test_every_row_field_the_band_reads_is_one_the_server_sends():
    """The whole failure class this band could die of, silently.

    A misread field name is `undefined` in every expression that touches it: the rule
    never fires, the fact never renders, there is no error anywhere, and the band simply
    looks like a POV with nothing going on. Nothing else in the stack would catch it --
    which is the same shape as a tile pointed at an endpoint nobody wrote.
    """
    import glob
    srcs = [os.path.join(_ROOT, "web_dashboard", "api", "pov.py")]
    srcs += glob.glob(os.path.join(_ROOT, "web_dashboard", "services", "pov_*.py"))
    # The spend and expiry halves of a row are described by services that are not named
    # for POVs, because both are shared with the estate inventory.
    srcs += [os.path.join(_ROOT, "web_dashboard", "services", "spend_policy.py"),
             os.path.join(_ROOT, "web_dashboard", "services", "expiry_policy.py"),
             os.path.join(_ROOT, "web_dashboard", "services", "suspend_schedule.py")]
    blob = "".join(open(s, encoding="utf-8").read() for s in srcs)

    read = set(re.findall(r"\bp\.([a-z_]+)", _band() + _attention_rules()))
    read |= set(re.findall(r"\bp\.(?:setup|use_cases|spend)\.([a-z_]+)",
                           _band() + _attention_rules()))
    read.discard("id")
    missing = sorted(f for f in read if '"%s"' % f not in blob)
    assert not missing, (
        "the POV band reads row fields the serializer never sends: %s. Each one is "
        "undefined at runtime, so its rule never fires and its fact never renders -- "
        "with no error anywhere" % missing)


def test_the_spend_rule_fires_on_the_cap_being_HIT_not_merely_set():
    """services/spend_policy keeps `capped` and `over` apart and says why: one means
    "there is a limit", the other means "it was hit". An alarm keyed on the first would
    fire for every POV that merely HAS a cap -- so the panel would be loudest about the
    POVs being managed best, and an SE would learn to ignore it."""
    rules = _attention_rules()
    spend = rules[rules.index("pov:spend:") - 400:rules.index("pov:spend:") + 400]
    assert "p.spend.over" in spend, \
        "the spend alarm does not read `over`, so it cannot tell a hit cap from a set one"
    assert "severity: 'error'" in spend and "severity: 'warn'" in spend, (
        "the spend rule has one severity. Approaching a cap and being over it are "
        "different days")


def test_the_pov_items_roll_up_rather_than_filling_the_panel():
    """The panel caps at eight. One badly-behaved POV can raise five items on its own,
    which would bury every failed job on the instance."""
    body = _js("loadAttention")
    assert "pov:rollup" in body, "the POV items do not roll up"


def test_the_blocked_and_unknown_os_rules_do_not_double_report():
    """A POV whose guests ALL report no OS is `blocked` on the ladder, so the blocked
    rule already names it. The separate rule exists for the PARTIAL case, where the
    ladder says `ready` and the wire-up will quietly skip exactly those guests."""
    body = _js("loadAttention")
    assert "next_blocked" in body, "the blocked rule is gone"
    between = body[body.index("p.setup.next_blocked"):body.index("pov:osunknown")]
    assert "} else if" in between, (
        "the unknown-OS rule is not an else-branch of the blocked rule, so a POV with no "
        "known guest OS raises both items for one fact")


# -- the ladder is rendered from one map -------------------------------------

def test_the_ladder_colour_map_lives_in_one_place():
    assert "function povStepClass(" in APP_JS and "function povStepMark(" in APP_JS, \
        "the ladder helpers are not in static/js/app.js"
    for page, name in ((DASHBOARD, "dashboard.html"), (POV_INDEX, "pov/index.html")):
        assert "povStepClass" in page and "povStepMark" in page, \
            "%s does not use the shared ladder helpers" % name
        assert "stepClass(s) {" not in page, \
            "%s still carries its own copy of the ladder colour map" % name


def test_every_rendered_step_state_is_a_real_one():
    """The states are produced server-side. A colour branch matching nothing renders grey
    forever and says nothing, on a band whose whole job is to say what to do next."""
    from web_dashboard.services import pov_setup_steps as S
    known = {S.DONE, S.RUNNING, S.READY, S.BLOCKED, S.SKIPPED, S.CONFIGURED}
    m = re.search(r"function povStepClass\(s\) \{(.*?)\n\}", APP_JS, re.S)
    assert m, "povStepClass moved or changed shape"
    branched = set(re.findall(r"^\s{8}([a-z_]+):", m.group(1), re.M))
    assert branched, "povStepClass has no state branches"
    assert branched <= known, (
        "povStepClass branches on states the service does not produce: %s"
        % sorted(branched - known))
    assert known <= branched, (
        "povStepClass has no branch for %s, so it renders as the unknown-state grey -- "
        "which is the same as `skipped` and means something else entirely"
        % sorted(known - branched))


def test_skipped_is_grey_and_never_a_warning():
    """A PRA-only POV is finished without a Resource Broker. Painting that amber is how a
    correctly scoped evaluation reads as half broken."""
    m = re.search(r"skipped: '([^']*)'", APP_JS)
    assert m, "povStepClass has no skipped branch"
    assert "gray" in m.group(1) and "amber" not in m.group(1) and "red" not in m.group(1)


# -- no tile may link to a page this profile 404s ----------------------------

def test_the_ot_tile_asks_whether_this_instance_serves_a_cloud_console():
    """Every href that tile can produce is a cloud console, and a POV instance 404s all
    four. Without this the tile shows an honest count whose only link is dead."""
    block = _tilesections()
    m = re.search(r"\{[^{}]*key: 'ot_cells'.*?\}", block, re.S)
    assert m, "the OT tile moved or changed shape"
    assert "cloud_pages" in m.group(0), (
        "the OT tile does not gate on the profile page group, so on a POV instance it "
        "renders a count whose only link 404s")


def test_the_page_group_reaches_the_browser():
    """The gate above is only real if the key is actually served. A gate on a key the
    features map does not carry is permanently false -- the same silent shape as a
    misspelled flag, and it fails OPEN here rather than closed."""
    from web_dashboard.services import feature_flags
    assert "cloud_pages" in feature_flags.feature_map(), (
        "cloud_pages is not in the map /api/features serves, so the OT tile's gate on it "
        "is permanently false and hides the tile on every instance")


def test_the_server_half_of_the_ot_gate_exists():
    """The client gate hides the tile; this stops the endpoint computing and shipping a
    number nobody can follow. Same reader as the nav link and the route."""
    m = re.search(r"def _ot_cells\(\):(.*?)_safe\(\"ot_cells\"", STATS, re.S)
    assert m, "_ot_cells moved or changed shape"
    assert 'profile_page_allowed("cloud_pages")' in m.group(1), (
        "_ot_cells does not resolve the profile page group, so on a POV instance it "
        "computes a count whose only link 404s")


# -- the tile catalog --------------------------------------------------------

def test_the_pov_tiles_are_declared_and_gated():
    block = _tilesections()
    for key in ("pov_active", "pov_guests", "pov_coverage"):
        m = re.search(r"\{\s*key: '%s'.*?\}" % key, block, re.S)
        assert m, "the %s tile is not declared in the catalog" % key
        assert "flag: 'pov_environments'" in m.group(0), (
            "the %s tile is not gated, so it would render on an estate instance where "
            "its endpoint 404s" % key)


def test_the_pov_tiles_are_answered_by_the_aggregate():
    """A tile with no source renders unavailable forever, and that is a designed state
    indistinguishable from a throttled API."""
    for key in ("pov_active", "pov_guests", "pov_coverage"):
        assert '"%s"' % key in STATS, \
            "api/dashboard.py does not answer the %s tile" % key


def test_the_pov_tiles_are_scoped_the_way_the_pov_router_scopes():
    """`pov:read` is the feature-area permission and pov_env_scope is the per-instance
    grant. Anything looser shows a narrowed SE a COUNT of POVs they cannot open."""
    m = re.search(r"def _pov_tiles\(.*?\n\n\n", STATS, re.S)
    assert m, "_pov_tiles moved or changed shape"
    body = m.group(0)
    assert "pov_env_scope" in body, "_pov_tiles ignores the per-instance grant"
    assert 'has_permission(user, "pov", "read")' in body, \
        "_pov_tiles ignores the pov:read permission its router carries"
    assert "_forbidden()" in body, \
        "_pov_tiles raises rather than degrading -- one permission must not blank a page"


def test_a_pov_tile_never_reports_zero_for_an_instance_that_has_no_answer():
    """Zero is a plausible number and renders as one. The sentinel exists so that an
    instance which does not run POVs is not reported as one running none."""
    m = re.search(r"def _pov_tiles\(.*?\n\n\n", STATS, re.S)
    body = m.group(0)
    gate = body[:body.index("if not has_permission")]
    # Comments stripped, because the code there explains itself by naming the call it
    # does NOT make -- and a guard that reads the explanation as the thing it forbids is
    # the same self-trip the tile catalog carries a note about.
    gate = "\n".join(line.split("#")[0] for line in gate.splitlines())
    assert "_unavailable(" in gate and "_tile(0)" not in gate, (
        "_pov_tiles reports 0 when POV environments are disabled, which is "
        "indistinguishable from a POV instance with none")


def test_no_tile_key_collides_with_the_persona_band_guard():
    """A guard suite asserts that the string naming the persona card band never appears
    inside the tile catalog -- comments included -- because a card scanned as a tile
    fails the collector-parity suite with a message about a missing collector. That
    string is DESCRIBED here rather than quoted, for the same reason."""
    banned = "use" + "_" + "cases"
    assert banned not in _tilesections(), (
        "the tile catalog contains the string that names the persona card band. The POV "
        "coverage tile is keyed pov_coverage precisely to avoid this, and a comment "
        "mentioning it counts too")


def test_the_quick_deploy_catalog_is_untouched():
    """The POV call to action lives in the band's empty state. A shortcut added to the
    quick-deploy catalog fails a guard suite that pins it byte for byte -- and the
    message there is a tuple diff that says nothing about where the CTA belongs."""
    block = DASHBOARD[DASHBOARD.index("quickDeployCatalog:"):]
    block = block[:block.index("\n      ],")]
    assert block.count("id:") == 8, (
        "the quick-deploy catalog is no longer the eight shortcuts a guard suite pins "
        "literally. The POV call to action belongs in the POV band's empty state")


# -- the band says what to do, and sends you where it is done ----------------

def test_the_band_renders_the_next_step_sentence():
    """The whole reason the ladder exists. A band showing eight chips and no sentence is
    the state the ladder was written to replace."""
    band = _band()
    assert "next_label" in band, "the band does not name the next step"
    assert "next_detail" in band, "the band does not say why a step is blocked"
    assert "next_blocked" in band, "the band does not distinguish blocked from ready"


def test_the_band_offers_links_and_not_action_buttons():
    """The dashboard holds a row and a step name; the POV page holds the handlers, the
    refusals and the confirmations. A button here would either send blind or need its own
    copy of that decision -- the reasoning the workgroup card on this page already
    records for the same choice."""
    band = _band()
    assert "<button" not in band, (
        "the POV band grew a button. Every POV action is refused out of turn and several "
        "are destructive; the page that owns those handlers owns the control")
    assert "povNextHref(" in band, "the next-step line is not a link into the POV"


def test_the_band_judges_wiring_by_the_artifact():
    """A wire-up whose guests all report no OS skips every one of them and completes
    GREEN. So the band counts wired guests, never a job's status."""
    band = _band()
    assert "wired_count" in band, "the band does not report how many guests are wired"
    assert "provision_job_id" not in band and "job.status" not in band


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print("ok   %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, e))
    print("\n%d/%d passed" % (len(fns) - failures, len(fns)))
    sys.exit(1 if failures else 0)
