"""Unit tests for services/workload_credentials_service.py.

Everything exercised here is pure — path building, response parsing, expiry
maths, request-body shaping — so the module loads by file path with no package
import chain and no network. The HTTP layer itself is deliberately untested:
it is a thin httpx wrapper, and the parts worth guarding are the shapes it
hands to and receives from Workload Credentials.

Two of these tests exist because the published documentation contradicted
itself, so tolerance is a requirement rather than defensiveness:
`parse_generated` must accept camelCase and PascalCase, and must find `leaseId`
in either the `secret` object or the response root.

Runs under pytest, or standalone:  python tests/test_workload_credentials.py
"""
import importlib.util
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services",
                     "workload_credentials_service.py")

_spec = importlib.util.spec_from_file_location("workload_credentials_service", _PATH)
wlc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wlc)

NOW = datetime(2026, 8, 20, 12, 0, 0)


def _read(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


def _assign_source(src, name):
    """The source text of the module-level assignment to `name`.

    Via the AST rather than string splitting: the bare name also appears in
    docstrings, and a nested ``frozenset({...})`` means the first ``}`` is not
    the end of the literal.
    """
    import ast
    tree = ast.parse(src)
    for node in tree.body:
        targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"no module-level assignment to {name}")


# ── Path building (mirrors the Terraform provider's BuildPath) ───────────────

def test_secrets_path_without_a_version_has_no_extra_segment():
    assert wlc.build_secrets_path("SITE", "/static") == "/site/SITE/secrets/static"


def test_secrets_path_inserts_the_path_version_when_set():
    assert (wlc.build_secrets_path("SITE", "/static", "v1")
            == "/site/SITE/secrets/v1/static")


def test_secrets_path_tolerates_a_missing_leading_slash():
    assert wlc.build_secrets_path("SITE", "folders") == "/site/SITE/secrets/folders"


def test_auth_path_is_a_different_grammar_and_takes_no_version():
    # Workload-identity registration lives on the platform auth service, not
    # under /secrets — conflating the two would 404 in a confusing way.
    assert (wlc.build_auth_path("SITE", "/workload-identities")
            == "/site/SITE/platform/auth/workload-identities")


# ── Expiry parsing ───────────────────────────────────────────────────────────

def test_expiration_with_offset_normalises_to_naive_utc():
    # The API's own sample uses a -06:00 offset; the DB columns are naive UTC.
    assert (wlc.parse_expiration("2026-09-14T15:00:00-06:00")
            == datetime(2026, 9, 14, 21, 0, 0))


def test_expiration_accepts_a_trailing_z():
    assert wlc.parse_expiration("2026-09-14T21:00:00Z") == datetime(2026, 9, 14, 21, 0, 0)


def test_expiration_passes_through_a_naive_datetime():
    assert wlc.parse_expiration(NOW) == NOW


def test_unparseable_expiration_is_none_not_an_exception():
    # None means "refresh now", which is the safe direction.
    assert wlc.parse_expiration("not a date") is None
    assert wlc.parse_expiration("") is None
    assert wlc.parse_expiration(None) is None


# ── parse_generated ──────────────────────────────────────────────────────────

def test_aws_camel_case_response_is_parsed():
    out = wlc.parse_generated({"secret": {
        "accessKeyId": "ASIAEXAMPLE",
        "secretAccessKey": "sekrit",
        "sessionToken": "token",
        "leaseId": "lease-1",
        "expiration": "2026-08-20T18:00:00Z",
    }})
    assert out["values"] == {"access_key_id": "ASIAEXAMPLE",
                             "secret_access_key": "sekrit",
                             "session_token": "token"}
    assert out["lease_id"] == "lease-1"
    assert out["expires_at"] == datetime(2026, 8, 20, 18, 0, 0)


