# What consumes these credentials

> **Audience:** operator · **Profile:** `demo` · **Read this when:** someone asks what actually spends what the lab issues, or you need to pick the consumer to run in front of a room.

> **Preview.** Two of the consumers here have never run live: the MCP worker (no live SPIRE
> trust domain and no live MCP endpoint) and the `wlc` / `ps` token sources it offers (no
> Workload Credentials tenant, no registered Workload Identity, no federation trust). The
> shipped playbooks have code behind every claim on this page; whether each has been run
> against a live authority is stated per tab, and the register at the end is the honest
> summary.

[Every tab governs what it creates](../workload-lab.md#every-tab-governs-what-it-creates)
is the invariant of the feature. This page is its mirror: **every tab is spent by
something, and the something is a file with a path.**

It needs stating because the record was wrong about it for a while.
[The demo cells we do not have](../design/next-demo-cells.md) argued that the Workload Lab
issued four credentials and that nothing consumed them — calling the candidates the hub
names, *"a pipeline, a broker or a cluster"*, hypothetical. That was an under-reading of
what had already shipped, and it has been corrected three times since. This page is the
register, so the claim cannot go stale again in either direction: it names what spends
each credential, what that consumer has to hold in order to retrieve, and the places where
the answer is still *nothing does*.

---

## Something spends each of the four

| Tab | What spends it | What that consumer holds in order to retrieve |
|---|---|---|
| [Certificates](certificates.md) | `examples/playbooks/certificates/ci-fetch-cert.yml`, against `nginx-mtls-endpoint.yml` | a Password Safe OAuth client, injected into the runner |
| [SPIRE](spiffe.md) | `examples/playbooks/agent/files/mcp_agent.py` | **nothing** — the SVID is re-fetched from the workload API every loop |
| [Kubernetes](kubernetes.md) | `examples/playbooks/k8s/ci-deploy-with-ps-token.yml`, `ci-read-with-ps-token.yml`, and the agent cell's `--k8s-episode` | the plays: a Password Safe OAuth client. The agent: **nothing** |
| [Cloud](cloud.md) | `examples/playbooks/cloud/ci-run-with-dynamic-creds.yml` | a Workload Credentials token, in the workload's own environment |

The third column is the one to read. "A consumer exists" is a low bar — every vault has
consumers. What differs between these four is **how much the consumer must already hold to
get what it is asking for**, and that is the axis the whole feature is arguing along.

---

## The certificate is spent against an endpoint that checks the name

Two plays, and they are halves of one demonstration:

| File | What it is |
|---|---|
| `examples/playbooks/certificates/nginx-mtls-endpoint.yml` | nginx on `:8443` with `ssl_verify_client on`, echoing `$ssl_client_s_dn` |
| `examples/playbooks/certificates/ci-fetch-cert.yml` | fetches both halves, splits the bundle, calls the endpoint |

**The proof is the echo, not the handshake.** `curl` succeeding proves TLS completed. The
endpoint answering `CN=svc-deploy-pipeline` proves the certificate authenticated as the
*right identity* — which is step 6 of
[Proving it works](certificate-lab.md#proving-it-works), and the step demonstrations
usually skip. A certificate that verifies in a console but fails a handshake has proven
nothing.

The consumer fetches **both halves**, which is what makes the
[two-halves split](certificates.md#what-the-plugin-does-in-one-paragraph) visible as a
control rather than an implementation detail: the passphrase from the managed account with
`beyondtrust.secrets_safe.managed_account`, the bundle from a Secrets Safe file secret with
`beyondtrust.secrets_safe.secret`. Retrieving one without the other yields nothing usable.
The `reason` string carries the `build_id`, so the audit trail answers *which build* spent
it.

**What it holds:** `PASSWORD_SAFE_API_URL`, `_CLIENT_ID` and `_CLIENT_SECRET`, injected into
every runner. Not nothing — see the register below.

**What is deliberately not consumed here:** the endpoint's *server* certificate is
self-signed and unrelated to the lab CA, which is why the play uses `curl -k`. Issuing it
from the lab CA would blur which half of the handshake is being demonstrated.

---

## The SVID is spent as evidence, not as an authenticator

This is the honest part of the page and it gets its own heading rather than a footnote.

`examples/playbooks/agent/files/mcp_agent.py` re-fetches its SVID from the SPIRE workload
API on **every loop** and prints it beside the token it spent:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · 14 active jobs, 2 failed today · 14:02:11
```

The worker is installed by `examples/playbooks/agent/agent-install.yml`; its registration
entry comes from `agent-spiffe-entry.yml`.

**The SVID does not authenticate to `/mcp`.** `web_dashboard/api/mcp_server.py` takes a
Bearer PAT and has no mTLS path. Bridging those two — having the SVID mint the PAT — would
need the Password Safe **SPIFFE SVID** plugin, whose configuration question
`services/spire_lab_service.py` records as unresolved. So on the default `file` token
source the SVID is consumed as *proof of who is running* and the PAT is what is actually
spent. The worker prints both on one line precisely so that gap stays visible rather than
being narrated away.

There is one path where the SVID **is** an authenticator: `--identity-platform spire` mints
a JWT-SVID and presents it to Workload Credentials, because
[the lab already publishes the trust domain as an OIDC issuer](spiffe.md#reaching-a-kubernetes-cluster-with-a-jwt-svid).
That is the row that covers a bare-metal host with no cloud underneath it. It has never
been run.

The SPIRE lab's own plays in `examples/playbooks/spire/` stand the trust domain up. They do
not spend an SVID, and this page does not count them as consumers.

---

## The cluster token has three consumers, and they differ in what they hold

`examples/playbooks/k8s/ci-deploy-with-ps-token.yml` and `ci-read-with-ps-token.yml` are
**the first Kubernetes plays in this repo that authenticate with something they fetched
themselves.** The other four in that folder — `deployment-apply.yml`, `helm-install.yml`,
`list-nodes.yml`, `namespace-ensure.yml` — take the kubeconfig the dashboard injects and
supply nothing, which is exactly the property these two remove.

**The refusals are asserted tasks inside the plays, not runbook steps.** Deploying into
another namespace must come back 403; reading a Secret as `reader` must come back 403. A
step in a runbook gets skipped, and an assertion does not — if the refusals do not refuse,
the plays fail.

The third consumer is the agent cell, and what it adds is not *a* consumer but **a consumer
that holds nothing to retrieve with**. `POST /api/agentcell/agent/{id}/k8s-request`
(`web_dashboard/api/agentcell.py`) opens one bounded episode — the **Request access**
button on the lab's own **Agent** tab, once the agent is linked to a Kubernetes token
there — and
`mcp_agent.py --k8s-episode` runs it: it asks, **waits for a person**, runs the same two
probes as steps 3 and 4 of [Proving it works](kubernetes.md#proving-it-works), and checks
the request slot back in. Its chain is workload identity → Workload Credentials → the
Password Safe client pair → `POST Auth/Connect/Token` → `SignAppIn` → `POST Requests` →
`GET Credentials/{id}` → `PUT Requests/{id}/Checkin`.

Exit codes are the punctuation: **0** proved the scope, **3** was never approved, **4**
means a refusal did not refuse — the one outcome that would otherwise look like success.

See [Agent Demo Cell](../profiles/demo/agent-demo-cell.md) for the presenter's version of
this, including what the approval gate does *not* prove.

---

## The cloud credential is spent, and deliberately not minted

`examples/playbooks/cloud/ci-run-with-dynamic-creds.yml` uses a credential minted
beforehand, then asserts the same call is refused once the lease expires — with a **real
wait**, not a faked clock. A play that faked the clock would prove it can print a failure
message, not that the credential died.

**It does not mint, and the second reason is the one that matters.** Issuance is metered,
so a play that minted on every run would bill on every run. More importantly, *the consumer
retrieving with its own token is the point* — a play that minted on the operator's behalf
would put the operator in Workload Credentials' audit log, which is the exact property the
mechanism exists to remove.

Revocation is asymmetric and the play says which case it is in: Azure leases can be
released early, **AWS leases cannot be revoked at all**, so there the TTL is the only
control there is.

**The agent cell does not spend this one.** Linking an agent to a `cloud` credential
(`POST /api/agentcell/agent/{id}/link`) is **accountability only** — that tab's credential
is returned to nobody by design, so no worker can spend it. The link records a lease whose
state is worth reporting beside the agent, and `workload_cloud_service.lease_state` keeps
"expired" and "failed" apart so an expiry reads as the mechanism working rather than as a
fault.

---

## The worker itself is the fifth consumer

The MCP worker spends a dashboard PAT against `/mcp` on a loop, and `--token-source`
decides what sits on the host:

| `--token-source` | What sits on the host |
|---|---|
| `file` (default) | a 0600 file holding the PAT — a static secret, smaller than an env var but still one |
| `wlc` | **nothing.** The platform vouches for the machine; WC serves the PAT from its own store |
| `ps` | **nothing.** WC hands over the Password Safe client pair and the worker *requests* the credential |

Re-running the install play with `wlc` or `ps` **removes** any token a previous `file`
install left behind, so switching is a migration rather than an accumulation.

---

## What is not consumed, and saying so is the point

The section that keeps this page honest. Each of these is true of a named file or setting.

- **`file` is still the default, because none of the holds-nothing paths has run live.** No
  Workload Credentials tenant, no registered Workload Identity, no federation trust. The
  strongest claim on this page is implemented and unproven.
- **The dashboard's own WC client is Azure-only.** Its Entra auth mode calls IMDS and
  nothing else, while the worker offers five identity platforms. The worker is ahead of the
  app — see [Workload Credentials](workload-credentials.md#how-the-dashboard-authenticates).
- **Nothing consumes a subordinate CA.** The bundle format is settled and the suite checks
  the passphrase opens the key, but uploading it to a real PRA, minting a client
  certificate beneath it and connecting is untested — as is which PKCS#8 ciphers PRA's PEM
  parser accepts. See
  [Handing the subordinate to PRA Vault](subordinate-ca.md#handing-the-subordinate-to-pra-vault).
- **The shipped k8s and certificate plays still hold a Password Safe OAuth client** in the
  runner's environment. Only the agent cell removes it, and only on a path that has not
  run. The chain bottoms out somewhere; this moves it, it does not make it vanish.
- **The vault authenticates whoever can retrieve, not the workload.** The agent narrows
  that — what reaches the vault is no longer transferable — and does not close it. Anyone
  who can retrieve *is* the workload, as far as this mechanism can tell. That is the axis
  [the SPIRE path](spiffe.md) wins on and the others do not, which is why both are on the
  same page.
- **No consumer for six of the nine certificate backends.** EST, EJBCA, Vault PKI, step-ca,
  DigiCert ONE and Sectigo are certificate authorities a customer already runs, and there
  is no build path for this lab to give them something to consume against.
- **No consumer reaches a managed cluster over SPIRE.** `--authentication-config` is a
  kube-apiserver flag, so that route reaches a self-managed k3s and **none of EKS, AKS or
  GKE**.
- **ECS cannot hold a workload identity at all.** An ECS task gets SigV4 credentials and a
  signed instance identity document, and neither is an OIDC token. Only EKS projects one.

---

## Two traps every consumer here encodes

Cross-cutting, and worth one heading because the same bug has now been written twice.

**Blank what the runner already has.** The k8s plays blank `K8S_AUTH_KUBECONFIG` and
`KUBECONFIG`; the cloud play blanks `AWS_ACCESS_KEY_ID` and its siblings. Left set, the
client falls back to them: every task passes, the refusal never refuses, the expiry
assertion never fires, and the play reports a successful demonstration of a mechanism it
never touched.

**`failed_when: false`, never `ignore_errors`.** The refusal task has to fail so the *next*
task can judge why. `ignore_errors` also swallows an unreachable API server, a missing CLI
or a broken proxy — and the assertion then passes on a typo. Every assertion here checks
both that the call failed **and** that it failed for the right reason: a 403, or a message
naming an expiry or an auth failure.

---

## Where things live

| | |
|---|---|
| Certificate consumers | [`examples/playbooks/certificates/`](../../examples/playbooks/certificates/README.md) |
| Cloud consumer | [`examples/playbooks/cloud/`](../../examples/playbooks/cloud/README.md) |
| Cluster-token consumers | [`examples/playbooks/k8s/`](../../examples/playbooks/k8s/README.md) — `ci-deploy-with-ps-token.yml`, `ci-read-with-ps-token.yml` |
| The agent cell | [`examples/playbooks/agent/`](../../examples/playbooks/agent/README.md), `files/mcp_agent.py` |
| SPIRE trust domain | [`examples/playbooks/spire/`](../../examples/playbooks/spire/README.md) |
| Episode API | `web_dashboard/api/agentcell.py` |
| Lease and token state | `web_dashboard/services/workload_cloud_service.py`, `workload_k8s_service.py` |
| The MCP surface the worker reads | `web_dashboard/api/mcp_server.py` |
| The design note this page answers | [The demo cells we do not have](../design/next-demo-cells.md) |
