# MCP Server (AI Client Integration)

## What is it?

The dashboard exposes an [MCP (Model Context Protocol)](https://modelcontextprotocol.io)
server at `/mcp`. Any MCP-compatible AI client — Claude Desktop, Claude Code,
Cursor, Continue, or any tool that speaks the protocol — can connect to it with
read-only access to your infrastructure data.

The MCP server runs **inside the main `app` container** with no extra services
or containers required. Access is controlled by a Personal Access Token (PAT)
that you create in the dashboard settings.

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
| MCP-compatible client | Claude Desktop, Claude Code, Cursor, Continue, or any MCP HTTP client |
| Personal Access Token | Created in **Settings → API Tokens** |

---

## Setup

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

| Tool | Description |
|---|---|
| `dashboard_summary` | Active jobs, today's failures, and enabled integrations |
| `list_jobs` | Recent jobs — filterable by status and/or workgroup |
| `get_job` | Full detail for one job by UUID (includes log output) |
| `list_vms` | VMware VMs (requires VMware integration to be enabled) |
| `list_ec2_instances` | EC2 instances deployed via this dashboard |
| `list_amis` | Available AMIs from your configured AWS account |
| `list_azure_vms` | Azure VMs deployed via this dashboard |

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
