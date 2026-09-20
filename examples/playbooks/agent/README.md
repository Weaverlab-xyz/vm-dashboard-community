# Agent cell samples (`agent/`)

The worker for the **agent demo cell**: a non-human principal that reads the estate
through the dashboard's own MCP server, names the identity it proved and the token it
spent on every loop, and stops visibly when that token is revoked.

Feature reference: [docs/profiles/demo/agent-demo-cell.md](../../../docs/profiles/demo/agent-demo-cell.md).

**These playbooks are cloud-agnostic.** They configure a Linux host over SSH, exactly as
the [SPIRE samples](../spire/README.md) do, so the only thing that changes between clouds
is how the VM was created.

| File | Target | What it does |
|---|---|---|
| `agent-install.yml` | the worker's host (SSH) | The worker, its 0600 token file and a systemd unit |
| `agent-spiffe-entry.yml` | the **SPIRE server** (SSH) | One registration entry, so the worker can attest |
| `files/mcp_agent.py` | — | The worker itself |

## Two credentials, and they are not the same one

This is the whole shape of the cell, and the worker's code is arranged so it cannot be
missed:

| | What it is | Where it lives |
|---|---|---|
| **SVID** | the worker's **identity** — it attests itself | nowhere. Re-fetched from the SPIRE workload API every loop, held in memory |
| **PAT** | the worker's **authorization** to this dashboard | either a 0600 file, or **nothing at all** — see below. Scoped to a user's RBAC, with an expiry, revocable instantly |

> **The SVID does not authenticate to `/mcp`, and nothing here pretends it does.** The MCP
> server takes a Bearer PAT (`api/mcp_server.py`) and has no mTLS path. Bridging the two —
> having the SVID mint the PAT — needs the Password Safe **SPIFFE SVID** plugin, whose
> configuration question `services/spire_lab_service.py` records as unresolved. So the
> worker proves its identity and spends its authorization in the same log line, and the
> gap between them stays visible rather than papered over.

## Two token sources, and the second stores nothing

`agent_token_source: file` (default) writes a 0600 file. That is a static secret —
smaller than an env var, which is readable from `/proc/<pid>/environ` and shows up in a
`ps e`, but a static secret nonetheless.

`agent_token_source: wlc` puts **nothing** on the host. The worker asks the platform for
its own identity token, presents that to **BeyondTrust Workload Credentials** in place of
a PAT, and reads its dashboard token back out. Everything it is configured with — site
id, service name, entra resource, base URL — is non-secret:

```
agent_token_source:     wlc
agent_wlc_base_url:     https://…
agent_wlc_site_id:      …
agent_wlc_service_name: …        # the registered Workload Identity this token satisfies
agent_wlc_resource:     …
agent_wlc_secret_name:  agent-mcp-pat
```

This is the suite answering its own question: Password Safe holds and governs the secret,
Workload Credentials brokers access to it against an identity the platform vouches for,
and the workload holds nothing. `services/workload_credentials_service` states the
principle — *"Two auth modes, and the second one stores nothing."*

Re-running the play with `wlc` **removes** any token a previous `file` install left
behind. "Nothing is stored on this host" must not be contradicted by a file in `/etc`.

> **`file` is still the default**, because the identity path is Azure-shaped today (IMDS,
> `X-IDENTITY-HEADER`), the in-cluster form is *Planned*, and the Azure + Pathfinder
> wiring has not been run live. The worker names its source on every line it logs.

## The log line is the demo

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · token vmcli_9f3c… · 14 active jobs, 2 failed today · 14:02:11
```

Three things in one line: who it is, what it spent, what it saw. The token hint is enough
to find the row in **Settings → API Tokens** and not enough to use — the full value never
reaches a log.

When the token is revoked:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · token vmcli_9f3c… · REFUSED — the token is revoked or expired · 14:06:41
[agent] stopping: the identity is still valid, the authorization is not.
```

That last line is the one to read aloud. The worker is still who it was; it simply may no
longer do anything.

## Prerequisites

- A host that is **already a SPIRE agent node** — run the [SPIRE plays](../spire/README.md)
  first. Without the workload API socket the worker logs `unattested` and the identity
  half of the demo is a claim rather than a fact.
- `mcp_server_enabled` on, and the dashboard's `/mcp` reachable from the host.
- A PAT **minted by the agent cell**, not by hand. The cell records what was issued; a
  token nothing recorded is the thing this demo argues against.

## Design notes worth keeping

**The unit does not `Restart=always`.** The worker exits 2 when its token is refused, and
that exit *is* the closing beat — restarting would hide the revoke behind a loop of
failures. `Restart=on-failure` with `SuccessExitStatus=2` means a genuine crash still
restarts and a revoke still stops.

**`PrivateTmp=no`.** The SPIRE workload API socket lives under `/tmp`, so a private `/tmp`
would make it invisible and the worker would log `unattested` forever — the same trap
`spire-agent-install.yml` documents for the agent itself.

**The token never enters the unit or the environment.** An `Environment=` line is
world-readable through `systemctl show`, and an env var is readable from
`/proc/<pid>/environ` by anything running as the same user. A 0600 file read once at
startup is the smaller surface.

**The selector is the unix UID, not a path.** A path selector attests whatever happens to
live at that path, so anything able to write there inherits the identity.

**The worker is given no route it does not need** — no cloud credentials, no kubeconfig,
no SSH. It talks to `/mcp` and the local SPIRE socket. The cell's argument is that a
non-human principal should hold the least it can and spend it visibly.

## Not yet run against live infrastructure

> The worker has never been run against a real SPIRE trust domain or a live MCP endpoint.
> Unobserved: whether `spire-agent api fetch x509` parses as expected on the target
> release, and whether the MCP SSE client negotiates cleanly through the dashboard's
> ingress. Validate both before demoing. `python3 files/mcp_agent.py --selftest` checks the
> argument wiring and touches nothing.
