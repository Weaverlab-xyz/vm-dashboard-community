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

- **Provisioning** *(stand it up)* — **nothing is created.** The worker attaches to a VM
  this dashboard already deployed, resolved from completed deploy-job rows rather than an
  address anyone supplied. The same call the [SPIRE lab](../../integrations/spiffe.md)
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
[agent] spiffe://weaverlab.test/agent/mcp-reader · token vmcli_9f3c… · 14 active jobs, 2 failed today · 14:02:11
```

Who it is, what it spent, what it saw. The token hint is enough to find the row in
**Settings → API Tokens** and not enough to use — the full value never reaches a log.

## What is not built

> **The SVID does not authenticate to `/mcp`, and nothing here pretends it does.** The MCP
> server takes a Bearer PAT (`api/mcp_server.py`) and has no mTLS path. Bridging the two —
> having the SVID *mint* the PAT — needs the Password Safe **SPIFFE SVID** plugin, and
> `services/spire_lab_service.py` records in its own docstring that whether the gateway
> populates plugin attributes is unresolved, so writing against it "would be betting on
> the answer". This cell does not bet on it either.
>
> So the two halves are shown side by side, each real, with the gap named. If somebody
> asks whether the identity could issue the authorization: **not yet, and here is exactly
> what has to be answered first.** That is a better moment than a hand-wave, and this
> audience is listening for it.

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
2. **Mint the agent.** Show the response: a SPIFFE ID, a token name, an expiry — and the
   raw token exactly once. Point out that the row keeps the first three and never the
   fourth.
3. **Install it** with the two playbooks in `examples/playbooks/agent/`, then
   `journalctl -u mcp-agent -f`.
4. **Read one line aloud.** The SPIFFE ID it proved, the token it spent, what it saw.
5. **Revoke the token** from Settings → API Tokens, with the log still on screen:

   ```
   [agent] spiffe://weaverlab.test/agent/mcp-reader · token vmcli_9f3c… · REFUSED — the token is revoked or expired · 14:06:41
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
- [ ] The log line carries **both** the SPIFFE ID and the token hint.
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
