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
import io
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
    # Workload Credentials is pre-GA and has already shipped one breaking API
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
    extra = sorted(bound - declared)
    assert not extra, f"bound in settings.html but not declared on the model: {extra}"


def test_the_panel_does_not_bind_the_preview_flag_itself():
    # The preview toggle owns workload_credentials_enabled. A second writer for
    # it here would flip the feature as a side effect of saving config.
    assert "panelCfg.workload_credentials_enabled" not in _panel_html()


def test_the_panel_warns_that_the_feature_is_pre_ga():
    # The operator has to be able to see this without reading the docs.
    html = _panel_html()
    assert "Pre-GA" in html or "pre-GA" in html


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
