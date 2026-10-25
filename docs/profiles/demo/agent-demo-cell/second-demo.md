# Demo 2: an agent that cannot authorise its own access

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting an agent that has to ask a person before it gets access.

Part of [Agent Demo Cell](../agent-demo-cell.md).

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
and for the same reason `docs/preview/workload-lab/kubernetes.md` gives: *"a step in a
runbook gets skipped, and an assertion does not."*

| Profile | Succeeds | Must be refused |
|---|---|---|
| `deployer` | list pods in its namespace | the same list in another namespace |
| `reader` | list pods cluster-wide | read a Secret — upstream `view` omits them by design |

Exit codes are the punctuation: **0** proved the scope, **3** was never approved, **4**
means a refusal did not refuse — the one outcome that would otherwise look like success —
and **5** means Password Safe released without consulting anybody, so there was no human
in the loop to demonstrate.

## What this demo does not prove, and you should say so

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
