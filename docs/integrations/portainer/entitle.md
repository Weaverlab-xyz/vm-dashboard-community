# Portainer: just-in-time access through Entitle

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want people to request Portainer access in Entitle and get an account for the duration, or the `portainer_access` adapter is misbehaving.

Part of [Portainer](../portainer.md).

This applies to a [managed Portainer server](managed-server.md) and to one you connected yourself.

A [PRA Web Jump](managed-server.md#pra-web-jump-optional) brokers *your* access to the admin account. This is the other half: letting
someone **request** Portainer access in Entitle, get an account minted for the duration,
and have it deleted on revoke.

Portainer has **no Entitle connector at all**, so the route is the `portainer_access`
**Cloud Function** — an adapter that implements Entitle's Remote Adapter contract
itself. **Just-in-time access (Entitle)** on the Portainer tab deploys and wires one in
a single click; the deploy form on the Functions page remains the path for a Portainer
outside this dashboard's reach.

What the button does, as one `portainer_adapter_pair` job:

1. **Stages the API token** in the node cloud's own secret store (Secrets Manager /
   Key Vault / Secret Manager). The function resolves it from there — the token is
   never a plain function setting, and never passes through Terraform state or the job
   record.
2. **Deploys the adapter** into the node's own cloud and region, **VPC-attached**, and
   points it at the node's **internal** IP. This is not a preference: the node firewall
   is fail-closed and a public function has no stable egress IP to allow, so a public
   adapter would deploy cleanly and then time out on every grant.
3. **Opens the firewall to it** — the function's own subnet range joins the merged
   source set (`portainer_adapter_source_cidr`), the same way a Gateway's `/32` does.
   VPC firewall rules apply to intra-VPC traffic too, so without this the function
   reaches the internal IP and is dropped.
4. **Registers it in Entitle** as a REST integration in **Ephemeral Accounts** mode.
   Skipped, with a note in the job, when `entitle_registration_enabled` is off — the
   adapter is still deployed and pointed at Portainer, and you can register it later
   from the Functions page.

Then, in Entitle: each Portainer **team** is an asset, with the team name as the role.
Configure the environment (or environment-group) access policy on a team once; a grant
is then a single membership row and a revoke removes it, so a revoke can never leave a
half-dismantled access policy behind.

> **The adapter is deployed ARMED.** Once registered, a grant creates a real Portainer
> account and a revoke deletes it. That is the point of the button, and a silently
> no-op adapter is the worse surprise — the card shows a `DRY RUN` badge if one ever is.

Two guards bound the blast radius, and both live in the function rather than in policy:
accounts are minted as **standard** users in **no team** (an actor whose grant never
arrives can reach nothing), and the adapter only ever lists or deletes accounts it
minted itself (the `jit-` prefix). Your real Portainer users are never shown to Entitle,
and a request to delete one is refused. See
[cloud-functions.md](../cloud-functions.md#portainer_access).

Needs Cloud Functions enabled, a stored `portainer_pat`, and a configured secret store
for the node's cloud. The card names whichever of those is missing instead of offering
a button that cannot work.

It also needs **at least one team in Portainer**, which a fresh node has none of. A
team is both the asset and the grant, so against a teamless Portainer the adapter
reports itself unconfigured and step 4 refuses to register it — after steps 1–3 have
already deployed a real function. The pair request reads the team list first and
refuses the click instead, naming the fix; a Portainer the *dashboard* cannot reach is
still pairable, because only the adapter's own reachability decides anything. If a
registration fails for some other reason, the job says so and says that the function
is deployed: fix the cause and finish it with **Register in Entitle** on the Functions
page, or remove the adapter here and pair again — pairing twice is refused.

## Preparing the team and its access, as a job

Both prerequisites are ordinary Config-Management runs — Portainer is its own target
family there, so nothing is installed anywhere and no credential is typed in:

| Playbook | What it does |
|---|---|
| [`portainer-jit-prereqs.yml`](../../../examples/playbooks/portainer/portainer-jit-prereqs.yml) | The team **and** its access to the environments you name, in one run |
| [`portainer-team-ensure.yml`](../../../examples/playbooks/portainer/portainer-team-ensure.yml) | Just the teams (`team_name`, or `team_names` for several) |
| [`portainer-env-access.yml`](../../../examples/playbooks/portainer/portainer-env-access.yml) | Just the access policy, in either direction (`state: present\|absent`) |

Upload one to Storage, then **Config Management → the asset → Portainer → Run** with,
say, `{"team_name": "Platform", "endpoint_names": ["local"]}`. All three are
idempotent, so re-running one against a Portainer that is already set up is a no-op
rather than a second team. The team name is the **role code** a requester picks in
Entitle, so name it the way it should read there.

A grant's access policy is merged, never replaced: Portainer's environment update
assigns the whole `TeamAccessPolicies` map, so writing only the new team would revoke
every other team's access. [`examples/playbooks/portainer/`](../../../examples/playbooks/portainer)
has the rest of that reasoning.

## Moving the node strands the adapter

Step 2 is not a preference, and it is not revisited. The adapter is VPC-attached in
the node's **own cloud and region**, and a VPC is regional — so where the adapter sits
is a reachability fact, decided once when you paired it.

The node, meanwhile, is relocatable: redeploying it to a different region moves it, and
redeploying it to a different cloud *relocates* it (the old node is deleted). Nothing
in either move touches the function. Afterwards:

- the adapter is still attached to the **old** network and cannot reach the node at its
  internal IP at all;
- `portainer_adapter_source_cidr` is a subnet range from that old network, so it is
  merged into the **new** node's allow-list, where it admits nothing;
- every Entitle grant times out, reported on **Entitle's** side as a connect timeout —
  while the adapter card shows the function `available` with a live integration id.

So nothing fails at the moment of the move, and nothing downstream says so. The deploy
names it instead: a relocation (or a reuse) that lands in a cloud or region other than
the paired adapter's puts an `adapter_stranded` note in the **job result**, and the
adapter card carries a red **STRANDED** badge with the same explanation.

**The fix is to re-pair**: **Remove adapter**, then **Deploy adapter**. That redeploys
the function beside the new node and re-opens the new node's firewall to its range.
Re-sending the token does *not* help — the token was never the problem. Removing the
adapter also takes its Entitle integration with it, so anyone with a standing Portainer
request in Entitle has to request again against the new integration.

A **public** adapter (an unmanaged Portainer, reached over its configured URL) is never
reported this way: it was never placed to match a node.

## Changing the adapter's token

Step 1 runs once, inside the pairing job, so the adapter holds its own copy of the
token — and that copy is what fails when the token changes. **Re-send the token to the
adapter** (under the node table) rewrites the staged secret in place; minting a token
does it for you. Neither one redeploys the function or touches its Entitle
integration.

When the new value takes effect differs by cloud, and the button says which applies:

| Cloud | Mechanism | When it takes effect |
|---|---|---|
| Azure | `@Microsoft.KeyVault(...)` app setting, resolved by the platform at app start. The reference is versionless and Azure re-polls it on its own schedule (up to 24h) | Immediately — the function is **restarted** for exactly this reason |
| GCP | `secret_environment_variables` at `version = "latest"`, resolved when an instance starts | At the next cold start; a warm instance keeps the old value for a few minutes |
| AWS | The function reads Secrets Manager itself, behind a 300s cache | Within five minutes |
