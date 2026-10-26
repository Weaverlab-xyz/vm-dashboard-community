# Config Management runners

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are choosing or configuring the runner a job executes on.

Part of [Config Management](../config-management.md).

Where the Ansible process actually runs. Picked in
**Settings → Ansible → Runner**.

| Runner | Where it runs | Best for |
|---|---|---|
| **Local Docker** | Inside the dashboard container's Docker context. Uses a side-car `chrweav/ansible-winrm` container per run (ansible + pywinrm). | On-prem hypervisor targets; corporate-network targets; anything reachable from the dashboard host. |
| **AWS ECS Fargate** | A Fargate task launched per run in your VPC. | EC2 targets in private subnets without a path back to the dashboard host. |
| **Azure ACI** | An Azure Container Instance per run, in your VNet. | Azure VMs in private subnets. |
| **GCP Cloud Run Jobs** | A Cloud Run Job per run, in your project. | GCE instances. |
| **Remote agent** | A one-shot container on the *agent's* host, inside your network. Not selectable here — it is chosen automatically when the target is only reachable that way. **Which** agent defaults to the one that discovered the VM, and can be routed per address range on Remote Agents → Config Routes. | On-prem hypervisor guests and on-prem databases, especially from a cloud-hosted dashboard; and cloud VMs and databases a cloud route sends to an agent in their network. See [remote agents](../../platform/spiffe/remote-agents/config-runs.md#agent-executed-config-management) and [which agent executes a run](../../platform/spiffe/remote-agents/config-runs.md#which-agent-executes-a-run). |

The cloud runners exist because connecting from a dashboard sitting on
a corporate LAN to a deeply-private cloud subnet is often impossible
without a VPN. Running the playbook *inside* the cloud avoids that
network problem at the cost of one Fargate task / ACI / Cloud Run
invocation per run.

**The runner choice constrains the storage choice.** The runner has to
fetch the asset before executing it, which means the runner needs network
reachability to the storage backend. Two combinations don't work:

| Storage | Runner | Outcome |
|---|---|---|
| Local Filesystem / UNC | ECS / ACI / Cloud Run | Refused (the cloud runner has no path back to the corporate file server). The dashboard surfaces this as a disabled radio + 400 on the API. |
| Cloud bucket (S3 / Blob / GCS) | Local Docker | Works fine. Fetches go out over the dashboard host's normal egress. |

Most teams pair Local Docker + Local Filesystem (or a cloud bucket) for
on-prem labs, and one of the cloud runners + a cloud bucket for cloud
fleets.

**A Password Safe managed account on the ECS or Cloud Run runner** needs one of two
delivery paths, and the run is refused until one is on:

- **Collect credentials from the dashboard** (`ansible_runner_credential_callback`). The
  task proves its own cloud identity (the ECS task role, set in
  `ansible_ecs_task_role_arn`, or the Cloud Run service account) and collects the
  credential from the dashboard, sealed to a key made inside the container. No copy is
  written to a cloud secret store. It needs the agent gateway URL pinned, because that is
  where the task calls back.
- **Ephemeral cloud secrets.** The dashboard copies the checked-out credential into AWS
  Secrets Manager or GCP Secret Manager for the run and deletes it afterwards.

