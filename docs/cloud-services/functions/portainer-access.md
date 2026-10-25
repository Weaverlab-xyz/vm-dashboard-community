# The portainer_access workload

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Entitle to grant and revoke Portainer team membership.

Part of [Cloud Functions](../functions.md).

Just-in-time Portainer access. Portainer has no Entitle connector at all, so this
adapter is the only route to it.

> **You probably do not want to deploy this by hand.** The Portainer page has a
> **Just-in-time access (Entitle)** card that deploys it into the node's own cloud and
> region, VPC-attached and aimed at the node's internal IP, with the API token staged
> in the cloud's secret store, the node firewall opened to the function's subnet, and
> the Entitle integration registered — all as one job. The adapter reads its target
> from its own environment, so a hand-deploy that named the wrong Portainer cannot be
> corrected afterwards. See
> [Portainer through Entitle](../portainer/entitle.md). The form below
> is the path for a Portainer that lives outside this dashboard's reach.

| Setting | Notes |
|---|---|
| `FN_PORTAINER_URL` | base URL of the Portainer instance |
| `FN_PORTAINER_API_KEY` | an access token — pass it as `secret_environment`, never as a plaintext setting (see [Credentials](../functions.md#credentials)) |
| `FN_PORTAINER_VERIFY_SSL` | `0` only for a self-signed lab instance |
| `FN_PORTAINER_DRY_RUN` | unset or truthy = dry run. **Default is dry run.** |

**Access is granted through team membership, not per-user.** In Portainer an access
policy attaches to an environment or environment group and names *teams*. Configure
that once; a grant is then a single membership row, and a revoke removes it. That
keeps the reversible per-request operation separate from the standing
configuration, so a revoke can never leave a half-dismantled access policy behind.

Entitle sees each **team** as an asset, with the team name as the `role_code` — so
an operator reading a request sees the team they configured access on.

Two guards worth knowing about, because they bound the blast radius:

- `create_actor` makes a **standard** user in **no team**. An actor whose
  `give_access` never arrives can reach nothing.
- `get_actors` and `delete_actor` only ever see accounts this adapter minted (the
  `jit-` prefix). Your real Portainer users are never listed to Entitle, and a
  request to delete one is refused with a 403 — a grant integration must not be able
  to remove an operator's account.

If Portainer is reachable only from inside your network, deploy the adapter in
`vpc` mode; otherwise `public` is fine.

> For a **dashboard-managed** Portainer node, `vpc` is not a preference — the node's
> firewall is fail-closed and a public function has no stable egress IP to allow, so a
> public adapter deploys cleanly and then times out on every grant. That is why the
> Portainer page's card does not offer the choice, and why it adds the function's own
> subnet range to the node firewall.
