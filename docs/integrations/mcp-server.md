# MCP Server (AI Client Integration)

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want an AI client to drive the dashboard through its own API.

## What is it?

The dashboard exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server at `/mcp`. Any MCP-compatible AI client — Claude Desktop, Claude Code,
Cursor, Continue, or any tool that speaks the protocol — can connect to it with
read-only access to your infrastructure data.

The MCP server runs **inside the main `app` container** with no extra services
or containers required. Access is controlled by a Personal Access Token (PAT)
that you create in the dashboard settings, and **every tool applies that token
owner's own permissions** — the same filtering the web UI applies to that user.

> **Upgrading from a build before this flag existed?** `/mcp` used to be mounted
> unconditionally. It is now off by default like every other integration, so an
> existing Claude Desktop / Cursor config will get a 404 until an admin turns
> **MCP Server** on under Settings → Integrations. That is one toggle, and it is
> deliberate: the endpoint reads your estate, so it should be something you chose
> rather than something you inherited.

---

## Use cases

- **Ask Claude about your infrastructure** — "What jobs failed today?", "How
  many EC2 instances are currently running?", "Show me the details of job
  abc-123."
- **AI-assisted troubleshooting** — paste a failed job log into Claude and ask
  what went wrong, with the AI able to fetch surrounding job context directly.
- **Dashboard queries without the browser** — check job status or VM inventory
  from your terminal or IDE without opening the web UI.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Dashboard running | The MCP server is built into the `app` container — no separate setup |
| **MCP Server enabled** | **Settings → Integrations → MCP Server.** Off by default; while it is off `/mcp` returns 404 |
| MCP-compatible client | Claude Desktop, Claude Code, Cursor, Continue, or any MCP HTTP client |
| Personal Access Token | Created in **Settings → API Tokens** |

---

## Setup

### Step 0 — Enable the MCP server

**Settings → Integrations → MCP Server.** Off by default; `/mcp` returns 404
while it is off, before it looks at your token at all.

### Step 1 — Create a Personal Access Token

1. Open the dashboard → click your username (top right) → **Settings**, or
   navigate to `/settings`.
2. Scroll to **API Tokens** → click **New Token**.
3. Enter a name (e.g. `claude-desktop` or `cursor`) and an optional expiry.
4. Click **Create** and copy the token — it looks like `vmcli_<64 hex chars>`.
   **It is shown only once.**

### Step 2 — Configure your AI client

#### Claude Desktop

Edit the config file for your platform:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "vm-dashboard": {
      "url": "http://localhost:8001/mcp",
      "headers": {
        "Authorization": "Bearer vmcli_<your-token>"
      }
    }
  }
}
```

Restart Claude Desktop. A **vm-dashboard** entry will appear in the tool
selector.

#### Claude Code (CLI)

```bash
claude mcp add --transport http vm-dashboard http://localhost:8001/mcp \
  --header "Authorization: Bearer vmcli_<your-token>"
```

Run `claude mcp list` to confirm the server was added.

#### Cursor / Continue / other clients

Point the client at `http://<host>:8001/mcp` with an
`Authorization: Bearer vmcli_<token>` header. The server uses the **HTTP
Streamable transport** (SSE-based), which is the MCP standard transport for
remote servers.

If the dashboard is running on a remote machine (not `localhost`), replace
`localhost:8001` with the hostname or IP of that machine.

---

## Available tools

All tools are **read-only**. Deploy, start, and stop actions must be performed
in the web UI or via the REST API.

**Every tool returns only what the token's owner can see in the web UI.** A token
belonging to a non-admin returns that user's workgroups and their own resources —
not the estate. So an empty result means *"none visible to you"*, which is not the
same as *"none exist"*, and the two are deliberately indistinguishable.

