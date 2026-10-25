# Troubleshooting the agent demo cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** a part of the agent demo cell will not deploy, connect or grant.

Part of [Agent Demo Cell](../agent-demo-cell.md).

**The worker logs `unattested`.** In order of likelihood:

- **The worker is not on a SPIRE agent node.** In this lab only the Kubernetes-linked k3s
  node runs an agent; the SPIRE lab's own VM runs the server. The Install dialog says
  which host the node is.
- **No registration entry matches** — the entry's parent must be that node's ID
  (`spiffe://<td>/node/k3s-01`) and its selector the uid the worker runs as.
- **The unit has a private `/tmp`.** The workload API socket lives under `/tmp`, so
  `PrivateTmp=yes` hides it — the same trap `spire-agent-install.yml` documents.
- **An old worker** passed the socket to `spire-agent` as a `unix://` URI, which it cannot
  dial. Re-run `agent-install.yml` to pick up the fixed worker.

**With `--token-source spiffe`, every poll fails at the token endpoint.** The dashboard
could not verify the SVID: check **Users → SPIFFE trust domains** shows the lab's trust
domain with a JWKS URL (a lab registers it when built; **Refresh keys** re-registers it),
and that the dashboard can reach the lab's `tcp/8443`. A replayed or wrong-audience SVID
is refused by design — the worker fetches a fresh one per exchange, audience-bound to
`/api/oauth/token` on the MCP URL's origin.

**Every poll fails with a 404.** The MCP server is off, or `/mcp` is not routed — the
worker's URL must be the SSE endpoint, `/mcp/sse`. The cell refuses a disabled MCP server
at mint time, so a 404 here means the flag changed afterwards or the URL is wrong.

**The unit restarts in a loop after a revoke.** `SuccessExitStatus=2` is missing from the
unit. The worker exits 2 on a refusal deliberately, so systemd must treat that as a stop.

**The agent sees more than expected.** Look at the token's user, not the agent. Every tool
applies that user's permissions — which is the point of the *What the agent could see*
card.
