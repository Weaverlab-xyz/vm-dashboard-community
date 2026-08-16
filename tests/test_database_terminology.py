"""The feature is "Databases", not "Cloud Databases"; this pins how far that goes.

Provisioning was the whole feature once, and it is cloud-only because it needs a Terraform
module. Registration changed that: a `CloudDatabase` row can now be one the dashboard never
built, on-premises included (`source='registered'`, `cloud='local'`), and
VALID_REGISTER_CLOUDS is deliberately wider than VALID_CLOUDS. So the *name* dropped
"cloud" while the *identifiers* kept it, and that second half is the part worth a test —
the obvious next contribution is "finish the rename", and finishing it would break things
that are not ours to rename:

  * **persisted permission data.** ``cloud_database`` is a key inside every user's and
    group's permissions JSON, and ``scripts/bootstrap_entitle_groups.py`` turns scope names
    into Entitle group names in a live tenant. Renaming the scope silently un-grants
    everyone who had it — the failure is a locked-out user, not an error.
  * **persisted config keys.** ``cloud_database_enabled`` and the ``clouddb_*`` keys are
    rows in ``app_config`` and lines in operators' ``.env`` files. Renaming the constant
    reads a key that was never written: the setting goes blank and nothing raises.
  * **names of things that already exist.** ``clouddb-jumpoint`` is a VM in someone's
    Azure subscription; ``clouddb-{id}`` is a live RDS identifier; ``clouddb/{id}/admin``
    is a path in the encrypted store; ``clouddb:`` prefixes inventory ids. Renaming the
    string orphans the resource or drops the row.

Unlike the Jumpoint→Gateway rename (see test_gateway_terminology.py, which this file is
modelled on) there is deliberately **no** blanket "no 'cloud database' in prose" rule:
plenty of remaining uses are correct, because they really are about the cloud-provisioned
half — a Terraform module per cloud, the shared cloud gateway host, the sandbox's DB
subnets. So this file asserts specific display strings instead of sweeping for a word.

Run: python tests/test_database_terminology.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TEXT_EXT = (".html", ".md", ".py", ".yml", ".yaml", ".js")


def _walk(root, exts=_TEXT_EXT):
    for dp, _, files in os.walk(os.path.join(_ROOT, root)):
        for f in sorted(files):
            if f.endswith(exts):
                yield os.path.join(dp, f)


def _read(path):
    return open(path, encoding="utf-8").read()


def _tpl(*parts):
    return os.path.join(_ROOT, "web_dashboard", "templates", *parts)


# ── the display half is done ──────────────────────────────────────────────────

def test_the_page_says_databases():
    src = _read(_tpl("databases", "index.html"))
    assert ">Databases</h1>" in src, "the page heading is not 'Databases'"
    assert "Cloud Databases" not in src


def test_the_list_column_is_location_not_cloud():
    """'Cloud' can't head a column whose value is 'local' for an on-prem row. The
    register form already calls this field Location; the table has to agree."""
    src = _read(_tpl("databases", "index.html"))
    assert ">Location</th>" in src, "the list column is not headed 'Location'"
    assert ">Cloud</th>" not in src
    assert "locationLabel(d.cloud)" in src, (
        "the badge renders the raw cloud value, so an on-prem row reads LOCAL")


def test_the_delete_verb_follows_source():
    """decommission() already branches on source and its confirm text says
    'Deregister'. The button label has to say the same thing."""
    src = _read(_tpl("databases", "index.html"))
    assert "d.source === 'registered' ? 'Deregister' : 'Decommission'" in src


def test_no_user_facing_label_still_says_cloud_databases():
    """The nav, the page, the dashboard tile, both feature toggles and the
    Config-Management target group are the six places a person reads the name."""
    offenders = []
    for rel in (("_nav_links.html",), ("databases", "index.html"), ("dashboard.html",),
                ("settings.html",), ("setup.html",), ("config-mgmt", "index.html")):
        if "Cloud Databases" in _read(_tpl(*rel)):
            offenders.append(os.path.join(*rel))
    assert not offenders, f"these still label the feature 'Cloud Databases': {offenders}"


def test_the_config_management_optgroup_says_databases():
    """This is the picker a registered on-prem database feeds into, so it was the
    label that was actively wrong, not merely dated."""
    assert 'optgroup label="Databases"' in _read(_tpl("config-mgmt", "index.html"))


def test_the_permission_grid_maps_the_scope_to_a_label():
    """The scope key is persisted, so the grid gets a display map rather than a
    rename. Unmapped scopes must keep falling back to the old behaviour."""
    app_js = _read(os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js"))
    assert "function permissionScopeLabel(" in app_js
    assert "cloud_database: 'Databases'" in app_js
    assert "replace(/_/g, ' ')" in app_js, "the unmapped-scope fallback is gone"
    for rel in (("users", "list.html"), ("groups", "index.html")):
        src = _read(_tpl(*rel))
        assert "permissionScopeLabel(scope)" in src, f"{rel} renders the raw scope key"


def test_the_doc_was_renamed_and_retitled():
    docs = os.path.join(_ROOT, "docs")
    assert not os.path.exists(os.path.join(docs, "cloud-databases.md")), (
        "docs/cloud-databases.md is back — the /docs index label and page title are "
        "derived from the filename, so the name lives in the filename")
    body = _read(os.path.join(docs, "databases.md"))
    assert body.startswith("# Databases\n"), "docs/databases.md is not titled 'Databases'"
    assert "## Registering an existing database" in body, (
        "the doc doesn't document registration, which is why it was renamed")


def test_no_doc_links_to_the_old_filename():
    offenders = [os.path.relpath(p, _ROOT) for p in _walk("docs")
                 if "cloud-databases" in _read(p)]
    if "cloud-databases" in _read(os.path.join(_ROOT, "README.md")):
        offenders.append("README.md")
    assert not offenders, f"these still link to cloud-databases.md: {offenders}"


def test_the_oci_heading_anchor_is_intact():
    """docs/kubernetes.md deep-links this heading; retitling it silently 404s the
    anchor, which nothing else would catch."""
    anchor = "#oci-autonomous-database--read-the-caveats"
    assert "### OCI (Autonomous Database) — read the caveats" in _read(
        os.path.join(_ROOT, "docs", "databases.md"))
    assert anchor in _read(os.path.join(_ROOT, "docs", "kubernetes.md"))


# ── the identifier half must NOT be renamed ───────────────────────────────────

def test_the_table_and_model_are_untouched():
    src = _read(os.path.join(_ROOT, "web_dashboard", "database.py"))
    assert '__tablename__ = "cloud_databases"' in src, "the table was renamed"
    assert "class CloudDatabase(" in src, "the ORM model was renamed"


def test_the_permission_scope_is_untouched():
    """Stored in user/group permissions JSON and mirrored into Entitle group names —
    renaming it un-grants everyone who had it."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "api", "auth.py"))
    assert '"cloud_database"' in src, "the permission scope key was renamed"