def test_the_shape_the_live_api_actually_returned():
    """Recorded from a real `generate` against a provisioned site, 2026-08-21.

    Until this call was made the response shape was a guess: the vendor wiki documented
    two incompatible ones and the Terraform provider never calls generate, so nothing
    could settle it. The observed answer is nested-under-`secret`, camelCase — which the
    test above had right.

    Kept as a separate case anyway, because the live payload carried two fields we had
    never seen (`credentialType`, `type`) and their handling is the part worth pinning.
    """
    out = wlc.parse_generated({"secret": {
        "accessKeyId": "ASIAEXAMPLE",
        "secretAccessKey": "sekrit",
        "sessionToken": "token",
        "leaseId": "545a16b2-b973-427c-97a6-ca6cea34b3b7",
        "expiration": "2026-08-21T17:43:49Z",
        "credentialType": "assumed_role",
        "type": "aws",
    }})
    assert out["lease_id"] == "545a16b2-b973-427c-97a6-ca6cea34b3b7"
    assert out["expires_at"] == datetime(2026, 8, 21, 17, 43, 49)

    # The point of the test: `values` is spread into boto3 client kwargs, so an unknown
    # field arriving there is a TypeError on every AWS call. Exact equality, not a
    # subset — the whole risk is EXTRA keys, which a subset check would not catch.
    assert out["values"] == {"access_key_id": "ASIAEXAMPLE",
                             "secret_access_key": "sekrit",
                             "session_token": "token"}


def test_the_session_token_is_present_in_the_observed_shape():
    """The field the whole dynamic tier depends on.

    Four call sites had to be changed to carry it. Had the live response omitted it,
    every one of them would have been pointless — so it is worth an assertion of its own
    rather than being folded into a dict comparison.
    """
    out = wlc.parse_generated({"secret": {
        "accessKeyId": "A", "secretAccessKey": "B", "sessionToken": "C",
        "leaseId": "l", "expiration": "2026-08-21T17:43:49Z"}})
    assert out["values"]["session_token"] == "C"


def test_aws_pascal_case_response_is_parsed_too():
    # BeyondTrust's own GitHub Action accepts both, so we must as well.
    out = wlc.parse_generated({"secret": {
        "AccessKeyId": "ASIA2", "SecretAccessKey": "s", "SessionToken": "t"}})
    assert out["values"]["access_key_id"] == "ASIA2"
    assert out["values"]["session_token"] == "t"


def test_lease_id_and_expiration_are_read_from_the_response_root_too():
    out = wlc.parse_generated({
        "secret": {"accessKeyId": "A", "secretAccessKey": "B", "sessionToken": "C"},
        "leaseId": "root-lease",
        "expiration": "2026-08-20T18:00:00Z",
    })
    assert out["lease_id"] == "root-lease"
    assert out["expires_at"] == datetime(2026, 8, 20, 18, 0, 0)


def test_azure_response_is_parsed_and_key_id_is_optional():
    out = wlc.parse_generated({"secret": {
        "clientId": "cid", "clientSecret": "csecret", "tenantId": "tid"}})
    assert out["values"]["client_id"] == "cid"
    assert out["values"]["tenant_id"] == "tid"
    # key_id only correlates a revoke; its absence is not a failure.
    assert out["values"]["key_id"] is None


def test_a_credential_at_the_response_root_is_accepted():
    out = wlc.parse_generated({"accessKeyId": "A", "secretAccessKey": "B",
                               "sessionToken": "C"})
    assert out["values"]["access_key_id"] == "A"


def test_a_partial_aws_credential_raises_rather_than_returning_half():
    # A credential missing its session token fails later, somewhere else,
    # looking like a permissions problem. Fail here instead.
    try:
        wlc.parse_generated({"secret": {"accessKeyId": "A", "secretAccessKey": "B"}})
    except wlc.WorkloadCredentialsError as exc:
        assert "session_token" in str(exc)
    else:
        raise AssertionError("expected WorkloadCredentialsError")


def test_an_unrecognised_payload_names_the_keys_but_never_the_values():
    try:
        wlc.parse_generated({"secret": {"surprise": "s3kr1t-do-not-log"}})
    except wlc.WorkloadCredentialsError as exc:
        assert "surprise" in str(exc)
        assert "s3kr1t-do-not-log" not in str(exc)
    else:
        raise AssertionError("expected WorkloadCredentialsError")


def test_a_non_dict_response_raises():
    for bad in ([], "text", None, 7):
        try:
            wlc.parse_generated(bad)
        except wlc.WorkloadCredentialsError:
            pass
        else:
            raise AssertionError(f"expected WorkloadCredentialsError for {bad!r}")


# ── refresh_due ──────────────────────────────────────────────────────────────

def _at(hour, minute=0):
    return datetime(2026, 8, 20, hour, minute, 0)


