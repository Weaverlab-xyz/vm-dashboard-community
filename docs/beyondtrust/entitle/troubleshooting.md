# Troubleshooting Entitle

> **Audience:** operator · **Profile:** `both` · **Read this when:** a resource will not register in Entitle, or a request is granted and nothing changes.

Part of [Entitle Integration](../entitle.md).

**Nothing registers in Entitle** — confirm both the global **Registration enabled**
capability *and* the per-build "Register in Entitle" checkbox are on, then check the job
log: `docker compose logs app | grep -i entitle`. Registration is non-fatal — the VM/DB
still provisions; the job message records why registration was skipped or failed.

**"an Entitle Agent token named X already exists in the tenant"** (the raw provider
form is `Failed to create the Agent Token, status code: 400 … Resource already exists`) —
the token mint failed, so the `k8s_entitle_agent` job died **before touching the cluster**;
nothing was installed and Live Output stays empty. It means the tenant already holds a
token of that name while this dashboard holds no copy of its value. The value is returned
only at creation and cannot be read back, and `ensure_agent_token` already tries to recover
it from `entitle_agent_token_tf_state` before minting — so reaching this error means there
is no stored state either (config migration treats all three keys as runtime handles and
drops them: see `config_migrate/classify.py` `_RUNTIME_HANDLES`). Teardown now destroys the
auto-minted token (the agent `remove` action, and the decommission of the hosting cluster),
so a conflicting token was minted by an earlier dashboard version, a different
dashboard/environment, or by hand. Fix by one of: delete
that token in Entitle and retry — which breaks any agent still using it; set
`ENTITLE_AGENT_TOKEN_NAME` to an unused name so a fresh token is minted; or set
`ENTITLE_AGENT_TOKEN_REF` to the existing token value. Both keys are **env/`.env` only** —
`entitle_agent_token_name` is read-only in the Settings panel and `entitle_agent_token_ref`
is not in it at all. To check whether an agent is in fact installed, the Kubernetes page's
per-cluster flag is just `entitle_agent_cluster_id == cluster.id`; confirm against reality
with `helm list -n entitle`.

**"private target requires entitle_agent_token_name"** — the resource is private and no
Entitle agent is configured. Either provision the agent (Kubernetes) and set
`entitle_agent_token_name`, or register only public resources.

**"Failed to fetch the resources of &lt;target&gt;" (SSH ephemeral)** — the agent can't
reach the private host on port 22. Almost always a **network path**: the agent's cluster
VPC has no route to the target (e.g. a GKE cluster in its own isolated VPC, and the
private VM in the sandbox `vm-subnet`). Fix = ensure the cluster module peered back to the
sandbox VPC — set `gcp_network` (GCP) / `aws_vpc_id` (AWS) **before provisioning** the
cluster, then re-provision so the peering + lab-VM firewall are created (see *Network
reachability* above). Verify from a pod: `nc -vz <target-private-ip> 22`. (For the
*Kubernetes* connector — not SSH — the same message instead means the agent SA lacks
cluster RBAC; that's handled by the cluster-admin binding `setup_entitle_agent` applies.)

**`Permission denied (publickey)` or `Connection timed out` connecting to an SSH grant** —
work through these in order (details in
[Connecting to an SSH ephemeral-account grant](resource-registration.md#connecting-to-an-ssh-ephemeral-account-grant)):

1. **`Connection timed out`**: there's no route from your machine to the private address.
   Start the PRA Network Tunnel through the gateway first.
2. **`Permissions 0777 ... are too open`**: you ran the one-liner in WSL from a
   `/mnt/c/...` directory. Run it from `~` instead, or use the PowerShell steps.
3. **`Permission denied (publickey)`**: the key, username or host is from a different
   grant than the other two. Each grant creates a new account, and when a grant ends its
   account is deleted. The server's `/var/log/auth.log` shows `Invalid user <name>` for
   an account that no longer exists. When a key really is rejected, the log shows the
   fingerprint of the key your client offered. Compare that with `ssh-keygen -lf` run on
   your `.pem` and on the account's `~/.ssh/authorized_keys`. On Azure you can read the
   log without logging in:
   `az vm run-command invoke --command-id RunShellScript --scripts "tail -50 /var/log/auth.log"`.
4. **You can't overwrite or delete the `.pem`**: `icacls` left you read-only access.
   Write each new grant's key to a new filename.
5. **The prompt never appears, or the output is red `NativeCommandError`**: you're in
   PowerShell ISE. Use a regular PowerShell window or Windows Terminal.

**"entitle_owner_id / entitle_workflow_id is not configured"** — both are required to
create an integration. Fill them in under Settings → Integrations → Entitle.

**`terraform init` can't find the provider** ("was not found in any of the search
locations") — the `entitleio/entitle` provider is baked into the image's read-only
provider mirror (`$TF_PROVIDER_MIRROR_DIR`, served via `/etc/terraform.tfrc`), and the
registry is deliberately not a fallback. Rebuild the image if you changed the Dockerfile
provider pre-cache step or the provider version constraint.

**Wrong application / connection** — `application.name` and the `connection_json` keys are
application-specific. Verify the slug against the `entitle_applications` data source and the
per-application connection schema in the BeyondTrust integration docs linked above.