def test_persisted_config_keys_are_untouched():
    keys = ("cloud_database_enabled", "clouddb_ps_onboarding_enabled",
            "clouddb_ps_workgroup", "clouddb_ps_azure_auth_mode")
    missing = [k for k in keys
               if not any(k in _read(p) for p in _walk("web_dashboard", exts=(".py", ".html")))]
    assert not missing, f"persisted config keys were renamed: {missing}"


def test_the_managed_resource_and_string_names_are_untouched():
    """Each of these names something that already exists — a VM, an RDS instance, a
    path in the encrypted store, an inventory id, a guardrail action."""
    for literal, where in (("clouddb-jumpoint", "web_dashboard"),
                           ("clouddb-shared-jumpoint", "web_dashboard"),
                           ("clouddb-nossl-pg16", "web_dashboard"),
                           ("clouddb/{db_id}/admin", "web_dashboard"),
                           ("clouddb-id", "web_dashboard"),
                           ("clouddb:", "web_dashboard")):
        found = any(literal in _read(p) for p in _walk(where, exts=(".py", ".html")))
        assert found, f"{literal!r} disappeared from {where}/ — it names live data"


def test_the_route_paths_are_untouched():
    """/databases and /api/databases were already clean, and the inventory page links
    resources at detail_href='/databases'."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "api", "cloud_databases.py"))
    assert 'prefix="/api/databases"' in src, "the API prefix moved"


def test_the_service_module_filenames_are_untouched():
    """test_expiry_wiring.py asserts on the literal filename cloud_database_service.py,
    and every test that stubs the service imports it by that name."""
    for rel in (("api", "cloud_databases.py"), ("services", "cloud_database_service.py")):
        assert os.path.exists(os.path.join(_ROOT, "web_dashboard", *rel)), (
            f"web_dashboard/{'/'.join(rel)} was renamed")


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