def test_a_fresh_lease_is_not_due():
    # Issued at 12:00, expires 13:00, 50% margin → due from 12:30.
    assert wlc.refresh_due(_at(13), _at(12), 50, now=_at(12, 10)) is False


def test_a_lease_past_its_margin_is_due():
    assert wlc.refresh_due(_at(13), _at(12), 50, now=_at(12, 40)) is True


def test_a_lease_exactly_at_its_margin_is_due():
    assert wlc.refresh_due(_at(13), _at(12), 50, now=_at(12, 30)) is True


def test_an_expired_lease_is_due():
    assert wlc.refresh_due(_at(11), _at(10), 50, now=_at(12)) is True


def test_a_missing_expiry_is_always_due():
    # One wasted metered issuance beats trusting an unknown lease and having
    # every cloud call fail.
    assert wlc.refresh_due(None, _at(12), 50, now=_at(12)) is True


def test_a_missing_issue_time_falls_back_to_an_hour_window():
    assert wlc.refresh_due(_at(13), None, 50, now=_at(12, 40)) is True
    assert wlc.refresh_due(_at(14), None, 50, now=_at(12)) is False


def test_margin_is_clamped_so_zero_does_not_mean_never_refresh():
    # 0 would otherwise read as "no margin" and pin a dead lease; clamped to 1%.
    assert wlc.refresh_due(_at(13), _at(12), 0, now=_at(12, 59, )) is True
    assert wlc.refresh_due(_at(13), _at(12), 0, now=_at(12, 10)) is False


def test_margin_is_clamped_so_hundred_does_not_bill_on_every_check():
    # 100 would mean "always due", i.e. a metered issuance per call. Clamped to 99.
    assert wlc.refresh_due(_at(13), _at(12), 100, now=_at(12, 0)) is False


def test_a_nonpositive_ttl_is_due():
    assert wlc.refresh_due(_at(12), _at(13), 50, now=_at(11)) is True


# ── Static-secret value shaping ──────────────────────────────────────────────

def test_static_read_unwraps_the_secret_map_as_json():
    # A WC static secret is a map, not a scalar — the provider models it as
    # secret_wo = { token = "..." }.
    assert wlc.static_value_from({"secret": {"token": "abc"}}) == '{"token": "abc"}'


def test_static_read_passes_a_bare_string_through():
    assert wlc.static_value_from("already-a-string") == "already-a-string"


def test_static_read_falls_back_to_serialising_the_whole_payload():
    assert wlc.static_value_from({"unexpected": 1}) == '{"unexpected": 1}'


def test_a_json_object_maps_straight_onto_the_secret_map():
    body = wlc.static_write_body('{"username": "u", "password": "p"}')
    assert body == {"secret": {"username": "u", "password": "p"}}


def test_a_json_scalar_is_wrapped_rather_than_rejected():
    assert wlc.static_write_body('"plain"') == {"secret": {"value": '"plain"'}}


def test_invalid_json_is_wrapped_rather_than_raising():
    # The Secrets page validates JSON at the API edge; this is belt-and-braces
    # for internal callers that write non-JSON infrastructure values.
    assert wlc.static_write_body("not json") == {"secret": {"value": "not json"}}


def test_write_then_read_round_trips_an_object():
    original = '{"username": "u", "password": "p"}'
    stored = wlc.static_write_body(original)
    assert wlc.static_value_from(stored) == original


# ── Error shaping ────────────────────────────────────────────────────────────

def test_a_coded_error_surfaces_both_code_and_message():
    msg = wlc.error_message_from(403, {"Code": "forbidden", "Message": "no access"})
    assert "403" in msg and "forbidden" in msg and "no access" in msg


def test_a_lowercase_coded_error_is_also_understood():
    assert "boom" in wlc.error_message_from(500, {"message": "boom"})


def test_an_html_error_page_is_truncated():
    # Otherwise it lands verbatim in a job's error_message, the only failure
    # detail the UI renders.
    msg = wlc.error_message_from(502, "<html>" + ("x" * 5000) + "</html>")
    assert len(msg) < 300


def test_an_empty_body_still_yields_the_status():
    assert "418" in wlc.error_message_from(418, "")