| Tool | Description | Who sees what |
|---|---|---|
| `dashboard_summary` | Active jobs, today's failures, enabled integrations | Job counts scoped like `/jobs`; integration flags match the unauthenticated `/api/features` |
| `list_jobs` | Recent jobs — filter by status and/or workgroup | Your own jobs unless you hold `jobs:read` or are an admin |
| `get_job` | Detail for one job by UUID | Same scope as `list_jobs`; the deploy payload is filtered (below) |
| `list_inventory` | Every resource, normalised across providers | Your workgroups; resources with no workgroup only if you created them |
| `list_ec2_instances` | EC2 instances deployed via this dashboard | Your workgroups |
| `list_azure_vms` | Azure VMs deployed via this dashboard | Your workgroups |
| `list_gcp_instances` | GCE instances deployed via this dashboard | Your workgroups |
| `list_oci_instances` | OCI compute instances deployed via this dashboard | Your workgroups |
| `list_amis` | AMIs owned by the configured AWS account | Requires `aws:read` |
| `list_vms` | VMware Workstation VMs synced by an agent | Only VMs an admin has tagged into one of your workgroups |
| `list_containers` | Cached containers for one Portainer endpoint | Requires `containers:read`; takes an `endpoint_id` |
| `list_databases` | Cloud databases | Your own unless you are an (effective) admin |
| `list_k8s_clusters` | Kubernetes clusters | Your own unless you are an (effective) admin |
| `list_functions` | Cloud functions | Your own unless you are an (effective) admin |
| `list_expiring` | Resources carrying an auto-delete timer, plus the timer's gates | Same rule as `list_inventory` |
| `config_drift` | Targets unverified or changed since their last Ansible apply | Any authenticated user, as on the Ansible stream |
| `list_agents` | Registered remote agents and their running-job counts | **Admin only** |
| `cost_summary` | Per-cloud month-to-date spend and budget alerts | **Admin only.** Reads the cost cache; never forces a billable requery |
| `secret_staleness` | Per-secret age and staleness | **Admin only** |

### What `get_job` will not return

A deploy job's payload carries operational plumbing — the Terraform state of the VM's
PRA Shell Jump, its Password Safe registration state, the name of its SSH secret, an
admin-password reference. None of that belongs in an AI client's context window, so
`get_job` filters the payload through an **allowlist**: identifiers, placement, image,
addresses and state come back, and anything unrecognised is dropped. It is an allowlist
rather than a blocklist because the payload grows every time an integration is added,
and a blocklist fails open on the next one.

If you need the unfiltered record, use `GET /api/jobs/{id}` — the same token works
there, subject to the same permission check.

---

## Token management

- Create separate tokens per client (Claude Desktop, Cursor, etc.) so you can
  revoke access for a specific client without affecting others.
- Tokens are hashed in the database — if you lose a token, create a new one.
- Set an expiry for short-lived clients or one-off queries.
- Revoke tokens any time from **Settings → API Tokens → Revoke**.

---

## Accessing the dashboard from a remote host

If your AI client runs on a different machine than the dashboard:

1. Replace `localhost:8001` with the dashboard host's IP or hostname.
2. Make sure port 8001 is open between the two machines (firewall / security
   group).
3. For production use, place the dashboard behind a reverse proxy with TLS and
   use `https://` in the MCP URL.

---

## Troubleshooting

**404 from `/mcp`, or the client reports the server is missing** — the MCP server
is off. It is off by default: enable **MCP Server** under Settings → Integrations.
This is the usual symptom after upgrading from a build where `/mcp` was always
mounted. The gate is checked before the token is, so a 404 says nothing about
whether your PAT is valid.

**A tool returns fewer resources than the web UI shows you** — check which user
the token belongs to. Tools apply that user's permissions, so a token created by
a non-admin returns their workgroups and their own resources. "None visible to
you" and "none exist" deliberately look the same.

**"Connection refused"** — verify the dashboard is running:
`curl http://localhost:8001/api/health`. If it returns `{"status":"ok"}` but
the MCP client still fails, check that the client is using `http://` not
`https://` (unless you have TLS configured).

**"Unauthorized"** — the PAT is missing, expired, or revoked. Create a new
token in **Settings → API Tokens**.

**No tools appear in Claude Desktop** — restart Claude Desktop after editing
`claude_desktop_config.json`. Also confirm the JSON is valid (no trailing
commas).

**"Tool call failed"** — the tool may require a feature that is not enabled
(e.g. `list_vms` requires `VMWARE_ENABLED=true`). The tool will return an
explanatory error message in the response.
