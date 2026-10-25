# Agent demo cell verification checklist

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you have deployed the cell and want to prove each part works before you present it.

Part of [Agent Demo Cell](../agent-demo-cell.md).

- [ ] The cell **refuses** with MCP off, with no trust domain, with a non-expiring token,
      with an admin user, and with a host the dashboard did not deploy — five refusals,
      each naming its remedy.
- [ ] The raw token appears **once**, in the create response, and nowhere in the row —
      and against a service account with a registered trust domain, **not at all**.
- [ ] The Install dialog's two commands run as shown: the entry lands on the SPIRE server
      with the node as its parent, and the worker installs on the node.
- [ ] `spire-agent api fetch x509` returns a SPIFFE ID the worker can parse. *(Run against
      SPIRE 1.15.3 locally; confirm on the node.)*
- [ ] With `--token-source spiffe`, the first line says the token came from `spiffe` and
      nothing is on the host, and polls succeed. *(The exchange was run against a real
      SVID locally; the full loop on a host has not.)*
- [ ] The MCP SSE client connects to `/mcp/sse` through your ingress. *(Unproven.)*
- [ ] The log line carries **both** the SPIFFE ID and the token's name, and no part of
      the token's value.
- [ ] Revoking the token stops the unit, and `systemctl status mcp-agent` shows it stopped
      rather than restarting in a loop.
- [ ] Deleting the SPIFFE registration entry makes the next line say `unattested`, without
      the worker being touched.
- [ ] `--cloud-episode` mints **exactly once** and says so; the deny probe you asserted
      comes back refused with the right error code, and the same call fails after the
      expiry passes. *(Unproven — it needs a Workload Identity registered by hand in
      Pathfinder, which is a GUI action with no customer API.)*
- [ ] No part of a minted cloud credential appears in `journalctl -u mcp-agent`.
