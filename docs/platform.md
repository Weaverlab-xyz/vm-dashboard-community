# Platform

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding how people sign in, how the dashboard proves its own identity, where it is hosted, or how its runners and AI clients connect.

The dashboard as a platform: the settings under Settings → Integrations → Platform. Sign-
in and federation, its own workload identity, its SPIRE server and the remote agents that attest to it, hosting it, its Ansible and
Kubernetes runners, and the MCP server.

| Page | Read it when |
|---|---|
| [Where work runs](platform/where-work-runs.md) | you are deciding how the dashboard reaches the infrastructure it manages: one-shot remote workers in the cloud, persistent remote agents, or both |
| [OIDC and single sign-on](platform/oidc.md) | you want single sign-on instead of local passwords, or need to know which of the OIDC features you need |
| [Workload identity](platform/workload-identity.md) | you still store a cloud key, a product token or an agent's signing key, and want the recommended replacement: an identity, with write privilege granted just in time |
| [SPIFFE and SPIRE](platform/spiffe.md) | you are running the dashboard's own SPIRE server or the remote agents that attest to it, or want the map of every feature that uses SPIFFE |
| [Cloud Hosting](platform/cloud-hosting.md) | you want the dashboard reachable from outside your LAN, or fronting remote agents, including from a Compose install behind NAT through Cloudflare Tunnel or ngrok |
| [Container User](platform/container-user.md) | you are upgrading from a release that ran as root, a job hits "permission denied", or you are setting a security context on the container |
| [Config Migration](platform/config-migration.md) | you are standing up a second instance and do not want to re-type months of configuration |
| [Remote Worker](platform/remote-worker.md) | you are configuring the one-shot cloud runners that execute Config Management, Kubernetes and image-promote jobs |
| [MCP Server](platform/mcp-server.md) | you want an AI client to drive the dashboard through its own API |
