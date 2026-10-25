# Agent Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing what governs a thing that acts on its own, and how to stop it.

> **Preview.** Off by default; turn it on with the **Agent Demo Cell** preview
> toggle in Settings. Work through the
> [E2E verification checklist](agent-demo-cell/verification.md) before you present it.
>
> **What has run, and what has not.** Against a real SPIRE 1.15.3 server and agent: the
> worker's own `spire-agent api fetch x509` and `fetch jwt` calls (which found and fixed a
> bug — the worker passed the socket as a `unix://` URI, so every loop would have logged
> `unattested`), the registration entry play, and a real JWT-SVID authenticating at the
> dashboard's token endpoint, with a replayed SVID, a wrong audience and a revoked client
> all refused. **Not yet observed:** the worker on a real host end to end, the MCP SSE
> client through your ingress, and the Workload Credentials and Password Safe token
> sources.

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
  address anyone supplied. The same call the [SPIRE lab](../../preview/workload-lab/spiffe.md)
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
| **Authorization** | what the worker may do on this dashboard, bounded by the token user's RBAC | depends on the token user — see below. Always expires; always revocable instantly |

What the authorization is depends on **who you mint the agent against**:

| Token user | The cell mints | On the host | What reaches `/mcp` |
|---|---|---|---|
| a person-shaped user | a **PAT** | a 0600 file | the PAT |
| a **service account**, trust domain not registered | an **OAuth client** (`client_id:secret`) | a 0600 file holding the pair | an access token that lives minutes |
| a **service account**, trust domain registered | an OAuth client **bound to the agent's SPIFFE ID** | **nothing** | an access token the **SVID itself** minted |

