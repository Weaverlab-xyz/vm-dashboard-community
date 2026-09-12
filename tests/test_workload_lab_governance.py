"""Every Workload Lab tab onboards its machine identity to Password Safe.

**This is the cross-cutting invariant of the whole feature**, and the reason it gets a file
of its own rather than an assertion inside each tab's suite: the Workload Lab exists to
demonstrate *governance of credentials used by machine identities* — a token or a
certificate, it does not matter which — and a tab that stands up an identity nothing
governs is a demo of the underlying technology instead. The SPIRE tab was exactly that for
a while: it built a trust domain and handed the operator a list of values to paste.

So these assert, for all three tabs at once:

  * each has a Password Safe onboarding path that WRITES (not a read-only list of strings);
  * each records the managed system it created, and can remove it;
  * none of them writes a credential onto its own row;
  * a FOURTH tab cannot ship without one, because the roster here is derived from the
    template directory rather than hand-listed.

That last point is what makes this more than three assertions. `_TABS` is built by reading
`templates/workload_lab/`, so adding `_vault.html` without an onboarding path fails here.

Runs under pytest or standalone:  python tests/test_workload_lab_governance.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TAB_DIR = os.path.join(_ROOT, "web_dashboard", "templates", "workload_lab")


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _code(path) -> str:
    """A module's source with comments and docstrings stripped.

    Every assertion below that looks for the ABSENCE of something needs this: these modules
    explain at length why they avoid particular calls, so the prose contains every name and
    a raw substring search finds the explanation rather than a call. Two earlier tests on
    this feature had to learn that the hard way.
    """
    src = _read(*path)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


def _tabs() -> list:
    """The tab slugs, read off disk rather than hand-listed. See the module docstring."""
    return sorted(f[1:-5] for f in os.listdir(_TAB_DIR)
                  if f.startswith("_") and f.endswith(".html"))


# Per tab: the service that owns its Password Safe onboarding, and the call that WRITES.
# A tab absent from here fails `test_every_tab_is_accounted_for`, which is the point.
_ONBOARDING = {
    "certificates": {
        "service": ("web_dashboard", "services", "cert_lab_service.py"),
        "writes": "cert_ps_service.register(",
        "records": "row.ps_system_id",
        "removes": "cert_ps_service.deregister(",
    },
    "spire": {
        "service": ("web_dashboard", "services", "spire_lab_service.py"),
        "writes": "ps_resource_service.register_managed_system(",
        "records": "row.ps_system_id",
        "removes": "ps_resource_service.deregister(",
    },
    "kubernetes": {
        "service": ("web_dashboard", "services", "workload_k8s_service.py"),
        "writes": "ps_resource_service.register_managed_system(",
        "records": "row.ps_system_id",
        "removes": "ps_resource_service.deregister(",
    },
}


def test_every_tab_is_accounted_for():
    """The roster is derived, so a new tab has to be added here deliberately.

    A fourth Workload Lab tab that governs nothing would otherwise ship silently — and
    "nothing governs it" is not visible from the page, which looks complete either way.
    """
    found = _tabs()
    assert found, f"no tab partials found in {_TAB_DIR}"
    missing = [t for t in found if t not in _ONBOARDING]
    assert not missing, (
        f"Workload Lab tab(s) {missing} have no Password Safe onboarding recorded here. "
        f"Every tab must govern the identity it creates — that is what the page is for. "
        f"Add the tab to _ONBOARDING with the call that writes its managed system.")
    stale = [t for t in _ONBOARDING if t not in found]
    assert not stale, f"_ONBOARDING names tab(s) that no longer exist: {stale}"


def test_every_tab_writes_a_managed_system():
    """A WRITE, not a list of strings to paste. The SPIRE tab was read-only for a while,
    which is the regression this exists to prevent."""
    for tab, spec in _ONBOARDING.items():
        code = _code(spec["service"])
        assert spec["writes"] in code, (
            f"the {tab} tab's service never calls {spec['writes']} — it does not onboard "
            f"its identity to Password Safe, so the lab demonstrates the technology rather "
            f"than governance of it")


def test_every_tab_records_and_can_remove_what_it_created():
    """Recording the id is what makes the row a governance record; being able to remove it
    is what stops the dashboard from creating objects nothing can clean up."""
    for tab, spec in _ONBOARDING.items():
        code = _code(spec["service"])
        assert spec["records"] in code, (
            f"the {tab} tab does not record the managed system it created")
        assert spec["removes"] in code, (
            f"the {tab} tab cannot remove its Password Safe objects — every create needs a "
            f"destroy, and the state to destroy from")


def test_no_tab_stores_a_credential_on_its_row():
    """The rule all three models state. Ids and titles NAME the credential; Password Safe
    and Secrets Safe hold it.

    Checked on the COLUMN DECLARATIONS, not the class body: all three docstrings explain at
    length why there is no credential column, so scanning the prose finds the very words it
    promises the absence of.
    """
    db = _read("web_dashboard", "database.py")
    for model in ("CertLab", "SpireLab", "WorkloadK8sToken"):
        start = db.index(f"class {model}(Base):")
        end = db.index(chr(10) + "class ", start + 10)
        columns = [ln.strip() for ln in db[start:end].splitlines() if "= Column(" in ln]
        assert columns, f"{model}: no column declarations found"
        declared = "\n".join(columns).lower()
        for banned in ("password", "kubeconfig", "bearer", "private_key", "pkcs12"):
            assert banned not in declared, (
                f"{model} declares a column matching {banned!r} — the row names the "
                f"credential, it does not hold it")


# ── the SPIRE tab's onboarding specifically ───────────────────────────────────

def test_the_spire_onboarding_creates_no_managed_account():
    """The plugin DISCOVERS its accounts, each one a SPIRE registration entry.

    An account created here would sit beside the discovered ones, indistinguishable, and
    move the count that is the lab's whole assertion — eleven entries seeded, eight
    discovered. That count caught a real plugin bug (discovery filtering on the mintable
    prefix and silently returning two), so moving it retires the assertion that found it.
    """
    from web_dashboard.services import ps_resource_service as ps
    hcl = ps._generate_managed_system_hcl(
        name="spire-weaverlab-test", host_name="weaverlab.test", ip_address="10.0.0.10",
        port=8081, functional_account_id=1, platform_id=2, workgroup_id="1",
        entity_type_id=1, ssh_key_enforcement_mode=2, managed_account_name="unused",
        method="spiffesvid", emit_private_key=False, emit_account=False)
    assert 'resource "passwordsafe_managed_system_by_workgroup"' in hcl
    assert "passwordsafe_managed_account" not in hcl, (
        "the spiffesvid HCL renders a managed account — it would be indistinguishable from "
        "a discovered registration entry and would move the discovery count")
    assert 'output "managed_account_id"' not in hcl, (
        "an output referencing a resource that is not rendered fails at plan time")
    # AND that the spiffesvid path actually passes the flag. The assertion above proves the
    # builder honours `emit_account`; it says nothing about whether `register_managed_system`
    # sets it, and flipping that line left this test green. Checked on the branch's own call
    # text, because the real function is async and runs terraform.
    src = _read("web_dashboard", "services", "ps_resource_service.py")
    branch = src[src.index('elif method == "spiffesvid":'):]
    branch = branch[:branch.index('elif method == "password":')]
    assert "emit_account=False" in branch, (
        "the spiffesvid branch does not pass emit_account=False, so onboarding a trust "
        "domain creates a managed account beside the discovered registration entries")
    assert "emit_private_key=False" in branch, (
        "the spiffesvid branch pushes a private key — the administrative PKCS#12 belongs "
        "in the functional account's DSS field, which this dashboard never reads")


def test_no_method_declares_a_terraform_var_it_does_not_use():
    """A declared-but-unset required var fails apply under TF_INPUT=0, so `emit_account`
    had to remove the password var with the account. Checked for EVERY method, because the
    flag is new and the two var emissions are now conditional on different things."""
    from web_dashboard.services import ps_resource_service as ps
    cases = [("ssh", {}), ("ssm", {"emit_private_key": False}),
             ("certificate", {"emit_private_key": False}),
             ("spiffesvid", {"emit_private_key": False, "emit_account": False})]
    for method, extra in cases:
        hcl = ps._generate_managed_system_hcl(
            name="x", host_name="h", ip_address="10.0.0.5", port=22,
            functional_account_id=1, platform_id=2, workgroup_id="1", entity_type_id=1,
            ssh_key_enforcement_mode=2, managed_account_name="adminuser",
            method=method, **extra)
        for var in ("ps_account_password", "ps_account_private_key"):
            declared = f'variable "{var}"' in hcl
            used = f"var.{var}" in hcl
            assert declared == used, (
                f"{method}: {var} declared={declared} but used={used} — a declared-but-"
                f"unset required var fails apply under TF_INPUT=0")


def test_the_spire_tab_reports_what_it_cannot_do():
    """Two steps still need a human, and both make the plugin fail an ACTION while the
    managed system looks correctly onboarded — so both failures read as a credential
    problem. Reported through `onboarding_gaps` rather than left in a docstring, because a
    green tick that overstates what happened is worse than no tick.

    The attribute is the one that matters most: the plugin takes its whole configuration
    from BeyondInsight attributes, no attribute API exists in this codebase, and whether
    the gateway populates them for a plugin action has never been observed. Writing a
    writer on that would be betting on the answer the lab was built to find.
    """
    from web_dashboard.services import spire_lab_service as svc

    class _Row:
        admin_spiffe_id = "spiffe://weaverlab.test/admin"
        trust_domain = "weaverlab.test"
        admin_secret_folder = "spire/weaverlab"

    gaps = svc.onboarding_gaps(_Row())
    assert len(gaps) >= 2, gaps
    # Matched on `what` specifically, not on the three fields joined. The joined form
    # passed when `what` was renamed to something else entirely, because the remedy still
    # happened to mention the attribute — so it asserted that the words appear SOMEWHERE
    # rather than that the gap is named.
    whats = " ".join(g["what"] for g in gaps).lower()
    assert "functional account" in whats, (
        f"no gap is NAMED for the functional account; got {[g['what'] for g in gaps]}")
    assert "spiffetrustdomain" in whats, (
        f"no gap is NAMED for the attribute; got {[g['what'] for g in gaps]}")
    for gap in gaps:
        for key in ("what", "why", "remedy"):
            assert gap.get(key), f"gap {gap.get('what')!r} has no {key}"
    # And the page renders them rather than its own wording.
    tab = _read("web_dashboard", "templates", "workload_lab", "_spire.html")
    assert "onboard.gaps" in tab, (
        "the SPIRE tab does not render the gaps, so it can claim the button does more than "
        "it does")
    assert "govern(lab)" in tab, "there is no button to onboard with"


def test_the_spire_job_type_is_registered_in_all_three_places():
    worker = _read("web_dashboard", "jobs_worker.py")
    assert worker.count('"spirelab_ps_register"') >= 3, (
        "spirelab_ps_register must be in the handled-types tuple, a tier tuple and the "
        f"dispatch chain; found {worker.count(chr(34) + 'spirelab_ps_register' + chr(34))}")
    assert 'job_type == "spirelab_ps_register"' in worker
    assert "spire_lab_service.run_ps_register(" in worker
    api = _read("web_dashboard", "api", "spire_lab.py")
    assert '"/{lab_id}/ps-register"' in api and "start_ps_register(" in api


# ── the k3s link teardown ─────────────────────────────────────────────────────

def test_closing_a_lab_unlinks_its_k3s_node():
    """`run_decommission` closed tcp/8081 and touched the k3s node not at all, so Close
    left an agent re-attesting to a server that no longer answers and an API server
    trusting an issuer that no longer resolves. Asymmetric with the Kubernetes tab, which
    deletes its ServiceAccount — and an asymmetry an operator meets as a mystery."""
    svc = _read("web_dashboard", "services", "spire_lab_service.py")
    dec = svc[svc.index("async def run_decommission("):svc.index("K8S_LINK_JOB_TYPE =")]
    # The CALL, not the name. "UNLINK_STAGE" also appears in this slice's comments and in
    # the progress label, so the bare name matched while `stage=` was pointed elsewhere.
    assert "stage=UNLINK_STAGE" in dec, (
        "run_decommission does not run the unlink stage, so Close leaves the k3s node's "
        "agent and its authentication-config drop-in behind")
    # Guarded, so a lab with no k3s half is untouched.
    assert 'k8s_status == "linked"' in dec, (
        "the unlink is not guarded on the link existing, so a lab that never had a k3s "
        "node would run a playbook against nothing")
    # Non-fatal: the ACL is the actual kill switch and must close regardless. Scoped to the
    # UNLINK's own try block — `run_decommission` has an outer `except Exception` of its
    # own, which this matched while the inner handler was narrowed to a type nothing raises.
    unlink = dec[dec.index('if row.k8s_status == "linked"'):dec.index("backend = require_backend")]
    assert "except Exception" in unlink, (
        "the unlink's failure is not caught, so a node that is gone or unreachable would "
        "stop the ACL from closing — and the ACL is what stops the server minting")
    assert "closing the ACL" in unlink or "regardless" in unlink, (
        "nothing records that the teardown continued past a failed unlink")


def test_the_unlink_play_removes_the_dropin_first_and_never_fails_on_absence():
    """The drop-in is the only change that can stop the API server, so it goes first. And
    every task tolerates a thing already gone, because a teardown runs on half-built hosts
    by definition — that is when it is needed."""
    import yaml
    path = os.path.join(_ROOT, "examples", "playbooks", "k3s", "k3s-spiffe-unlink.yml")
    assert os.path.exists(path), "the unlink play is missing"
    play = yaml.safe_load(open(path, encoding="utf-8").read())[0]
    tasks = play.get("tasks") or []
    names = [t.get("name") or "" for t in tasks]

    def _idx(needle):
        return next(i for i, n in enumerate(names) if needle.lower() in n.lower())

    assert _idx("drop-in") < _idx("agent"), (
        "the SPIRE agent is removed before the API server's drop-in; the drop-in is the "
        "only change that can stop the API server, so it goes first")
    # The restart is gated on the drop-in having changed, or a re-run disrupts the node.
    restart = next(t for t in tasks if "restart k3s" in (t.get("name") or "").lower())
    assert restart.get("when"), (
        "k3s is restarted unconditionally, so re-running the teardown on an already-"
        "unlinked node disrupts everything on it for no reason")
    # Nothing may hard-fail on an absent resource.
    for t in tasks:
        f = t.get("ansible.builtin.file") or {}
        if f.get("path") and f.get("state") != "absent" and "directory" not in str(f):
            raise AssertionError(
                f"{t.get('name')!r} uses file: with state={f.get('state')!r}; a teardown "
                f"should only ever remove")


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
