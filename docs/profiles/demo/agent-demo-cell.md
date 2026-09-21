# Agent Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing what governs a thing that acts on its own, and how to stop it.

> **Preview.** The worker has never been run against a live SPIRE trust domain or a real
> MCP endpoint. Unobserved: whether `spire-agent api fetch x509` parses as expected on the
> target release, and whether the MCP SSE client negotiates cleanly through the
> dashboard's ingress. Off by default; turn it on with the **Agent Demo Cell** preview
> toggle in Settings. Work through the
> [E2E verification checklist](#e2e-verification-checklist) before you present it.

The dashboard can install a **non-human principal** onto a Linux VM it already deployed: a
worker that reads the estate through the dashboard's own MCP server on a loop, proves its
identity to a SPIRE trust domain on every pass, and **stops when you revoke its token
while the audience watches**.

That last part is the demo. Everything else is arrangement.

**Where it lives: `/workload-lab`, the `Agent` tab.** Not a page of its own, and the
placement is the argument. Every other tab on that page answers *how does a machine get a
credential* — a certificate, a SPIFFE identity, a Kubernetes token, a short-lived cloud
credential — and each stays a description of a mechanism until something holds one. This
cell is that something: it attests to the **SPIRE** tab's trust domain, and it can be made
answerable for the **Cloud** tab's lease, ask for the **Kubernetes** tab's token, or hold the **Certificate** tab's identity. Turning
the preview on makes the Workload Lab reachable on its own, so the tab cannot be switched
on and then be unfindable.

- **Provisioning** *(stand it up)* — **nothing is created.** The worker attaches to a VM
  this dashboard already deployed, resolved from completed deploy-job rows rather than an
  address anyone supplied. The same call the [SPIRE lab](../../workload-lab/spiffe.md)
  made, for the same reason, with the same payoff: the host keeps its auto-delete timer,
  its Password Safe onboarding and its Destroy button.
- **Layer 1 — PRA** *(reach it)* — **not part of this cell**, and that is worth saying out
  loud. PRA governs sessions a human opens. There is no human in this workflow at all,
  which is the entire point of it.
- **Layer 2 — Password Safe** *(manage its secrets)* — the authority behind the SPIRE
  trust domain that attests the worker, through the SPIFFE SVID plugin.
- **Layer 3 — Entitle** *(grant time-boxed access)* — **where this goes, not what it does
  today.** The agent's token expires and can be revoked, but nothing grants it *through*
  Entitle yet. See [What is not built](#what-is-not-built).

---

## Two credentials, and they are not the same one

This is the shape of the whole cell, and the worker's code is arranged so nobody can miss
it.

| | What it is | Where it lives |
|---|---|---|
| **SVID** | the worker's **identity** — it attests itself | nowhere. Re-fetched from the SPIRE workload API every loop, held in memory |
| **PAT** | the worker's **authorization** to this dashboard | a 0600 file the operator owns; scoped to a user's RBAC, with an expiry the cell always sets, revocable instantly |

Every loop leaves one line carrying both:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · 14 active jobs, 2 failed today · 14:02:11
```

Who it is, what it spent, what it saw.

**The token is named, not shown.** That is the PAT's *name* in the line — the one
**Settings → API Tokens** lists, and the one you are about to revoke. No part of the
credential reaches a log, and an error from the MCP client is scrubbed of anything
token-shaped on its way to one.

## What is not built

> **The SVID does not authenticate to `/mcp`, and nothing here pretends it does.** The MCP
> server takes a Bearer PAT (`api/mcp_server.py`) and has no mTLS path. Bridging *those
> two specifically* — having the SVID mint the PAT — would need the Password Safe
> **SPIFFE SVID** plugin, whose configuration question `spire_lab_service` records as
> unresolved. This cell does not bet on it.
>
> **But the worker still need not hold a static secret**, and that is the part worth
> demoing — see [No static secret on the host](#no-static-secret-on-the-host). The
> identity that removes it federates over **OIDC**, which is a different mechanism from
> the mTLS bridge above. SPIRE can be that issuer — the lab already publishes the trust
> domain as one — and so can any of the three clouds. What stays unbuilt is specifically
> the SVID-to-PAT bridge inside Password Safe, not the workload's ability to prove who it
> is without holding anything.

## No static secret on the host

The worker has three token sources, and two of them store nothing.

| `--token-source` | What sits on the host | Honest name for it |
|---|---|---|
| `file` (default) | a 0600 file holding the PAT | a static secret, smaller than an env var but still a static secret |
| `wlc` | **nothing** | the platform vouches for the machine; Workload Credentials serves the PAT from its own store |
| `ps` | **nothing** | Workload Credentials hands over the Password Safe API client pair, and the worker *requests* the credential from the vault |

In both of the second two, the worker starts the same way:

1. it asks its platform for **its own identity token**;
2. it presents that to **Workload Credentials** in place of a PAT, with
   `X-BT-Service-Name` naming which registered Workload Identity it satisfies.

`wlc` stops there and reads the PAT out of WC. `ps` goes one step further, and that step
is the argument:

3. WC returns the **Password Safe API client id and secret**;
4. the worker signs in to Password Safe with that pair and **requests** the credential —
   `POST Auth/Connect/Token` → `SignAppIn` → `POST Requests` → `GET Credentials/{id}` →
   `PUT Requests/{id}/Checkin`, the same sequence `services/ps_api_service` uses.

Everything the worker is *configured* with — site id, service name, audience, base URL,
the two WC secret **names**, the account id — is **non-secret**.
`services/workload_credentials_service` puts its half plainly: *"Two auth modes, and the
second one stores nothing."*

### Why `ps` is worth the extra hop

Password Safe authenticates an application with a client-credentials pair, so that pair is
a standing credential and always was. The question is **where it lives** — and in `ps` mode
it lives in Workload Credentials, not on this box. WC becomes a *bootstrap for the vault*
rather than a second vault beside it, which is what lets the worker reach anything Password
Safe governs rather than only what was copied into WC.

Everything Password Safe already governs then governs this too. Say it in the room while
the credential is in flight:

- the retrieval is a **recorded request**, with a duration and a reason;
- it can be made to **require approval** — the worker simply waits, and says so;
- the credential behind it **rotates** on its own schedule, and the worker re-requests.

A PAT in a file has none of those properties and never will.

**This is the suite answering its own question.** Password Safe (old) holds and governs the
secret; Workload Credentials (new) brokers the way in against an identity the platform
vouches for; the workload holds nothing. Neither product does that alone, and the seam
between them is the thing worth showing — a competitor with one half cannot.

It also removes the asterisk this cell was carrying. "A non-human principal that holds no
standing credential" is the argument, and a PAT in a file was that argument with a caveat.

### The identity is not Azure-only

Every major cloud federates non-human identities over OIDC. `--identity-platform` picks
which issuer is asked, and one of them is not a cloud at all:

| Platform | Where the token comes from |
|---|---|
| `azure` | IMDS, or `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` where the runtime injects them |
| `gcp` | the metadata server's `instance/service-accounts/default/identity` endpoint |
| `aws` | the projected token at `AWS_WEB_IDENTITY_TOKEN_FILE` (IRSA) or `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` (EKS Pod Identity) |
| `spire` | a JWT-SVID from the SPIRE agent already on this host |
| `file` | any other projected token on disk, such as a Kubernetes ServiceAccount token |
| `auto` | whichever of the above this host declares — it refuses rather than guessing |

Two of these rows are worth saying out loud:

- **AWS has no OIDC endpoint on IMDS.** A plain EC2 instance gets SigV4 credentials and a
  signed identity document, not an OIDC JWT. The projected file is how an AWS non-human
  identity holds one, and it comes from a cluster. On plain EC2, use `spire`.
- **`spire` needs no cloud.** The Workload Lab already publishes the trust domain as an
  OIDC issuer, so a bare-metal host federates on the same mechanism a cloud VM does. That
  is the row that covers the hardware in a rack — the shape the network and OT cells have.

`auto` detects by marker and **refuses rather than guessing** when the host declares
nothing. No file tells a bare Azure VM apart from a bare GCE one, and `169.254.169.254` is
both their metadata addresses; probing would mean a request that has to time out to say no.
Name the platform — it is one word and never wrong.

> **Unproven, so `file` is still the default.** No Workload Credentials tenant, no
> registered Workload Identity and no federation trust has been stood up for this. The
> dashboard's *own* WC client is Azure-only besides (`AUTH_MODE_ENTRA` calls IMDS and
> nothing else) — the worker has all five platforms, the app has one. The `ps` source
> additionally needs an API-enabled managed account with the Requestor role and an access
> policy that auto-releases, the usual out-of-band prerequisites in
> [password-safe.md](../../integrations/password-safe.md). The worker names its token
> source on every line it logs, so which mode is in play is never in doubt.

Re-running the install play with `agent_token_source: wlc` or `ps` **removes** any token a
previous `file` install left behind — "nothing is stored on this host" must not be
contradicted by a file in `/etc`.

## What this agent is answerable for — and what it can ask for

An agent can be **linked** to one Workload Lab credential
(`POST /api/agentcell/agent/{id}/link`), so that "what does this agent have access to" is
one lookup rather than a conversation.

**What the link confers depends on the tab, and conflating the two is the failure mode:**

| Link | What it is | Why |
|---|---|---|
| `cloud` | **accountability only** | That tab's credential *"is returned to nobody"* by design, so no worker can spend it. The link records a lease whose state is worth reporting beside the agent. |
| `kubernetes` | **a capability** | The worker reaches Password Safe holding nothing, so it can genuinely *request* that tab's token — subject to whatever the access policy requires. |
| `certificates` | **a capability, and the one nobody can take away** | The same client pair opens both halves — the PKCS#12 passphrase from the managed account and the bundle from Secrets Safe. A CA row is not an identity, so the link names the managed account and the bundle title. **There is no Request-access button for it:** that episode is one shot on the host, not a route this dashboard owns. |

> **An earlier version of this page said no Workload Lab credential could reach this
> worker without it already holding one.** That stopped being true when the worker gained
> `--token-source ps`: it reaches Password Safe with a workload identity brokered by
> Workload Credentials, holding nothing at all. The Kubernetes tab's own sentence — *"the
> consumer is a program with a Password Safe API client"* — describes this worker.

**One link at a time.** An agent answerable for a cloud lease *and* a cluster token *and*
a certificate would be the most over-credentialed principal in the estate, which is the arrangement this cell
argues against. Unlink before relinking, so widening is a decision rather than an
accumulation.

### What a `cloud` link is good for

- **It says the revoke asymmetry out loud at link time.** Azure leases can be released
  early; **AWS leases cannot be revoked at all**, so the TTL is the only control there is.
  That is the provider's limit rather than this dashboard's, and hearing it when you link
  is better than discovering it when you try to revoke in front of a room.
- **An expired lease reads as the mechanism working**, not as a fault — honouring
  `workload_cloud_service.lease_state`, which exists to keep those two apart.

## The second demo: an agent that cannot authorise its own access

This is the one worth building the room around, and it is a different beat from the
revoke. With a `kubernetes` link in place:

```
POST /api/agentcell/agent/{id}/k8s-request      # open one bounded episode
```

Then, on the host, `mcp_agent.py --k8s-episode`:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · requesting deployer access to https://10.0.0.5:6443 · 14:02:11
[agent] holding nothing: the Password Safe client pair came from Workload Credentials against this machine's own identity · 14:02:11
[agent] spiffe://weaverlab.test/agent/mcp-reader · WAITING for approval (20s) — this agent cannot authorise its own access · 14:02:31
[agent] spiffe://weaverlab.test/agent/mcp-reader · WAITING for approval (40s) — this agent cannot authorise its own access · 14:02:51
[agent] spiffe://weaverlab.test/agent/mcp-reader · approved — a token was released · 14:03:14
[agent] spiffe://weaverlab.test/agent/mcp-reader · scope proved — namespace-scoped: it can list pods in app and is refused in kube-system · 14:03:15
[agent] spiffe://weaverlab.test/agent/mcp-reader · the request was checked back in · 14:03:15
```

**Read the WAITING lines aloud.** That is an AI agent asking a person for access to a
cluster and being unable to proceed until they say yes.

Then the two reads. The success proves the token works; **the 403 proves it is scoped**,
which is the half worth showing — the same two beats the shipped consumer plays assert,
and for the same reason `docs/workload-lab/kubernetes.md` gives: *"a step in a
runbook gets skipped, and an assertion does not."*

| Profile | Succeeds | Must be refused |
|---|---|---|
| `deployer` | list pods in its namespace | the same list in another namespace |
| `reader` | list pods cluster-wide | read a Secret — upstream `view` omits them by design |

Exit codes are the punctuation: **0** proved the scope, **3** was never approved, **4**
means a refusal did not refuse — the one outcome that would otherwise look like success —
and **5** means Password Safe released without consulting anybody, so there was no human
in the loop to demonstrate.

### What this demo does not prove, and you should say so

- **The approval gates retrieval, not use.** In bound mode rotation does not revoke: a
  token already released lives out its TTL whatever happens next. Checking the request
  back in returns the *slot*, not the token. Deleting the ServiceAccount is the only hard
  kill, and it kills every token ever issued to that account.
- **The vault still cannot tell who retrieved.** The agent holds no standing credential to
  ask with, so what reaches Password Safe is not transferable — but *anyone who can
  retrieve is the workload*, as far as this mechanism can tell. That is the axis the SPIRE
  path wins on and this one does not, which is why both exist on the same page.
- **Without an approval policy there is no wait.** The worker no longer lets that pass
  silently — it refuses with exit code 5 rather than printing an approved-looking line.
  Set the managed account's access policy to require approval, or pass
  `--no-require-approval` and say so.

## The third demo: the credential nobody can take away

The three episodes are worth running in order, because the arc teaches what no single one
does:

| Episode | The credential | How you take it away |
|---|---|---|
| the loop | its MCP **PAT** | **revoke it** — the worker stops mid-poll, visibly |
| cluster access | a Workload Lab **token** | **gated at retrieval** — a person decides; once released it lives out its TTL |
| this one | a **certificate** | **neither** |

`docs/workload-lab/certificates.md` states the third position rather than hiding it:

> **No revocation checking.** The plugin consults neither CRLs nor OCSP. Short lifetimes
> are the mitigation, and that is a deliberate design position.

With a `certificates` link in place, `mcp_agent.py --cert-episode`:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · requesting the certificate identity behind svc-deploy-pipeline
[agent] holding nothing: the Password Safe client pair came from Workload Credentials against this machine's own identity
[agent] spiffe://weaverlab.test/agent/mcp-reader · WAITING for approval (20s) — this agent cannot authorise its own access
[agent] spiffe://weaverlab.test/agent/mcp-reader · passphrase released; downloading the bundle from Secrets Safe
[agent] spiffe://weaverlab.test/agent/mcp-reader · identity proved — the endpoint answered 200 and echoed svc-deploy-pipeline
[agent] spiffe://weaverlab.test/agent/mcp-reader · the request was checked back in
```

**Two halves, and neither is usable alone.** That is the tab's design and the agent
honours it: the PKCS#12 **passphrase** is the managed account's credential, fetched
through the same recorded request as the cluster token; the **bundle** it opens is a
Secrets Safe file secret. Retrieving one without the other yields nothing.

Secrets Safe is part of Password Safe, so **both halves come down one session**: the
session that released the passphrase reaches the bundle unchanged. No second sign-in, no
second credential, and nothing on this host that was not there a moment ago.

**Not through `ps-cli`, though it is what the dashboard uses.** It cannot carry these
bytes. [password-safe.md](../../integrations/password-safe.md) establishes it and
`secrets_backend_service` refuses on it: the endpoint returns `application/octet-stream`
faithfully, but every route ps-cli offers decodes the body to text first, so a PEM bundle
survives and **a `.pfx` is corrupted rather than refused**. That is the worst of the three
outcomes — the corruption is silent at the transport and surfaces three steps later, as
what looks like the wrong bundle in Secrets Safe. So the worker calls
`GET Secrets-Safe/Secrets/{id}/file/download` and keeps the bytes, which is what that page
prescribes for exactly this case.

Because the bundle arrives as a **file**, the episode opens its directory before it
fetches anything: the bundle lands there, `openssl` opens it there, and the whole directory
goes at the end. One guarded place, named in the section below rather than left to be
discovered.

### The approval is the only moment anybody gets a say

Both halves are governed, and the **passphrase** goes through the same approval-gated
request as the cluster token — so a person decides before this agent gets an identity at
all.

That matters more here than anywhere else in the cell, and the reason is the next section:
a certificate cannot be revoked out from under the agent. With the PAT you can change your
mind afterwards. With the cluster token you can at least wait out a TTL you chose. Here,
**the approval is the last decision anybody makes about this identity** until it expires.

> **The worker refuses an ungated release.** If Password Safe hands the passphrase over on
> the first ask, no person was consulted — and an episode that printed its usual success
> line would be describing something that did not happen. So it stops, with exit code
> **5**, and says which access policy to change.
>
> This worker cannot *make* Password Safe require approval; that is the managed account's
> access policy, set in BeyondInsight with auto-release off. What it can do is refuse to
> pretend. Pass `--no-require-approval` to run it as an ungated fetch — and then say so
> when you present it.
>
> The same check now runs on the [cluster episode](#the-second-demo-an-agent-that-cannot-authorise-its-own-access),
> where the page's claim that the agent "cannot authorise its own access" was equally
> untrue on an auto-releasing policy.

### Then do the thing that does not work

**Disable the managed account — which revokes the certificate on a backend that can — and
run the episode again. It still works.**

Nothing on this path checks a CRL or an OCSP responder. The agent stops when the
certificate **expires**, not when somebody takes it away.

Say that out loud. It is the opposite of the PAT demo and it is the reason the PAT demo
matters: a room that has just watched a revoke stop an agent dead will understand exactly
what it means that this one does not.

### What this demo does not prove

- **The bundle and the private key touch disk**, in one place. There is no version of
  this that keeps them out of the filesystem: the bundle is a file secret and ps-cli
  downloads it, `openssl` needs a file to open a PKCS#12, and Python's `ssl` needs file
  paths for a client certificate. So the episode bounds it instead — **one** `0700`
  temporary directory holds the bundle, the certificate and the key; the key is written
  `0600`, the passphrase reaches `openssl` through `PFXPASS` rather than the command line,
  and the directory goes on the failure path too. It is the one unavoidable exception to
  "nothing is stored on this host", and it is better said than found.
- **It needs BeyondInsight 26.1.0.878 or newer.** Below that, file secrets downloaded
  through the API came back larger than the original, so this episode would retrieve a
  corrupt bundle however careful it is — and the DER check does not catch it, because a
  too-large bundle still starts `0x30`. It is already a
  [Certificate Lab prerequisite](../../workload-lab/certificate-lab.md#password-safe);
  it is named again here because this episode depends on it silently.
- **Revocation on six of nine backends only.** EST, step-ca and `selfsigned` have no
  revocation operation at all, so on those the certificate stays valid until it expires
  whatever you do. Check which backend the CA uses before promising a revoke.
- **Nothing here has been run against a real CA**, a real Password Safe tenant or a real
  mTLS endpoint. The probe is exercised against a generated CA and a local server in
  `tests/test_agentcell_cert_episode.py`, and that is all it is.

## The refusals, and why each one exists

The cell refuses rather than installing something that would mislead:

| Refusal | Because |
|---|---|
| **The MCP server is off** | The worker's whole loop is one MCP call. It would install, start, and 404 on every poll — which reads as a broken agent rather than a feature nobody turned on. |
| **No SPIRE trust domain** | The worker would run and log `unattested`. Not fatal to the worker; fatal to the demo, because the identity half becomes a claim. |
| **A token that never expires** | The model allows it. This cell does not. A non-human principal whose authorization has no end is the arrangement being argued against. |
| **A token minted against an administrator** | The one that matters most. Every MCP tool applies the token user's RBAC, so the token user *is* the agent's blast radius — and picking an admin would quietly make the demo say the opposite of what it means to. |
| **A host this dashboard did not deploy** | Two privileged playbooks against a host of the caller's choosing is not something this should accept. The host is re-derived from deploy rows. |

## Before you deploy

The tab's own *Not ready yet* panel checks most of this at load and names the remedy, so
open **Workload Lab → Agent** first and read it before working down the list.

- [ ] The **Agent Demo Cell preview** on, and **MCP Server** on.
- [ ] A **SPIRE lab** stood up on the host — the worker attaches to a machine that is
      already a SPIRE agent node.
- [ ] A **narrow user** for the agent to be minted against. Not an administrator; the cell
      refuses that.
- [ ] The host reachable by the Ansible runner, and the dashboard's `/mcp` reachable from
      the host.

## The demo, end to end

About twelve minutes, and step 5 is the whole thing.

1. **Ask the question.** How many non-human principals are running in your estate right
   now, and could you stop one in the next sixty seconds?
2. **Mint the agent** — **Workload Lab → Agent → Mint an agent**. Show the response: a
   SPIFFE ID, a token name, an expiry — and the raw token exactly once. Point out that the
   row keeps the first three and never the fourth. Worth saying while the form is open:
   the user list holds **no administrators**, because the cell refuses one.
3. **Install it** with the two playbooks in `examples/playbooks/agent/` — the tab's
   **Install** button has both commands with this agent's values filled in — then
   `journalctl -u mcp-agent -f`. The dashboard does not run them and does not watch them,
   which is why the tab shows no progress bar and points at the journal instead.
4. **Read one line aloud.** The SPIFFE ID it proved, the token it spent, what it saw.
5. **Revoke the token**, with the log still on screen — the tab's **Revoke** button, or
   Settings → API Tokens if you would rather show it landing among the ordinary human
   tokens:

   ```
   [agent] spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · REFUSED — the token is revoked or expired · 14:06:41
   [agent] stopping: the identity is still valid, the authorization is not.
   ```

   **Stop there.** That last line is the demo: the worker is still who it was, and may no
   longer do anything.
6. **Then the record** — the token's `last_used_at`, the audit trail, the worker's log.
   Three records that agree about a principal nobody was watching.

## Lifecycle

**Revoking is not uninstalling, deliberately.** `DELETE /api/agentcell/agent/{id}` clears
the token and marks the row; the worker keeps running and keeps attesting, and its next
poll is refused. Tearing it down in the same action would remove the thing worth watching.

The host is an ordinary VM, so **Destroy reaps the worker with it** — this cell adds no
teardown of its own beyond revoking what it issued.

## E2E verification checklist

- [ ] The cell **refuses** with MCP off, with no trust domain, with a non-expiring token,
      with an admin user, and with a host the dashboard did not deploy — five refusals,
      each naming its remedy.
- [ ] The raw token appears **once**, in the create response, and nowhere in the row.
- [ ] `spire-agent api fetch x509` returns a SPIFFE ID the worker can parse. *(Unproven —
      see the preview note.)*
- [ ] The MCP SSE client connects through your ingress. *(Unproven.)*
- [ ] The log line carries **both** the SPIFFE ID and the token's name, and no part of
      the token's value.
- [ ] Revoking the token stops the unit, and `systemctl status mcp-agent` shows it stopped
      rather than restarting in a loop.
- [ ] Deleting the SPIFFE registration entry makes the next line say `unattested`, without
      the worker being touched.

## Troubleshooting

**The worker logs `unattested`.** Either no registration entry matches, or the unit has a
private `/tmp`. The SPIRE workload API socket lives under `/tmp`, so `PrivateTmp=yes`
hides it — the same trap `spire-agent-install.yml` documents.

**Every poll fails with a 404.** The MCP server is off, or `/mcp` is not routed. The cell
refuses this at mint time, so a 404 here means the flag changed afterwards.

**The unit restarts in a loop after a revoke.** `SuccessExitStatus=2` is missing from the
unit. The worker exits 2 on a refusal deliberately, so systemd must treat that as a stop.

**The agent sees more than expected.** Look at the token's user, not the agent. Every tool
applies that user's permissions — which is the point of the *What the agent could see*
card.

## Related

* [Workload Lab](../../workload-lab.md) — the four credential mechanisms this cell was
  aligned to. Three of them it can hold; one of them it deliberately cannot.
* [What consumes these credentials](../../workload-lab/consumers.md) — the register of
  what spends each one, and where this cell sits in it.
* [Workload access to Kubernetes](../../workload-lab/kubernetes.md) — the tab whose token
  this cell can genuinely request, holding nothing.
* [SPIFFE and SPIRE](../../workload-lab/spiffe.md) — the trust domain this worker attests
  itself against every loop.