def test_conflict_detection_matches_the_shapes_that_mean_already_exists():
    assert wlc.is_conflict(Exception("HTTP 409")) is True
    assert wlc.is_conflict(Exception("Conflict: version mismatch")) is True
    assert wlc.is_conflict(Exception("secret already exists")) is True
    assert wlc.is_conflict(Exception("HTTP 500 boom")) is False


# ── Wiring guards (the drift this repo keeps getting bitten by) ──────────────

def test_the_backend_is_registered_in_every_dispatch_table():
    # Six tables in secrets_backend_service; missing one shows up as a feature
    # that half-works (readable but not listable, say) rather than as an error.
    src = _read("web_dashboard", "services", "secrets_backend_service.py")
    for table, fn in (("_TEST_FN", "test_wlc"), ("_WRITE_FN", "write_wlc"),
                      ("_READ_FN", "read_wlc"), ("_DESCRIBE_FN", "describe_wlc"),
                      ("_LIST_FN", "list_wlc"), ("_DELETE_FN", "delete_wlc")):
        block = src.split(table, 1)[1].split("}", 1)[0]
        assert '"wlc"' in block, f"{table} is missing the wlc entry"
        assert fn in block, f"{table} does not point at {fn}"


def test_the_pat_is_registered_in_all_four_secret_lists():
    # These four lists are deliberately non-identical and have drifted before,
    # so a new secret has to be added to each one by hand.
    for parts, label in (
        (("web_dashboard", "services", "config_service.py"), "_SECRET_KEYS"),
        (("web_dashboard", "services", "secret_hygiene.py"), "SECRET_REGISTRY"),
        (("web_dashboard", "api", "setup.py"), "_WIZARD_SECRET_FIELDS"),
        (("web_dashboard", "scripts", "config_migrate", "classify.py"),
         "HTTP_MASKED_KEYS"),
    ):
        assert "wlc_pat" in _read(*parts), f"{label} is missing wlc_pat"


def test_the_pat_cannot_be_migrated_into_workload_credentials_itself():
    # Migrating the PAT into WC would make the backend unreadable without itself.
    block = _assign_source(_read("web_dashboard", "api", "secrets.py"),
                           "_BOOTSTRAP_BLOCKLIST")
    assert '"wlc"' in block and "wlc_pat" in block


def test_the_reference_prefix_is_registered_on_both_sides():
    # config_service resolves the prefix; secret_hygiene owns the id->prefix map.
    # A prefix in one and not the other resolves to an empty string at runtime.
    assert '"wlc://"' in _read("web_dashboard", "services", "config_service.py")
    assert '"wlc://"' in _read("web_dashboard", "services", "secret_hygiene.py")


def test_the_feature_is_gated_behind_a_preview_flag():
    # Workload Credentials is not yet generally available and its API may still
    # change, so it must not sit among the normal integration toggles.
    src = _read("web_dashboard", "api", "setup.py")
    flags = src.split("_PREVIEW_FLAGS = {", 1)[1].split("\n}", 1)[0]
    assert "workload_credentials_enabled" in flags
    cfg_map = src.split("_PREVIEW_FLAG_CONFIG = {", 1)[1].split("\n}", 1)[0]
    assert "workload_credentials" in cfg_map


def test_the_panel_is_config_only_so_the_preview_flag_owns_the_switch():
    # If the panel also wrote workload_credentials_enabled, saving config would
    # silently flip the feature on or off behind the operator.
    block = _assign_source(_read("web_dashboard", "api", "setup.py"),
                           "_CONFIG_ONLY_FEATURES")
    assert "workload_credentials" in block


def test_dynamic_secrets_are_not_reachable_through_the_secrets_backend():
    # config_service resolves wlc:// references on read. If `generate` were
    # wired into that path, every reference resolution would mint — and bill —
    # a fresh cloud credential.
    src = _read("web_dashboard", "services", "secrets_backend_service.py")
    assert "wlc.generate" not in src
    assert "generate(" not in src.split("BeyondTrust Workload Credentials", 1)[1] \
                                 .split("# ── Dispatch table", 1)[0]


# ── Settings-panel binding drift (this repo has shipped it in both directions) ─

_PANEL_KEY = "workload_credentials"


