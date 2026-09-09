"""Wiring tests for the Certificate Lab preview feature.

Text-and-registry checks rather than behaviour: every one of these pins a connection that
fails LATE and quietly if it is missed — a Terraform module that is only absent in the
published image, a job type the worker will not claim, an auto-delete kind that silently
never reaps a CA pool that goes on billing.

No app imports, so it runs on a checkout without the requirements installed.
Runs under pytest or standalone:  python tests/test_cert_lab_wiring.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the Terraform module reaches the image ────────────────────────────────────

def test_the_gcp_cas_module_exists_and_declares_the_google_provider():
    tf = _read("terraform", "cert_ca", "gcp_cas", "main.tf")
    assert 'source  = "hashicorp/google"' in tf
    for resource in ("google_privateca_ca_pool", "google_privateca_certificate_authority",
                     "google_service_account", "google_privateca_ca_pool_iam_member",
                     "google_service_account_key"):
        assert f'resource "{resource}"' in tf, resource


def test_the_three_teardown_guards_are_all_set():
    # Each one on its own leaves a pool that is still billing after `terraform destroy`:
    # deletion_protection refuses outright, a CA that has issued anything refuses without
    # ignore_active_certificates_on_deletion, and a deleted CA holds its pool for 30 days
    # without skip_grace_period.
    tf = _read("terraform", "cert_ca", "gcp_cas", "main.tf")
    assert "deletion_protection                    = false" in tf
    assert "ignore_active_certificates_on_deletion = true" in tf
    assert "skip_grace_period                      = true" in tf


def test_the_pool_defaults_to_the_devops_tier():
    # ~$20/month against an order of magnitude more for ENTERPRISE, which buys certificate
    # records the lab does not need.
    tf = _read("terraform", "cert_ca", "gcp_cas", "main.tf")
    assert re.search(r'variable "tier"\s*\{[^}]*default\s*=\s*"DEVOPS"', tf, re.S)


def test_the_aws_pca_module_exists_and_declares_the_aws_provider():
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    assert 'source  = "hashicorp/aws"' in tf
    for resource in ("aws_acmpca_certificate_authority", "aws_acmpca_certificate",
                     "aws_acmpca_certificate_authority_certificate",
                     "aws_iam_user", "aws_iam_user_policy", "aws_iam_access_key"):
        assert f'resource "{resource}"' in tf, resource


def test_the_aws_module_pins_the_provider_version_the_image_already_caches():
    # The image pre-caches `aws ~> 5.0` into a READ-ONLY provider mirror. A module that
    # pins anything else needs its own pre-cache leg, and without one the build's own
    # "provider mirror MISS" check fails — or worse, a provision dies months later with
    # "was not found in any of the search locations".
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    assert re.search(r'aws\s*=\s*\{[^}]*version\s*=\s*"~> 5\.0"', tf, re.S)
    assert 'aws = { source = "hashicorp/aws", version = "~> 5.0" }' in _read("Dockerfile")


def test_the_aws_ca_asks_for_the_shortest_deletion_window():
    # `permanent_deletion_time_in_days` defaults to 30, which leaves a destroyed CA
    # restorable for a month. 7 is the floor the API accepts, and this module exists to
    # get rid of the thing.
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    assert "permanent_deletion_time_in_days = 7" in tf
    # An IAM user with a stray key or inline policy fails the destroy; terraform removes
    # the key it made, force_destroy covers one added in the console.
    assert "force_destroy = true" in tf


def test_the_aws_root_is_self_signed_with_the_root_ca_template():
    # With the end-entity template the certificate is issued but the CA refuses to
    # install it, and the apply fails after the CA already exists — i.e. already billing.
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    assert "template/RootCACertificate/V1" in tf
    assert 'type       = "ROOT"' in tf


def test_the_aws_chain_is_read_off_the_signing_resource_not_the_ca():
    # The CA's own `certificate` attribute is populated by a refresh AFTER activation, so
    # on the apply that creates it the output would be empty — and an empty chain is
    # stored on the row and only noticed when the mTLS endpoint refuses every client.
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    chain = tf[tf.index('output "ca_chain_pem"'):]
    chain = chain[:chain.index("}")]
    assert "aws_acmpca_certificate.root.certificate" in chain, chain


def test_the_enrollment_policy_is_scoped_to_this_ca_and_to_issuance():
    # The GCP module grants certificateRequester, which covers creating certificates and
    # nothing else. A wildcard Resource here would hand the plugin every CA in the
    # account, which is the kind of thing nobody notices until an audit.
    tf = _read("terraform", "cert_ca", "aws_pca", "main.tf")
    policy = tf[tf.index('resource "aws_iam_user_policy"'):]
    policy = policy[:policy.index("\nresource ")]
    assert "aws_acmpca_certificate_authority.this.arn" in policy
    assert '"*"' not in policy, "the enrollment policy must not be account-wide"
    for action in ("acm-pca:IssueCertificate", "acm-pca:GetCertificate"):
        assert action in policy, action


def test_the_module_is_copied_into_the_image_and_reincluded_in_the_dockerignore():
    # A COPY without the .dockerignore re-include FAILS THE BUILD; a re-include without
    # the COPY fails only at deploy time, in the published image. Both, or neither.
    assert "COPY terraform/cert_ca/ ./terraform/cert_ca/" in _read("Dockerfile")
    assert "!terraform/cert_ca" in _read(".dockerignore")


def test_the_service_declares_the_module_path_the_shipped_modules_guard_can_see():
    # tests/test_terraform_modules_shipped.py regexes this exact construction out of
    # web_dashboard/services/*.py. Build the path any other way and the guard stops
    # seeing it, silently, and the COPY above can be deleted without failing anything.
    # EVERY cloud, not just the first: a path built another way degrades the guard's view
    # to the parent `terraform/cert_ca`, which still satisfies the existence and COPY
    # checks vacuously — so nothing fails and the specific module stops being covered.
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    for module in ("gcp_cas", "aws_pca"):
        assert f'os.path.join(_REPO_ROOT, "terraform", "cert_ca", "{module}")' in svc, module


# ── the worker will actually claim the jobs ───────────────────────────────────

def test_every_job_type_is_handled_tiered_and_dispatched():
    worker = _read("web_dashboard", "jobs_worker.py")
    for job_type in ("certca_provision", "certca_decommission", "cert_ps_register"):
        assert f'"{job_type}"' in worker, f"{job_type} missing from jobs_worker"
        assert f'elif job_type == "{job_type}":' in worker, f"{job_type} has no dispatch branch"
    # Exactly one tier each: HEAVY for the terraform pair, MEDIUM for the short
    # Password Safe apply. Two tiers would double-count the type against concurrency.
    heavy = worker.split("HEAVY_TYPES = (")[1].split(")")[0]
    medium = worker.split("MEDIUM_TYPES = (")[1].split("\n)")[0]
    light = worker.split("LIGHT_TYPES = (")[1].split("\n)")[0]
    assert '"certca_provision"' in heavy and '"certca_decommission"' in heavy
    assert '"cert_ps_register"' in medium
    for job_type in ("certca_provision", "certca_decommission", "cert_ps_register"):
        tiers = [name for name, body in
                 (("heavy", heavy), ("medium", medium), ("light", light))
                 if f'"{job_type}"' in body]
        assert len(tiers) == 1, f"{job_type} is in {tiers}, expected exactly one tier"


# ── the auto-delete timer reaches the CA ──────────────────────────────────────

def test_certlab_is_a_reapable_kind_with_an_idle_state_and_a_teardown():
    # A pool bills whether or not it ever issues a certificate and appears on no page the
    # dashboard had before this feature, so it is exactly the thing the timer is for.
    policy = _read("web_dashboard", "services", "expiry_policy.py")
    assert '"certlab"' in policy.split("REAPABLE_KINDS = (")[1].split(")")[0]
    assert '"certlab":  frozenset({"available"})' in policy
    # "failed" must NOT be reapable: a failed apply may have left a CA mid-creation, and a
    # destroy racing that is how a pool ends up undeletable.
    states = policy.split('"certlab":  frozenset({')[1].split("})")[0]
    assert "failed" not in states

    reaper = _read("web_dashboard", "services", "expiry_reaper.py")
    assert 'elif kind == "certlab":' in reaper
    assert "cert_lab_service.start_decommission" in reaper
    assert "CertLab" in reaper.split("\n")[0:60][-1] or "CertLab" in reaper


def test_the_inventory_emits_a_certlab_row_so_the_sweep_can_find_it():
    inv = _read("web_dashboard", "services", "inventory_service.py")
    assert "def _certlab_item(" in inv
    assert '"kind": "certlab"' in inv
    assert "items.append(_certlab_item(row))" in inv
    # Unconditional, like the POV rows: turning the feature off hides the page, it does
    # not delete the pool, and a pool nobody can see still bills.
    assert "db.query(CertLab)" in inv


def test_the_timer_is_stamped_at_provision_because_null_means_never():
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    assert "expiry_policy.default_expiry_for_kind(INVENTORY_KIND)" in svc


def test_teardown_clears_the_timer_in_the_same_transaction():
    # At-most-once: without this the next sweep pass enqueues a second destroy for a row
    # already being destroyed.
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    block = svc.split("def start_decommission(")[1].split("\ndef ")[0]
    assert "row.expires_at = None" in block


def test_a_failed_teardown_does_not_re_arm_the_timer():
    # Re-arming would have the sweep retry a destroy that already failed once, on a loop,
    # silently. A half-destroyed pool needs a human.
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    block = svc.split("async def run_decommission(")[1]
    tail = block.split("except Exception as exc:")[1]
    assert "expires_at" not in tail


# ── the preview flag ──────────────────────────────────────────────────────────

def test_the_gcp_project_defaults_to_the_dashboards_own_credential():
    # Three surfaces, and a miss on any one of them fails quietly: the service fallback is
    # what actually works, the API field is what the form reads, and the prefill is what
    # makes the value visible. Losing only the prefill leaves an operator typing a project
    # id that was already known.
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    block = svc.split('elif cloud == "gcp":')[1].split("    else:")[0]
    # The same two keys, in the same order, that gcp_env() reads to build GOOGLE_PROJECT.
    assert '_cfg("gcp_project") or _cfg("gcp_project_id")' in block
    env = _read("web_dashboard", "services", "terraform_provider_env.py")
    assert '_cfg("gcp_project") or _cfg("gcp_project_id")' in env, \
        "the fallback order must keep matching gcp_env(), or the pool and the credential " \
        "can disagree about the project"

    api = _read("web_dashboard", "api", "cert_lab.py")
    assert '"default_project"' in api
    page = _read("web_dashboard", "templates", "cert_lab", "index.html")
    assert "o.default_project" in page, "the API exposes it but the form never reads it"


def test_the_project_is_still_required_when_nothing_supplies_one():
    # The fallback must not turn a missing project into an empty -var: terraform would
    # fail inside the worker, after the row and the job row already exist.
    svc = _read("web_dashboard", "services", "cert_lab_service.py")
    block = svc.split('elif cloud == "gcp":')[1].split("    else:")[0]
    assert "raise CertLabError" in block
    assert "project-scoped" in block


def test_the_sandbox_scripts_grant_everything_a_ca_build_needs():
    """Both twins, because a CA build needs more than `privateca.admin`.

    The module creates the plugin's enrollment service account and a key for it, so a
    build with only `privateca.admin` gets the pool and then fails on
    `iam.serviceAccounts.create` — an IAM error that names neither the script nor the
    Certificate Lab. Live-observed 2026-09-09, on a project the script had already set up.
    """
    failures = []
    for parts in (("scripts", "sandbox", "Linux", "setup-gcp.sh"),
                  ("scripts", "sandbox", "Windows", "Setup-GcpSandbox.ps1")):
        src, name = _read(*parts), parts[-1]
        if "privateca.googleapis.com" not in src:
            failures.append(f"{name}: does not enable privateca.googleapis.com")
        for role in ("roles/privateca.admin", "roles/iam.serviceAccountAdmin",
                     "roles/iam.serviceAccountKeyAdmin"):
            if role not in src:
                failures.append(f"{name}: does not grant {role}")
    assert not failures, "a CA build would fail on:\n  " + "\n  ".join(failures)


def test_it_ships_as_a_preview_flag_alongside_workload_credentials():
    setup = _read("web_dashboard", "api", "setup.py")
    flags = setup.split("_PREVIEW_FLAGS = {")[1].split("\n}")[0]
    assert '"cert_lab_enabled"' in flags
    assert '"workload_credentials_enabled"' in flags, "sanity: the reference preview flag"
    # And its config panel is reachable from the flag's Configure link.
    assert '"cert_lab_enabled": "cert_lab"' in setup
    assert '"cert_lab": CertLabFeatureConfig' in setup
    assert '"cert_lab"' in setup.split("_CONFIG_ONLY_FEATURES = {")[1].split("}")[0]


def test_the_flag_gates_the_router_the_page_and_the_nav():
    main = _read("web_dashboard", "main.py")
    assert 'app.include_router(cert_lab_api.router,' in main
    assert main.count('_feature_gate("cert_lab_enabled")') >= 2, "router AND page"
    assert '@app.get("/cert-lab"' in main
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    assert "{% if cert_lab_enabled %}" in nav
    flags = _read("web_dashboard", "services", "feature_flags.py")
    assert '"cert_lab_enabled"' in flags


def test_the_page_carries_the_preview_badge():
    page = _read("web_dashboard", "templates", "cert_lab", "index.html")
    assert ">Preview</span>" in page
    # Every call goes through window.API, which attaches the bearer token. A bare fetch
    # would be anonymous and 401 -- there is no auth cookie on this app. Comment lines are
    # stripped first: the page explains that rule in a comment, and matching the
    # explanation instead of the code is how this assertion fires on the wrong thing.
    code = chr(10).join(l for l in page.splitlines()
                        if not l.strip().startswith("//"))
    assert "fetch(" not in code
    assert "API.get(" in code and "API.post(" in code and "API.del(" in code


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