The dashboard's own SPIRE server registers its trust domain when a cell is minted against
it, and keeps the keys current. A SPIRE lab built by the Workload Lab **registers its trust
domain with the dashboard automatically** too (by JWKS URL, kept current by the lab's scheduled **Refresh keys**), so a
service account against a lab-built trust domain lands in the last row. The mint response
then shows *Token: None — this agent holds no secret* and the client id.

Every loop leaves one line carrying both:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · 14 active jobs, 2 failed today · 14:02:11
```

Who it is, what it spent, what it saw.

**Pick a service account as the token user.** A service account can never be an
administrator and can never hold a PAT ([Service accounts](../../admin/service-accounts.md)),
what reaches `/mcp` expires on its own, and revoking the cell still refuses the very next
call — the next `/mcp` request, or the next exchange at `/api/oauth/token`, whichever
comes first. It is the recommended token user, and against a registered trust domain it
is the one that leaves nothing on the host.

**The credential is named, not shown.** That is its *name* in the line — the PAT's name as
**Settings → API Tokens** lists it, or the client's under **Users → Service accounts** —
and the thing you are about to revoke. No part of the credential reaches a log, and an
error from the MCP client is scrubbed of anything token-shaped on its way to one.

## What is not built

> **The SVID can now mint the authorization — through the dashboard, not Password Safe.**
> With a service account as the token user and the trust domain registered with the
> dashboard (automatic for a Workload Lab SPIRE lab), the cell binds an OAuth client to
> the agent's SPIFFE ID. The worker runs with `--token-source spiffe`: it presents a fresh
> JWT-SVID at `/api/oauth/token` and gets a minutes-long access token, holding nothing on
> the host. See [Service accounts → SPIFFE workloads](../../admin/service-accounts.md#spiffe-workloads-authenticate-with-the-svid-hold-nothing).
>
> What stays unbuilt is the *other* bridge — the SVID minting a credential **inside
> Password Safe** via the SPIFFE SVID plugin. Whether that plugin can be configured the
> way it was designed is still unresolved; the SPIRE lab's
> [attribute probe](../../runbooks/spire-lab-standup.md#5-onboard-and-read-the-one-line-that-matters)
> is how it gets answered. And the SVID **does not authenticate to `/mcp`
> directly** — `/mcp` has no mTLS path; the SVID is exchanged at the token endpoint for a
> bearer token, and that token is what `/mcp` sees.
>
> Without a service account, or against a trust domain the dashboard has not registered,
> the cell behaves as before: a PAT or client secret is the authorization, and the SVID
> proves identity beside it in the log.

## Pages

The demos, and the checks around them, have a page each:

| Page | Read it when |
|---|---|
| [No static secret on the agent host](agent-demo-cell/no-static-secret.md) | you want to show the agent host holds no stored credential, and why the ps hop is worth it |
| [Demo 2: an agent that cannot authorise its own access](agent-demo-cell/second-demo.md) | you are presenting an agent that has to ask a person before it gets access |
| [Demo 3: the credential nobody can take away](agent-demo-cell/third-demo.md) | you are presenting just-in-time access whose approval is the only moment anyone has a say |
| [Demo 4: an agent that mints its own cloud credential](agent-demo-cell/fourth-demo.md) | you are presenting an agent minting a short-lived cloud credential for itself |
| [Agent demo cell verification checklist](agent-demo-cell/verification.md) | you have deployed the cell and want to prove each part works before you present it |
| [Troubleshooting the agent demo cell](agent-demo-cell/troubleshooting.md) | a part of the agent demo cell will not deploy, connect or grant |

## What this agent is answerable for — and what it can ask for

An agent can be **linked** to one Workload Lab credential
(`POST /api/agentcell/agent/{id}/link`), so that "what does this agent have access to" is
one lookup rather than a conversation.

**All three confer a capability, and they differ in WHICH AUTHORITY the worker reaches —
which is the distinction that replaced "does this confer anything at all":**

| Link | What it is | Why |
|---|---|---|
| `cloud` | **a capability, and the one that bills** | The worker reaches **Workload Credentials directly** and *mints*: it presents its own identity token and generates against the dynamic secret. No vault in the chain and no approval, because there is nothing standing to release. Every run is one metered issuance. **There is no Request button for it:** that episode is one shot on the host. |
| `kubernetes` | **a capability** | The worker reaches Password Safe holding nothing, so it can genuinely *request* that tab's token — subject to whatever the access policy requires. |
| `certificates` | **a capability, and the one nobody can take away** | The same client pair opens both halves — the PKCS#12 passphrase from the managed account and the bundle from Secrets Safe. A CA row is not an identity, so the link names the managed account and the bundle title. **There is no Request-access button for it:** that episode is one shot on the host, not a route this dashboard owns. |

> **An earlier version of this page said no Workload Lab credential could reach this
> worker without it already holding one.** That stopped being true when the worker gained
> `--token-source ps`: it reaches Password Safe with a workload identity brokered by
> Workload Credentials, holding nothing at all. The Kubernetes tab's own sentence — *"the
> consumer is a program with a Password Safe API client"* — describes this worker.
>
> **And this page called a `cloud` link "accountability only", on the grounds that the
> tab's credential is *"returned to nobody"*.** That sentence was always about the
> **dashboard**, and it is still true of the dashboard — no route there hands a cloud
> credential to anything. It was being read as a statement about the mechanism. The
> worker calls Workload Credentials itself, which is exactly what keeps the dashboard
> out of it: the entry in WC's audit log is the workload.

**One link at a time.** An agent answerable for a cloud lease *and* a cluster token *and*
a certificate would be the most over-credentialed principal in the estate, which is the arrangement this cell
argues against. Unlink before relinking, so widening is a decision rather than an
accumulation.

### What a `cloud` link is good for, before you spend it

- **It says the revoke asymmetry out loud at link time.** Azure leases can be released
  early; **AWS leases cannot be revoked at all**, so the TTL is the only control there is.
  That is the provider's limit rather than this dashboard's, and hearing it when you link
  is better than discovering it when you try to revoke in front of a room.
- **An expired lease reads as the mechanism working**, not as a fault — honouring
  `workload_cloud_service.lease_state`, which exists to keep those two apart.
- **It names the dynamic secret, and hands back the command.** There is no button, so the
  link's notes carry the `mcp_agent.py --cloud-episode` line with your values already in
  it.

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
- [ ] A SPIRE server to attest the worker, one of two:
  - **This dashboard's own** (`docker-compose.spire.yml`) — **one VM, no lab.** Pick it on
    the mint form. The dashboard creates the entry and shows a one-use join token; the
    Install dialog's first step puts a SPIRE agent on the worker's own host with
    `spire-agent-install.yml`. The host must reach the dashboard on `tcp/8081`, as remote
    agents do. Revoking the cell keeps its identity (that is the demo); **Remove SPIRE
    identity** on the revoked row deletes it.
  - A **SPIRE lab** (any deployment mode — VM, Docker or Kubernetes) **linked to a k3s
    node** with its **Kubernetes** action. The worker's host must run a SPIRE agent, and
    in a lab the host that does is that k3s node — mint the agent **on that node**.
    The SPIRE lab's own VM runs the *server*, not an agent, and a worker there logs
    `unattested`. The Install dialog warns when the host is not the node. Choose this
    when the demo also shows the Password Safe SPIFFE plugin governing the lab.
- [ ] A **service account** to mint the agent against (recommended — see above), or at
      least a narrow user. Not an administrator; the cell refuses that.
- [ ] The Ansible runner able to reach both the SPIRE server and the node, and the
      dashboard's `/mcp/sse` and `/api/oauth/token` reachable from the node.

## The demo, end to end

About twelve minutes, and step 5 is the whole thing.

1. **Ask the question.** How many non-human principals are running in your estate right
   now, and could you stop one in the next sixty seconds?
2. **Mint the agent** — **Workload Lab → Agent → Mint an agent**, against a service
   account. Show the response: a SPIFFE ID, a credential name, an expiry — and **Token:
   None**, because this agent holds no secret. (Minted against a person-shaped user, the
   raw token appears here exactly once instead; point out that the row keeps the first
   three and never the fourth.) Worth saying while the form is open: the user list holds
   **no administrators**, because the cell refuses one.
3. **Install it** with the two playbooks in `examples/playbooks/agent/` — the tab's
   **Install** button has both commands filled in: the entry play aimed at the SPIRE
   server (with the lab's CLI prefix in Docker or Kubernetes mode, and the node's SPIFFE
   ID as the parent), then the worker aimed at the node, with
   `agent_token_source=spiffe` and the client id. Then `journalctl -u mcp-agent -f`. The
   dashboard does not run them and does not watch them, which is why the tab shows no
   progress bar and points at the journal instead.
4. **Read one line aloud.** The SPIFFE ID it proved, the token it spent, what it saw.
5. **Revoke it**, with the log still on screen — the tab's **Revoke** button, or the
   client under **Users → Service accounts** (a PAT: Settings → API Tokens, landing among
   the ordinary human tokens). With `spiffe` the worker's next exchange at the token
   endpoint is refused even though its SVID is still perfectly valid:

   ```
   [agent] spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · REFUSED — the token is revoked or expired · 14:06:41
   [agent] stopping: the identity is still valid, the authorization is not.
   ```

   **Stop there.** That last line is the demo: the worker is still who it was, and may no
   longer do anything.
6. **Then the record** — the token's `last_used_at`, the audit trail, the worker's log.
   Three records that agree about a principal nobody was watching.

## Lifecycle

**Revoking is not uninstalling, deliberately.** `DELETE /api/agentcell/agent/{id}` revokes
the PAT or deactivates the OAuth client and marks the row; the worker keeps running and
keeps attesting, and its next poll is refused. Tearing it down in the same action would remove the thing worth watching.

The host is an ordinary VM, so **Destroy reaps the worker with it** — this cell adds no
teardown of its own beyond revoking what it issued.

## Related

* [Workload Lab](../../preview/workload-lab.md) — the four credential mechanisms this cell was
  aligned to. It can now spend three of them, and attests against the fourth.
* [What consumes these credentials](../../preview/workload-lab/consumers.md) — the register of
  what spends each one, and where this cell sits in it.
* [Workload access to Kubernetes](../../preview/workload-lab/kubernetes.md) — the tab whose token
  this cell can genuinely request, holding nothing.
* [Short-lived cloud credentials](../../preview/workload-lab/cloud.md) — the tab whose credential
  this cell **mints** for itself, and the only one that bills when it does.
* [SPIFFE and SPIRE](../../preview/workload-lab/spiffe.md) — the trust domain this worker attests
  itself against every loop.