def _model_fields():
    """Field names declared on WorkloadCredentialsFeatureConfig."""
    import ast
    tree = ast.parse(_read("web_dashboard", "api", "setup.py"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WorkloadCredentialsFeatureConfig":
            return [s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
    raise AssertionError("WorkloadCredentialsFeatureConfig not found")


def _panel_html():
    """Just this feature's <template> block from settings.html."""
    html = _read("web_dashboard", "templates", "settings.html")
    marker = f"panel?.key === '{_PANEL_KEY}'"
    start = html.index(marker)
    end = html.index("</template>", start)
    return html[start:end]


def test_every_declared_panel_field_is_bound_in_the_template():
    # Declared but unbound is env/PATCH-only: the operator edits it in the UI,
    # sees no field, and the value silently never changes.
    import re
    bound = set(re.findall(r"panelCfg\.([a-z_0-9]+)", _panel_html()))
    missing = [f for f in _model_fields() if f not in bound]
    assert not missing, f"declared on the model but not bound in settings.html: {missing}"


def test_every_bound_panel_field_is_declared_on_the_model():
    # Bound but undeclared is worse: pydantic drops the unknown key, so the
    # field renders, accepts input, and is discarded on save.
    import re
    declared = set(_model_fields())
    bound = set(re.findall(r"panelCfg\.([a-z_0-9]+)", _panel_html()))
    # Underscore-prefixed keys are panel metadata _read_feature adds to describe the
    # fields (which are secret, which already hold a value); savePanelConfig strips them
    # before the PATCH. Allowlisted by name so the prefix can't hide a real config key.
    meta = {k for k in bound if k.startswith("_")}
    assert not (meta - {"_secret_fields", "_secrets_set"}), \
        f"unknown panelCfg metadata key: {sorted(meta - {'_secret_fields', '_secrets_set'})}"
    extra = sorted(bound - meta - declared)
    assert not extra, f"bound in settings.html but not declared on the model: {extra}"


def test_the_panel_does_not_bind_the_preview_flag_itself():
    # The preview toggle owns workload_credentials_enabled. A second writer for
    # it here would flip the feature as a side effect of saving config.
    assert "panelCfg.workload_credentials_enabled" not in _panel_html()


def test_the_panel_carries_a_preview_warning():
    """The operator has to see this without reading the docs.

    Deliberately checks for the status word only, not a date. This repository is public
    and a release schedule is BeyondTrust's to announce, so no operator-facing text here
    carries one.
    """
    html = _panel_html()
    assert "Preview" in html or "preview" in html
    for leak in ("2026-10-14", "2026-09-14", "code freeze", "general availability on"):
        assert leak not in html, f"the panel should not state {leak!r}"


# ── Timestamp envelope tolerance ─────────────────────────────────────────────
#
# The live create response nests everything but the value under "metadata". Whether the
# metadata and list routes do the same is unconfirmed, and getting it wrong is SILENT:
# no timestamp means staleness reports when the reference was pasted in here rather than
# the vault's own last-changed date.

def _timestamp_fn():
    """`_wlc_timestamp` from secrets_backend_service, loaded by file path.

    The module's only top-level imports are stdlib, and every relative import sits inside
    a function, so it loads without the package chain.
    """
    import importlib.util
    path = os.path.join(_ROOT, "web_dashboard", "services", "secrets_backend_service.py")
    spec = importlib.util.spec_from_file_location("secrets_backend_service", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._wlc_timestamp


def test_a_flat_timestamp_is_found():
    assert _timestamp_fn()({"updatedAt": "2026-08-20T15:00:00Z"}) == "2026-08-20T15:00:00Z"


def test_a_timestamp_nested_under_metadata_is_found():
    # The shape actually observed from a live create.
    fn = _timestamp_fn()
    observed = {"metadata": {"createdAt": "2026-08-20T15:16:10Z", "id": "x", "version": 1},
                "secret": {"username": "u"}}
    assert fn(observed) == "2026-08-20T15:16:10Z"


def test_an_update_time_wins_over_a_create_time():
    fn = _timestamp_fn()
    assert fn({"updatedAt": "2026-08-20T16:00:00Z",
               "createdAt": "2026-08-01T10:00:00Z"}) == "2026-08-20T16:00:00Z"


def test_no_timestamp_anywhere_is_none():
    fn = _timestamp_fn()
    assert fn({"id": "x", "version": 2}) is None
    assert fn({"metadata": {"id": "x"}}) is None


def test_a_non_dict_is_none_rather_than_an_error():
    fn = _timestamp_fn()
    assert fn(None) is None
    assert fn("2026-08-20") is None
    assert fn([]) is None


def test_a_non_dict_metadata_member_does_not_break_the_scan():
    # Defensive: a provider that returns metadata: null must not take out the top level.
    fn = _timestamp_fn()
    assert fn({"metadata": None, "updatedAt": "2026-08-20T15:00:00Z"}) == "2026-08-20T15:00:00Z"


# ── Collection envelope ──────────────────────────────────────────────────────
#
# Verified live: collections come back as {"data": [...]}. The earlier guess
# (secrets/static/items) matched none of them, so list_static returned [] on every call
# and the Secrets page showed nothing — a silent empty rather than an error.

def test_a_collection_keyed_under_data_is_found():
    # The confirmed live shape.
    assert wlc._collection({"data": [{"name": "a"}]}) == [{"name": "a"}]


def test_a_bare_list_is_returned_unchanged():
    assert wlc._collection([{"name": "a"}]) == [{"name": "a"}]


def test_an_empty_collection_is_an_empty_list():
    assert wlc._collection({"data": []}) == []


def test_legacy_collection_keys_are_still_tolerated():
    # Kept as fallbacks: the API is still pre-release and may rename things, and a wrong
    # guess here fails silently rather than loudly.
    for key in ("secrets", "static", "items", "folders"):
        assert wlc._collection({key: [{"name": "a"}]}) == [{"name": "a"}]


def test_an_unrecognised_envelope_is_an_empty_list_not_an_error():
    assert wlc._collection({"unexpected": {"nested": 1}}) == []
    assert wlc._collection(None) == []
    assert wlc._collection("text") == []


def test_data_wins_over_the_legacy_keys():
    assert wlc._collection({"data": [1], "secrets": [2]}) == [1]


# ── The read envelope, as observed live ──────────────────────────────────────

def test_the_live_read_response_yields_just_the_secret_map():
    """The exact body a live GET /static/{name} returned.

    The value sits under `secret` alongside a sibling `metadata` block; anything that
    returned the whole envelope would hand the Secrets page a document containing
    version and id fields the operator never wrote.
    """
    observed = {"metadata": {"createdAt": "2026-08-20T15:16:10.140955Z",
                             "id": "cc407dd9-3314-4571-9f53-279d952019ac",
                             "tags": {}, "version": 1},
                "secret": {"password": "p", "username": "u"}}
    assert wlc.static_value_from(observed) == '{"password": "p", "username": "u"}'


def test_the_live_metadata_response_is_flat():
    """A live GET /static/{name}/metadata returned timestamps at the TOP level.

    Recorded because the create and read responses nest theirs under `metadata`, so the
    inconsistency is the surprising part and the reason the timestamp scan reads both.
    """
    observed = {"createdAt": "2026-08-20T15:16:10.140955Z",
                "id": "cc407dd9-3314-4571-9f53-279d952019ac",
                "tags": {}, "version": 1}
    fn = _timestamp_fn()
    assert fn(observed) == "2026-08-20T15:16:10.140955Z"


# ── List entries, as observed live ───────────────────────────────────────────
#
# A live entry carries ONLY `path` — no `name`, no `folder` — with timestamps nested
# under `metadata`. Deriving the folder from config instead of from the path is correct
# only by coincidence for a top-level secret, and wrong for anything in a sub-folder,
# which listing returns because it is recursive.

LIVE_LIST_ENTRY = {"metadata": {"createdAt": "2026-08-20T15:16:10.140955Z",
                                "id": "cc407dd9-3314-4571-9f53-279d952019ac",
                                "tags": {}, "version": 1},
                   "path": "dashboard/wlc-probe",
                   "type": "static"}


def _backend_mod():
    """secrets_backend_service, loaded by file path (stdlib-only top-level imports)."""
    import importlib.util
    path = os.path.join(_ROOT, "web_dashboard", "services", "secrets_backend_service.py")
    spec = importlib.util.spec_from_file_location("secrets_backend_service_list", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_live_list_entry_yields_name_folder_and_ref_from_the_path():
    """The exact body a live GET /static?recursive=true returned.

    `path` is the reference; everything else is derived from it, because the entry has no
    name and no folder of its own.
    """
    mod = _backend_mod()
    folder, name = mod._wlc_split(LIVE_LIST_ENTRY["path"])
    assert folder == "dashboard"
    assert name == "wlc-probe"
    assert mod._wlc_timestamp(LIVE_LIST_ENTRY) == "2026-08-20T15:16:10.140955Z"


def test_a_subfolder_path_keeps_its_full_folder():
    """The case that made deriving the folder from config wrong.

    Listing is recursive, so a secret two levels down comes back too. Taking the folder
    from config would report it as `dashboard/x`, and every later read of it would 404.
    """
    mod = _backend_mod()
    folder, name = mod._wlc_split("dashboard/sub/deeper/x")
    assert folder == "dashboard/sub/deeper"
    assert name == "x"


def test_a_root_level_path_has_no_folder():
    mod = _backend_mod()
    folder, name = mod._wlc_split("just-a-name")
    assert folder == ""
    assert name == "just-a-name"


def test_list_wlc_reads_path_and_never_name_or_folder():
    # The guard. The live entry has no `name` and no `folder` key at all, so an
    # implementation relying on either returns nothing (or a wrong ref) and the page
    # looks empty rather than erroring.
    src = _read("web_dashboard", "services", "secrets_backend_service.py")
    body = src.split("def list_wlc", 1)[1].split("\ndef ", 1)[0]
    assert 'entry.get("path")' in body
    assert 'entry.get("name")' not in body
    assert 'entry.get("folder")' not in body


def test_list_wlc_skips_entries_that_are_not_static():
    # A dynamic secret handed to a reader that decodes it as a stored value would be a
    # billable mint at best and a confusing failure at worst.
    src = _read("web_dashboard", "services", "secrets_backend_service.py")
    body = src.split("def list_wlc", 1)[1].split("\ndef ", 1)[0]
    assert '"static"' in body


# ── The panel's live auth check ───────────────────────────────────────────────
#
# Source-level, because this file loads the service by path and never builds the
# FastAPI app. The point of the endpoint is diagnostic: /credential-sources replays the
# last recorded lease error out of the database, so a revoked PAT, a mistyped folder and
# a stale row all render identically, and the error text can outlive the problem. This
# is the only surface that actually asks BeyondTrust.


def test_test_connection_uses_the_unmetered_session_endpoint():
    """`GET /session` authenticates without issuing anything. Swapping it for a mint
    would put a BILLED call behind a button an operator is invited to press repeatedly."""
    import ast
    src = _read("web_dashboard", "services", "workload_credentials_service.py")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "test_connection")
    body = ast.get_source_segment(src, fn)
    assert '"/session"' in body, "test_connection must probe /session"
    assert "generate" not in body, "a connection test must never mint a metered credential"


def test_the_panel_route_is_exposed_and_off_the_hot_path():
    src = _read("web_dashboard", "api", "secrets.py")
    assert '@router.post("/wlc/test")' in src, \
        "the Workload Credentials panel has no live auth check"
    assert "asyncio.to_thread(wlc.test_connection)" in src, \
        "test_connection is sync and does network I/O — it must not block the event loop"
    assert "_require_admin(request)" in src


def test_the_panel_has_a_test_connection_button():
    html = _read("web_dashboard", "templates", "settings.html")
    assert "testWlcConnection()" in html, "no button calls the probe"
    assert "/api/secrets/wlc/test" in html, "the button posts to the wrong path"


def test_the_pat_field_is_never_prefilled_with_a_mask():
    """A masked value in a type=password box is indistinguishable from an empty one, so
    pasting into it appends to the mask and the save silently drops the field. This is
    the bug that made a wrong PAT look unfixable — the box must render genuinely empty."""
    html = _read("web_dashboard", "templates", "settings.html")
    pat = next(line for line in html.splitlines() if "panelCfg.wlc_pat" in line)
    assert "x-model" in pat and "type=\"password\"" in pat
    # A :placeholder (bound) is fine — it renders only while the field is empty. A plain
    # value= or a static bullet placeholder standing in for a stored token is not.
    assert ":placeholder" in pat or "placeholder" not in pat, \
        "the stored-token hint must be a bound placeholder, not a pre-filled value"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
