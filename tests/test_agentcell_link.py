"""A link records accountability. It must never become a consumption.

An agent can be made *answerable for* one Workload Lab credential. That is a governance
record, and the failure mode is that it starts reading — or behaving — like a capability.
It cannot be one: the Cloud tab's credential is returned to nobody, and the Kubernetes
and Certificate tabs vault theirs where a consumer needs a Password Safe client to reach
them. A worker able to fetch any of those would already be holding the standing secret
this whole cell argues against.

So the assertions here are mostly about what the link does NOT do:

  * **No credential crosses into the agent.** Not onto the row, not into the response.
  * **The refusal for an unwired tab names the structural reason**, rather than reading
    as work somebody forgot to finish — an operator who tries `kubernetes` is asking a
    reasonable question.
  * **One link at a time.** An agent answerable for three credentials is the shape the
    cell argues against, so widening is a decision rather than an accumulation.
  * **An expired lease is the mechanism working.** `workload_cloud_service.lease_state`
    exists to keep "expired" and "broken" apart; rendering the first as the second would
    make a correctly-behaving identity look faulty most of the time.

Runs under pytest, or standalone:
    python tests/test_agentcell_link.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-link")

_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_DOC = os.path.join(_ROOT, "docs", "profiles", "demo", "agent-demo-cell.md")
_SVC = os.path.join(_ROOT, "web_dashboard", "services", "agentcell_service.py")
_TAB = os.path.join(_ROOT, "web_dashboard", "templates", "workload_lab",
                    "_agent.html")

from web_dashboard.services import agentcell_service as A  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


# -- what can be linked, and what the refusals say -----------------------------

def test_cloud_became_spendable_and_the_reversal_is_recorded():
    """The third time a mechanism crossed from accountability to capability, and the one
    whose superseded claim was not wrong — it was about something else.

    The comment used to say the Cloud tab's credential "is returned to nobody by design,
    so nothing here can spend it". **That was always a statement about the DASHBOARD**,
    and it is still true of the dashboard: no route here returns a cloud credential. It
    was being read as a statement about the mechanism. The worker calls `generate` on the
    dynamic secret itself, which is exactly what preserves the dashboard's abstinence —
    the thing in Workload Credentials' audit log is now the worker.

    So all three are spendable today. The two sets STILL have different names, for the
    reason `EPISODE_MECHANISMS` gives: the next mechanism will be linkable before it is
    spendable, which is the order all three of these arrived in.
    """
    assert A.LINKABLE_MECHANISMS == ("cloud", "kubernetes", "certificates"), \
        "the linkable set changed; the refusals below and the docs must change with it"
    assert set(A.SPENDABLE_MECHANISMS) == set(A.LINKABLE_MECHANISMS)
    for tab in A.LINKABLE_MECHANISMS:
        assert A.link_problem(tab) == ""
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "agentcell_service.py"))
    assert "SPENDABLE_MECHANISMS = (" in svc and "LINKABLE_MECHANISMS = (" in svc, \
        ("one set is now derived from the other. They coincide today by coincidence, "
         "not by definition, and deriving deletes the distinction at the moment it "
         "stopped being visible")
    # Same rule the other two reversals follow: the superseded sentence may be quoted,
    # but never left standing as the last word. The phrase appears twice — once in the
    # list of what each tab's original refusal was, once in the correction — so the
    # check is that the correction comes AFTER the first quotation, not that it sits
    # within some window of it.
    if "returned to nobody" in svc:
        assert "ALWAYS ABOUT THE DASHBOARD" in svc, \
            ("the service states the superseded conclusion with nothing marking it as "
             "superseded — an operator reading it would believe it")
        assert svc.index("ALWAYS ABOUT THE DASHBOARD") > svc.index("returned to nobody"), \
            "the correction is filed above the claim it corrects"


def test_the_stale_unreachable_reasoning_is_gone():
    """The refusal used to tell every unwired tab that a consumer "needs a Password Safe
    client to reach it — another credential". That stopped being true when the worker got
    `--token-source ps`: it reaches Password Safe holding nothing. A refusal repeating it
    would send an operator looking for a barrier that was removed two PRs ago."""
    for src in (A.link_problem("certificates"), A.link_problem("banana")):
        assert "another credential" not in src, \
            "a refusal still claims the worker would need a second credential"
    # The superseded conclusion may still APPEAR — the comment quotes it to record that
    # it was reversed, and that record is worth keeping. What it must never do is stand
    # alone as current. So wherever the phrase is, the correction has to be with it.
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "agentcell_service.py"))
    if "cannot SPEND any of them" in svc:
        para = svc[max(0, svc.index("cannot SPEND any of them") - 600):
                   svc.index("cannot SPEND any of them") + 600]
        assert "It did not" in para or "earlier version" in para.lower(), \
            ("the service states the superseded conclusion with nothing marking it as "
             "superseded — an operator reading it would believe it")


def test_certificates_became_linkable_and_the_old_refusal_is_gone():
    """That refusal said the tab "writes a PKCS#12 into Secrets Safe rather than a
    managed-account password". Half of a certificate identity IS a managed-account
    password — the passphrase — and the worker could always reach it. The gap was the
    bundle alone, and Secrets Safe being part of Password Safe means the same client pair
    opens it."""
    assert A.link_problem("certificates") == ""
    assert "certificates" in A.SPENDABLE_MECHANISMS
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "agentcell_service.py"))
    if "rather than a managed-account" in svc:
        near = svc[max(0, svc.index("rather than a managed-account") - 500):
                   svc.index("rather than a managed-account") + 500]
        assert "imprecise" in near or "The gap was the BUNDLE" in near, \
            ("the superseded refusal is quoted with nothing marking it as superseded — "
             "an operator reading it would believe it")


def test_the_certificate_notes_lead_with_what_revocation_cannot_do():
    """The limit easiest to assume away, and the one this episode exists to show:
    revoking the certificate does not stop the agent, because nothing on the path checks
    a CRL or OCSP."""
    notes = " ".join(A.cert_link_notes("svc-deploy-pipeline", "cert/sys/acct",
                                       "svc-deploy-pipeline"))
    assert "will not stop the agent" in notes
    assert "CRL" in notes and "OCSP" in notes
    assert "expires" in notes, "the notes do not say what DOES stop it"


def test_spire_is_refused_because_it_is_already_the_agents_identity():
    msg = A.link_problem("spire")
    assert msg and "SPIFFE" in msg, \
        "the spire refusal does not explain that the agent is already attested"


def test_nonsense_is_refused_and_lists_what_is_linkable():
    msg = A.link_problem("banana")
    assert msg and "cloud" in msg


# -- one at a time -------------------------------------------------------------

def test_an_agent_with_a_link_refuses_a_second():
    class Row:
        linked_mechanism = "cloud"
    msg = A.already_linked_problem(Row())
    assert msg, "an agent was allowed to accumulate a second credential link"
    assert "Unlink" in msg, "the refusal does not say how to proceed deliberately"


def test_an_unlinked_agent_is_accepted():
    class Row:
        linked_mechanism = None
    assert A.already_linked_problem(Row()) == ""


# -- the notes lead with what the link is not ----------------------------------

def test_the_notes_lead_with_the_capability_and_name_the_meter():
    """This test used to assert the opposite, and the inversion is the change.

    When the link was accountability only, the thing most likely to be misread was a
    governance record reading as a capability — so the first note said "it does not give
    the worker the credential". It does now. The two things an operator will not think
    to ask about are that it BILLS and that this dashboard does not decide what the
    credential may do, so both have to be said without being asked.
    """
    notes = A.link_notes("aws", revocable=False, dynamic_name="ci-aws")
    assert notes, "linking says nothing back"
    first = notes[0].lower()
    assert "mint" in first, \
        "the first note does not say the agent can now mint its own credential"
    assert "audit log" in first, \
        "the first note does not say where the issuance is recorded, which is the point"
    joined = " ".join(notes).lower()
    assert "billed" in joined or "bills" in joined, (
        "the notes never say this link costs money to spend — it is the only one in the "
        "cell that does, and a worker looping on it is a cost problem first")
    assert "ci-aws" in " ".join(notes), "the notes do not name the dynamic secret"
    assert "does not decide" in joined or "cannot widen" in joined, (
        "the notes imply this dashboard scopes the credential. It does not — the "
        "dynamic secret's own definition does, and that is the honest difference from "
        "the Kubernetes and Certificate tabs")


def test_a_non_revocable_cloud_says_so_plainly():
    notes = " ".join(A.link_notes("aws", revocable=False)).lower()
    assert "cannot be revoked" in notes, \
        "an unrevocable lease does not say so; somebody would promise a revoke"
    assert "ttl" in notes, "the note never says what the only remaining control is"


def test_a_revocable_cloud_does_not_overclaim():
    """The overclaim moved rather than went away.

    It used to be that a revoke changed nothing about the agent, because nothing
    consumed the credential. Now something does — and the new overclaim available is
    assuming a release stops a credential already in use. It does not: releasing the
    lease deletes the service principal's secret, and an access token already issued
    lives out its own hour. The same shape as the cluster episode's "the approval gates
    retrieval, not use", and it has to be said at link time rather than discovered.
    """
    notes = " ".join(A.link_notes("azure", revocable=True)).lower()
    assert "cannot be revoked" not in notes
    assert "another" in notes and "already issued" in notes, (
        "the revocable note implies a release stops a credential already in use. It "
        "ends the ability to get another one; it does not withdraw the current one")


# -- an expired lease is the mechanism working ---------------------------------

def test_an_expired_lease_does_not_read_as_a_fault():
    out = A.link_summary("cloud", "expired").lower()
    assert "expired" in out
    assert "working" in out, (
        "an expired lease renders as a bare failure. workload_cloud_service.lease_state "
        "exists to keep 'expired' and 'broken' apart, and this is where that is shown")


def test_the_other_lease_states_render():
    assert "live" in A.link_summary("cloud", "live")
    assert A.link_summary("", "live") == "", "an unlinked agent claims a summary"


# -- nothing secret moves ------------------------------------------------------

def test_the_row_gains_no_credential_column():
    block = _read(_DB).split("class AgentCell(Base):", 1)[1].split("\nclass ", 1)[0]
    cols = set(re.findall(r"^\s{4}(\w+)\s*=\s*Column", block, re.M))
    for required in ("linked_mechanism", "linked_credential_id", "linked_at"):
        assert required in cols, f"AgentCell no longer records {required!r}"
    for banned in ("linked_credential", "linked_secret", "lease_value", "credential"):
        assert banned not in cols, f"AgentCell has a {banned!r} column"


def test_the_link_route_never_reads_a_credential_value():
    body = _code(_API).split("def link_agent(", 1)[1].split("def unlink_agent(", 1)[0]
    for banned in ("values", "secret", "credential_value", "get_credential"):
        assert banned not in body, \
            f"the link route touches {banned!r} — it must move no credential"
    assert "lease_state" in body, "the link route does not report the lease's state"


def test_unlinking_touches_neither_side():
    # Bounded at the next def. An unbounded slice runs to end of file and picks up
    # revoke_agent, whose whole job IS to revoke -- the first draft of this test tripped
    # on exactly that.
    body = _code(_API).split("def unlink_agent(", 1)[1].split("\n@router.", 1)[0]
    for banned in ("revoke", "start_revoke", "terminate"):
        assert banned not in body, (
            f"unlinking touches {banned!r}. It should drop the association only — the "
            "lab's row keeps its own lifecycle")


def test_the_api_is_reachable_and_paired():
    import warnings
    warnings.filterwarnings("ignore")
    from web_dashboard.main import app
    paths = {r.path for r in app.routes if "agentcell" in getattr(r, "path", "")}
    assert "/api/agentcell/agent/{agent_id}/link" in paths, "no link route is mounted"


# -- the page says it too ------------------------------------------------------

def test_the_page_distinguishes_accountability_from_capability():
    """This replaces an assertion that the page said a link "is not a consumption".
    That was true of every tab once and is now true of only one, so the flat claim had
    to go — but the distinction it protected matters MORE now, not less: a `cloud` link
    that read as capability, or a `kubernetes` link that read as a mere record, would
    both mislead."""
    doc = _read(_DOC)
    assert "accountability only" in doc and "a capability" in doc, \
        "the page does not distinguish a link that confers access from one that does not"
    assert "returned to nobody" in doc, \
        "the page never says why the cloud tab's credential cannot reach the worker"
    assert "cannot authorise its own access" in doc, \
        "the page does not state the beat the kubernetes link exists for"


def test_the_page_states_what_the_approval_does_not_gate():
    """The limit somebody will assume away. The approval gates RETRIEVAL; a token already
    released lives out its TTL, because rotation does not revoke."""
    doc = _read(_DOC)
    assert "gates retrieval, not use" in doc
    assert "rotation does not revoke" in doc.lower()
    assert "ServiceAccount" in doc, \
        "the page does not name the only hard kill switch"



# -- the tab has to offer what the route accepts --------------------------------

def test_the_options_route_populates_every_linkable_mechanism():
    """`build_options` seeds `credentials` from LINKABLE_MECHANISMS and then fills the
    keys one branch at a time, so adding a mechanism to that tuple emits an EMPTY list
    under its name rather than a failure. An empty list is indistinguishable on the page
    from a tab with no rows -- "That tab has no rows to link. Create one there first." --
    so the mechanism is linkable by the API and unreachable from the UI."""
    body = _code(_API).split("def build_options(", 1)[1].split("def link_agent(", 1)[0]
    for mechanism in A.LINKABLE_MECHANISMS:
        assert f'credentials["{mechanism}"].append(' in body, (
            f"/options never populates credentials[{mechanism!r}], so the picker offers "
            f"an empty list for a mechanism the link route accepts")


def test_the_certificates_listing_borrows_the_certificate_tabs_visibility_rule():
    """Creator-scoped for a non-admin, which is NOT this router's own rule. A CA a
    caller cannot see on the Certificates tab must not become selectable here, and
    restating the looser workgroup-or-creator rule would disclose rows that tab does
    not."""
    body = _code(_API).split("def build_options(", 1)[1].split("def link_agent(", 1)[0]
    assert "from .cert_lab import _visible" in body,         "the certificates listing does not reuse the Certificate tab's visibility rule"
    assert "_ca_visible(row, current_user)" in body
    assert 'cert_lab_enabled' in body,         "the certificates listing is not gated on the Certificate Lab's own flag"


def test_the_picker_offers_every_linkable_mechanism():
    """The failure this catches is silent in both directions: the route accepts
    `certificates` and the tab's `mechanisms()` listed two, so the link could be made by
    curl and not by anybody using the page it was built for."""
    tab = _read(_TAB)
    keys = set(re.findall(r"\{ key: '([a-z]+)'", tab))
    for mechanism in A.LINKABLE_MECHANISMS:
        assert mechanism in keys, (
            f"the Agent tab's mechanisms() does not offer {mechanism!r}, so that link "
            f"cannot be made from the UI at all")


def test_the_link_form_carries_the_fields_the_route_takes():
    """A CA row is not an identity -- it carries one managed account per identity and
    this dashboard tracks none of them individually -- so the three fields
    `AgentCellLinkRequest` gained are how the link names which one. Without them the
    route takes its blanks and returns notes promising an empty account and an empty
    bundle."""
    tab = _read(_TAB)
    for field in ("account_name", "bundle_title", "expect_cn",
                  "cloud_scope", "cloud_deny_probe"):
        assert f'x-model="linkForm.{field}"' in tab, (
            f"the link modal has no field for {field!r}, which the link route accepts "
            f"and the notes render")
    assert "linkReady()" in tab,         "the Link button does not require the identity a CA row cannot supply"
    # The whole form is posted, so a field bound in the template and not DECLARED on
    # the request model is silently dropped by pydantic — and the note it was meant to
    # shape comes back generic, which reads as the feature working.
    from web_dashboard.models.agentcell import AgentCellLinkRequest
    declared = set(AgentCellLinkRequest.model_fields)
    bound = set(re.findall(r'x-model="linkForm\.([a-z_]+)"', tab))
    assert bound <= declared, (
        f"the link modal binds {sorted(bound - declared)}, which AgentCellLinkRequest "
        f"does not declare — pydantic will discard them without an error")


# -- the episode the dashboard does NOT own ------------------------------------

def test_spendable_is_not_the_episode_gate():
    """The one that shipped broken, and it now has TWO live instances rather than one.

    `certificates` is spendable with no episode route here, so a panel gated on
    spendability offered a Request-access button whose handler posts at /k8s-request --
    which looks the credential up in the TOKEN table, misses, and refuses a perfectly
    valid link as one whose credential no longer exists. `cloud` joined it: it is
    spendable now and its episode also runs on the host. Two instances is what turns
    "named rather than derived" from a precaution into a rule.
    """
    host_side = {"certificates", "cloud"}
    assert host_side <= set(A.SPENDABLE_MECHANISMS)
    assert not (host_side & set(A.EPISODE_MECHANISMS)), (
        f"{sorted(host_side & set(A.EPISODE_MECHANISMS))} now claims a dashboard "
        "episode route — if one was added, the tab must dispatch on "
        "row.linked_mechanism rather than share /k8s-request")
    assert set(A.EPISODE_MECHANISMS).issubset(set(A.SPENDABLE_MECHANISMS)),         "an episode is offered for a mechanism no worker can spend"
    # Every host-side episode has to be able to say WHERE it runs, or the refusal falls
    # back to naming the wrong flag — which is what the certificate answer did to a
    # cloud link before `HOST_EPISODE_FLAGS` existed.
    assert host_side <= set(A.HOST_EPISODE_FLAGS), (
        "a spendable mechanism with no dashboard route has no entry in "
        "HOST_EPISODE_FLAGS, so its refusal cannot name the run")


def test_the_cluster_route_refuses_a_certificate_link_by_name():
    """Not with the Kubernetes lookup's 404. That message accuses a valid link of having
    lost its credential and sends the operator to unlink the one thing that was right."""
    row = type("Row", (), {"linked_mechanism": "certificates", "episode_state": ""})()
    msg = A.cluster_episode_mechanism_problem(row)
    assert msg, "a certificates link is accepted for a cluster-access request"
    assert "cert-episode" in msg,         "the refusal does not say where that episode actually runs"
    assert "no longer exists" not in msg and "Unlink" not in msg,         "the refusal still points the operator at undoing a valid link"
    k8s = type("Row", (), {"linked_mechanism": "kubernetes", "episode_state": ""})()
    assert A.cluster_episode_mechanism_problem(k8s) == ""
    unlinked = type("Row", (), {"linked_mechanism": "", "episode_state": ""})()
    assert A.cluster_episode_mechanism_problem(unlinked) == "",         "an unlinked agent gets this refusal instead of episode_link_problem's, which "        "names the remedy"


def test_the_refusal_happens_before_the_token_lookup():
    """Order is the whole point: the lookup is what produces the misleading 404, so the
    mechanism check has to run first or the message never changes."""
    body = _code(_API).split("def request_cluster_access(", 1)[1]
    body = body.split("def release_cluster_access(", 1)[0]
    guard = body.index("cluster_episode_mechanism_problem")
    lookup = body.index("wks.get_row")
    assert guard < lookup,         "the Kubernetes lookup runs before the mechanism check, so its 404 still answers"


def test_the_tab_says_where_the_certificate_episode_runs():
    """A capability with no button is a dead end unless the page says why. The tab has
    no route to offer, exactly as it has none for the two install playbooks, so it names
    the command and the journal instead."""
    tab = _read(_TAB)
    assert "episodeMechanism(row)" in tab,         "the episode panel is not gated on the mechanism this dashboard has a route for"
    panel = tab.split("Certificate use", 1)
    assert len(panel) == 2, "the tab never mentions the certificate episode"
    panel = panel[1][:1600]
    assert "--cert-episode" in panel,         "the tab does not name the run that spends a certificate link"
    assert "CRL" in panel or "revoking this certificate will not stop" in panel.lower(),         "the tab does not say that revoking the certificate stops nothing"


def test_the_cluster_route_refuses_a_cloud_link_by_name():
    """The certificate refusal's twin, and the reason `HOST_EPISODE_FLAGS` is a table.

    The first version of that refusal hardcoded the certificate answer, so a CLOUD link
    asking for cluster access was told about `--cert-episode` — a correct-sounding
    sentence pointing at the wrong flag, which is worse than a generic one.
    """
    row = type("Row", (), {"linked_mechanism": "cloud", "episode_state": ""})()
    msg = A.cluster_episode_mechanism_problem(row)
    assert msg, "a cloud link is accepted for a cluster-access request"
    assert "--cloud-episode" in msg, \
        "the refusal does not say where the cloud episode actually runs"
    assert "--cert-episode" not in msg, \
        "a cloud link is being told about the certificate episode"
    assert "no longer exists" not in msg and "Unlink" not in msg, \
        "the refusal still points the operator at undoing a valid link"


def test_the_tab_says_where_the_cloud_episode_runs():
    """Second capability with no button, second panel that has to say why. And two
    things a certificate panel never had to carry: the mint is BILLED, and the ending
    is not a revoke."""
    tab = _read(_TAB)
    panel = tab.split("Cloud credential", 1)
    assert len(panel) == 2, "the tab never mentions the cloud episode"
    panel = panel[1][:2000]
    assert "--cloud-episode" in panel, \
        "the tab does not name the run that spends a cloud link"
    assert "metered" in panel or "bills" in panel, \
        "the tab does not say this is the one link that costs money to spend"
    assert "not a revoke" in panel or "cannot be withdrawn" in panel, \
        "the tab implies the credential can be pulled back; on AWS nothing can"


def test_the_page_says_which_authority_each_capability_reaches():
    """The distinction that replaced accountability-versus-capability. A single
    "reaches Password Safe holding nothing" over a cloud link describes the wrong
    mechanism — there is no vault in that chain at all."""
    tab = _read(_TAB)
    assert "capabilityNote(" in tab, \
        "the capability sentence is still one flat string for three mechanisms"
    note = tab.split("capabilityNote(row) {", 1)[1][:900]
    assert "Workload Credentials" in note and "Password Safe" in note, \
        "the capability note does not distinguish the two authorities"
    assert "billed" in note or "metered" in note, \
        "the cloud branch of the capability note does not mention the meter"


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
