"""One list of secret config keys, and GET /api/setup/config masks all of them.

There used to be four lists, and they had drifted. The one that decided what the setup
endpoint masked held five keys, so an admin's GET /api/setup/config returned the rest
decrypted: the BeyondTrust Gateway deploy keys, the ACR password, every hypervisor password,
the OIDC client secret. The wizard then copied some of them into its form in plaintext.

Now ``secret_hygiene.SECRET_KEYS`` is the list, and the endpoint, the feature panels, the
wizard's blank-keeps-stored rule and config_migrate all use it. What this file pins:

  * **Completeness.** Every setting or feature-panel field whose name looks like a secret
    is in the list, or in ``_NOT_SECRET`` below with the reason it only looks like one. A
    new ``*_password`` that nobody registers fails here, rather than shipping unmasked.
  * **The endpoint masks every one of them**, and nothing that is not a secret.
  * **A masked value can never be written back** by the wizard, and the page drops masked
    values before it pre-fills a field.

Runs under pytest, or standalone:
    python tests/test_secret_keys.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-secret-keys")

_SETUP_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "setup.html")

# Names that look like a secret and are not one. Each needs a reason.
_NOT_SECRET = {
    "eso_bt_credentials_secret": "the NAME of a Kubernetes Secret, not its contents",
    "k8s_ps_token_delete_legacy_secret": "a boolean: whether to retire a legacy Secret",
}


def _looks_secret_names():
    from web_dashboard.api import setup as s
    from web_dashboard.config import settings
    from web_dashboard.scripts.config_migrate import classify
    names = set(type(settings).model_fields)
    for model in s._FEATURE_MODELS.values():
        names |= set(model.model_fields)
    # classify.is_secret is the suffix rule, with its pointer exceptions (*_secret_name and
    # friends). Pass names through it without the list itself, so the check is not circular.
    return {n for n in names
            if n.endswith(classify._SECRET_SUFFIXES) and not n.endswith(classify._POINTER_SUFFIXES)}


# ── one list, used everywhere ────────────────────────────────────────────────

def test_every_secret_looking_setting_is_registered_or_explained():
    from web_dashboard.services.secret_hygiene import SECRET_KEYS
    missing = sorted(_looks_secret_names() - SECRET_KEYS - set(_NOT_SECRET))
    assert not missing, (
        "these look like secrets but are not in secret_hygiene.SECRET_KEYS, so "
        "GET /api/setup/config would return them decrypted:\n  " + "\n  ".join(missing)
        + "\nAdd them there, or to _NOT_SECRET here with the reason they are not secrets.")


def test_the_allowlist_holds_only_names_that_still_exist_and_are_not_listed():
    from web_dashboard.services.secret_hygiene import SECRET_KEYS
    names = _looks_secret_names()
    for key in _NOT_SECRET:
        assert key in names, f"{key} no longer exists; drop it from _NOT_SECRET"
        assert key not in SECRET_KEYS, f"{key} is both a secret and not one"


def test_the_vault_registry_is_a_subset():
    from web_dashboard.services.secret_hygiene import SECRET_KEYS, SECRET_REGISTRY
    assert {k for k, _ in SECRET_REGISTRY} <= SECRET_KEYS


def test_every_consumer_uses_the_one_list():
    from web_dashboard.api import setup as s
    from web_dashboard.scripts.config_migrate import classify
    from web_dashboard.services import config_service
    from web_dashboard.services.secret_hygiene import SECRET_KEYS
    assert config_service._SECRET_KEYS is SECRET_KEYS, "the setup endpoint masks another list"
    assert s._SECRET_FEATURE_KEYS is SECRET_KEYS, "feature panels redact another list"
    assert s._WIZARD_SECRET_FIELDS is SECRET_KEYS, "the wizard keeps another list"
    assert classify.HTTP_MASKED_KEYS is SECRET_KEYS, "config_migrate expects another list"


# ── the endpoint masks them ──────────────────────────────────────────────────

def test_get_all_public_masks_every_secret_and_nothing_else():
    from web_dashboard import database as d
    from web_dashboard.services import config_service
    from web_dashboard.services.secret_hygiene import SECRET_KEYS
    d.Base.metadata.create_all(bind=d.engine)
    plain = {"aws_region": "us-east-2", "eso_bt_credentials_secret": "bt-creds",
             # a vault reference is a pointer, not the secret: shown, so it can migrate
             "vsphere_password": "azure_kv://vsphere-pass"}
    secrets = {k: f"real-{k}" for k in SECRET_KEYS if k not in plain}
    try:
        config_service.set_many({**plain, **secrets})
        config_service.invalidate()
        out = config_service.get_all_public()
        leaked = sorted(k for k in secrets if out.get(k) != config_service.MASK)
        assert not leaked, f"returned unmasked: {leaked}"
        for k, v in plain.items():
            assert out.get(k) == v, f"{k} was masked but is not a secret"
    finally:
        for k in list(plain) + list(secrets):
            config_service.delete(k)
        config_service.invalidate()


# ── a mask is never written back ─────────────────────────────────────────────

def test_the_wizard_keeps_the_stored_secret_for_a_blank_or_masked_field():
    from web_dashboard.api.setup import _keep_stored
    for value in ("", None, "••••••••", "••"):
        assert _keep_stored("azure_acr_password", value), repr(value)
    assert not _keep_stored("azure_acr_password", "new-password")
    assert not _keep_stored("aws_region", ""), "a blank NON-secret field is written as blank"


def test_the_wizard_page_drops_masked_values_before_it_prefills():
    src = open(_SETUP_HTML, encoding="utf-8").read()
    init = src.split("const cfg = await cfgRes.json();", 1)[1].split("this.form.", 1)[0]
    assert "startsWith('••')" in init and "delete cfg[k]" in init, (
        "setup.html copies config into the form without dropping masked values first; a "
        "pre-filled mask would be submitted as the secret")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
