# Demo 4: an agent that mints its own cloud credential

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting an agent minting a short-lived cloud credential for itself.

Part of [Agent Demo Cell](../agent-demo-cell.md).

The demo aimed at the most common non-human credential there is. Ask anyone how their
build server reaches AWS and the answer is an access key in a CI secret store: no expiry,
no revocation, no record of which build read it. **This replaces that key, and the
replacement is minted by the thing that spends it.**

With a `cloud` link in place, on the host:

```bash
mcp_agent.py --cloud-episode --cloud-dynamic-name ci-aws
```

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · requesting a short-lived cloud credential from dynamic secret ci-aws · 14:02:11
[agent] holding nothing: this machine's own identity token is what Workload Credentials accepted. No PAT, no Password Safe client pair, and the issuance is recorded against this workload rather than the dashboard · 14:02:12
[agent] MINTED — one metered issuance. The aws credential expires 2026-09-23T15:02:00+00:00; it can be revoked or inspected · 14:02:13
[agent] spiffe://weaverlab.test/agent/mcp-reader · scope proved — authenticated as arn:aws:sts::…:assumed-role/ci-runner/x, and an AWS role scoped to its workload cannot enumerate IAM users (AccessDenied) · 14:02:15
[agent] spiffe://weaverlab.test/agent/mcp-reader · waiting out the lease — 59m 45s remaining. Nothing can shorten this · 14:02:15
…
[agent] spiffe://weaverlab.test/agent/mcp-reader · the credential is dead: the same call now returns 403/ExpiredToken · 15:02:31
```

**The three beats to name as they go past:**

1. **Nothing was on that host.** No key, no PAT, no client pair. The platform vouched for
   the machine and Workload Credentials accepted that.
2. **The refusal is the proof.** That the credential *works* shows only that it exists.
   That it is *refused* something is the scope.
   *(The lease id is deliberately absent from that line. It is a correlation handle to a
   live credential, and this journal gets tailed, screenshotted and pasted into tickets —
   so the line says whether one came back, which is what decides if the issuance can be
   revoked at all, and leaves the handle itself to Workload Credentials' own audit log.)*
3. **Nothing shortened the wait.** On AWS there is no revoke to reach for — STS will not
   withdraw a credential it has signed. The room watches the clock, and that is the
   argument for a short TTL rather than an embarrassment.

Exit codes are the punctuation, as everywhere else in this cell: **0** proved scope and
the ending; **4** a refusal did not refuse — the deny probe succeeded, or the credential
still worked after its expiry passed; **5** the ending was not proved, either because
the run was told to skip it or because it could not be watched. **3 is never returned**,
because nobody was asked.

## What this demo does not prove, and you should say so

- **This dashboard did not scope that credential, and could not.** The dynamic secret's
  definition in Workload Credentials decides what the assumed role may do. The refusal
  you just watched is an assertion *you* made with `--cloud-deny-probe`; a narrow
  dynamic secret is what makes the whole thing worth anything, and a secret that assumes
  an administrator role produces a short-lived skeleton key.
- **It costs money every time.** This is the only beat in this cell that bills. One run,
  one issuance.
- **On Azure, a release is not a kill.** `--cloud-end-with release` deletes the service
  principal's secret; an access token already issued lives out its own hour. Same shape
  as the cluster episode's *"the approval gates retrieval, not use"*.
- **WC authenticates whoever can present an accepted token.** The SPIRE path narrows that
  to nothing-at-rest; anyone who can attest as this workload can still mint.
- **None of this has run against a live tenant.** It needs a Workload Identity registered
  by hand in Pathfinder — a GUI action with no customer API — which is the same
  prerequisite `--token-source wlc` already has.