Both are under **Settings → Ansible**; how each works, and what each one trusts, is in
[Secrets management](../../admin/secrets-management/ansible-runs.md#collect-from-the-dashboard-no-store-copy).
ACI injects the credential inline and needs neither.

## Why one-shot runners (the security argument)

Every runner — Local Docker, ECS Fargate, ACI, Cloud Run Jobs — is
**ephemeral by design**. A new container is spawned per run; it
executes the playbook; it exits and is destroyed. Nothing persists
between runs.

This is deliberate, and it matters. Long-lived runners are a known
weakness in CI/CD and config-management estates: they accumulate
secrets in environment variables, cached SSH keys in `~/.ssh`,
remembered hosts in `known_hosts`, leftover state from previous runs.
A single compromise of a long-lived runner can yield credentials that
have been touched by every job that ever ran on it. The community
edition's design refuses to be that target:

- **Secrets are fetched just-in-time.** The cloud secret store is
  consulted at the start of each run, and the SSH private key never
  outlives it. A cloud runner writes the key inside the task's own
  ephemeral filesystem, which is destroyed with the task. The local runner
  writes it, mode 0600, into a per-run temporary directory on the
  dashboard host that is mounted into the run container and deleted when
  the run ends. A remote agent copies it into its run container through
  the Docker archive API, and it goes with the container. There's no place
  for the key to leak to.
- **No process or filesystem outlives the run.** The Ansible binary,
  the playbook bytes, the asset cache, the inventory JSON — all of it
  goes away with the container. The next run starts from zero.
- **No shared user namespace between runs.** Two operators running
  back-to-back jobs against the same target cannot accidentally read
  each other's variables, output, or temp files. Each run is its own
  Linux process tree.
- **The dashboard process itself never holds the secret long.** The
  playbook bytes pass through the dashboard's memory once on the way
  to the runner; nothing is cached on disk on the dashboard host.

The compliance angle: regulations like SOC 2 CC6.1, NIST SP 800-53
AC-6 (least privilege) and SC-39 (process isolation), and CIS Controls
4.1 / 4.7 all point at "minimise persistent privileged surface". An
ephemeral runner satisfies them by construction — there's nothing
persistent to harden, audit, or rotate. Auditors generally accept
"the runner has a 90-second lifespan and zero state at rest" with less
friction than "here's our hardening baseline for the long-running
worker fleet."

Compared to common alternatives:

| Approach | Persistent attack surface | Secret-at-rest in runner | Per-run isolation |
|---|---|---|---|
| **Dedicated CI worker** (e.g. self-hosted GitHub runner) | The whole VM | Cached creds, SSH known_hosts, build artefacts | Best-effort cleanup scripts |
| **Always-on Ansible Tower / AWX** | Platform process | Vault-decrypted secrets in process memory | Within the platform's job isolation |
| **Dashboard's one-shot runner** | None — container is gone | Tmpfs, lifetime of run | Container per run; no shared FS |

This isn't unique to the dashboard at the technical level — the
underlying primitives (Fargate / ACI / Cloud Run Jobs / `docker run
--rm`) have been around for years. What's notable is the design
decision to *only* offer ephemeral runners. There's no escape hatch in
the codebase for "give me a long-lived worker for performance reasons."
You pay a one-second startup penalty per run; you never have to defend
a fleet of long-lived runners to a security review.

### The one long-lived process, and why it doesn't contradict this

[Remote agents](../../platform/spiffe/remote-agents.md) run a persistent container inside a customer's
private network. That looks like the exact thing this section argues against, so it is
worth being precise about why it isn't.

Read the sentence above again: no escape hatch for a long-lived worker *for performance
reasons*. An agent is not asking for persistence to save a second of startup. It asks
for it for **reachability** — you cannot launch a one-shot container inside a network
you cannot reach, so something has to already be there. That is a different
justification, and the four invariants this section actually names all survive it:

| Invariant above | How the agent holds it |
|---|---|
| Secrets fetched just-in-time | The agent stores no target credential. For a run that needs one (a cloud VM's SSH key, a dashboard-held secret), the dashboard seals it to that agent for that one job, the agent copies it into the one-shot run container, and it goes with the container. With **Release dashboard-held credentials only to SPIRE-attested agents** on, only an attested agent receives one |
| No process or filesystem outlives the run | Nothing is written per job; the only persistent state is the agent's own signing key |
| No shared user namespace between runs | Agent-executed Ansible spawns a **one-shot sibling container** per job; the supervisor never runs a playbook itself |
| Dashboard never holds the secret long | Strengthened, in fact: refs are resolved on demand and the agent fetches them for the life of one child container |

So the runner is still one-shot. What moved is the thing that launches it — from the
dashboard's Docker socket to one inside the network the target actually lives on. The
supervisor that does the launching is small, holds no credentials, and executes nothing
the dashboard sends it: the job payload is a closed allowlist of scalars and network
addresses, and its handler table is a closed dict rather than a dispatch on a string
from the wire.
