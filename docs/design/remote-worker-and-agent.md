# Design: how far a remote agent can replace the remote worker

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are deciding whether a piece of remote work should run on the one-shot remote worker or on a SPIFFE-attested remote agent, or you are about to move a workload from one to the other.

**Phase 1 built; phases 2 and 3 not built.** This note assesses the two execution paths as
they stand after agent 2.11 (lifecycle, ongoing checks and cloud node attestation). It
recommends what to move, what to leave alone, and in what order. Of the
[phases](#recommendation-agent-first-worker-kept), only phase 1 is built. The user-facing comparison is
[Where work runs](../platform/where-work-runs.md); this page is the reasoning behind
changing it.

## The question

The dashboard has two ways to do work that must happen beside infrastructure rather than
through a cloud API:

- **The remote worker:** a one-shot container per job, in the target's cloud. That is ECS
  Fargate, Azure Container Instances or a Cloud Run job, plus the local sibling runner on
  the dashboard host.
- **The remote agent:** a long-running container inside a network. It attests through
  SPIRE, leases signed jobs, and runs each Config Management job in a one-shot sibling
  container beside it.

Since 2.11 an attested agent has a lot behind it:
- an identity that never touches disk, and is re-attested daily;
- per-job sealed credentials, released only to attested agents;
- signed, host-gated upgrades;
- checks that catch a cloned or drifting agent;
- for hosts the dashboard deploys, cloud instance identity with no secret in user-data.

That makes it tempting to route everything through agents. The question is how much of the
worker's work an agent can realistically take, what that buys, and what it costs.

**Short answer:**
- **Cloud-VM Config Management** can move to agents today, and it is better there.
- **Cloud database plays** can follow with a modest change.
- **Kubernetes operations** can move only to agents the dashboard itself owns, because they
  carry cluster-admin credentials.
- **Image promotion** should stay on the worker.

Keep both paths. Make the agent the first choice where one exists, and keep the worker as
the fallback and for the heavy, bursty jobs.

## What the worker does today, and whether an agent can take it

| Workload | Worker path today | Agent today | Verdict |
|---|---|---|---|
| **Config Management on cloud VMs** (SSH/WinRM): playbooks, plus Windows Arc join and the GCP directory DNS link, which ride the same runner | `ansible_local` → `_dispatch_cloud_runner` → Fargate/ACI/Cloud Run, image `chrweav/ansible-winrm` | **Yes, already.** Config routes send a VPC's VMs to an agent in it (`agent_ansible`, run kind `vm`). Dashboard-deployed agents create their own routes | **Move: agent-first** |
| **Localhost plays against databases** | `ansible_cloud_run` → in-cloud runner, image `chrweav/ansible-cloud` | **Partly.** Run kind `database` exists, but only for database rows bound to an agent (`row.agent_id`). Cloud-provisioned databases have no route | **Move, after a small change** ([phase 2](#phase-2-databases-in-a-cloud-through-routes)) |
| **Localhost plays against Kubernetes clusters** | `ansible_cloud_run`, kubeconfig in `KUBECONFIG_B64` | **No.** `target_kind == "k8s"` is refused by name. The bundle forbids `ansible_connection: local` and `ansible_*` variables, because kubectl and helm are on the image's PATH | **Owned agents only** ([phase 3](#phase-3-kubernetes-on-dashboard-owned-agents)) |
| **Kubernetes operations** (`kubectl`/`helm`): ESO install, Entitle agent, SA token mints, RBAC, Rancher import, Entra/impersonator bindings | `k8s_runner_service.run`, image `dtzar/helm-kubectl`, a cluster-admin kubeconfig in the environment | **No.** No handler and no policy key launches a kubectl image | **Owned agents only** (phase 3) |
| **Rancher API through a runner** | `rancher_api_runner`: one-shot `curl -K -` from inside the node's cloud, to get past a TLS-inspecting proxy | **No** | **Leave**, or fold into phase 3 |
| **Image promotion** (convert a VM image and upload it into a cloud's image library) | `promote_runner_service`: 1 vCPU/4 GB with a 100 GiB disk on ECS, 4 vCPU/16 GiB on Cloud Run; 15–60 minutes; writes with cloud credentials | **No** | **Keep on the worker** ([why](#why-image-promotion-stays-on-the-worker)) |

Work the agent already does and the worker never could is unaffected:
- hypervisors behind a firewall;
- on-premises discovery;
- brokered file shares;
- the BeyondTrust Gateway.

## What moving to the agent buys

The user's instinct is right: for the work an agent can take, it is the more efficient
path. The concrete reasons, with the numbers the code has:

1. **It frees the dashboard's job worker.**
   - A cloud-VM run on the worker is an `ansible_local` job. That is a **heavy** job, and it
     holds one of at most **four** heavy slots per worker process (`HEAVY_CAP_CEILING`) for
     the whole run.
   - The run polls the cloud every 10 seconds for up to 20 minutes.
   - An agent leases `agent_ansible` jobs directly. They are not in `jobs_worker.HANDLED_TYPES`
     and take no worker slot at all.
   - On a large estate this is the binding constraint, and an agent removes it.
   - Capacity moves to the agents: up to 8 concurrent runs per agent, and as many agents as
     there are networks.
2. **No cold start.**
   - The agent is already running and polls every 5 seconds.
   - A worker run pays the cloud's container provisioning on every job.
   - This repo never measured that: the docs' "one second" and "90-second lifespan" are
     assertions, not measurements. ACI's create blocks until the group exists, and Fargate
     and Cloud Run add image pull and scheduling.
   - Expect seconds to low minutes per run on the worker, against the next poll on an agent.
3. **A better run.**
   - **Live Output streams, and Cancel works mid-run.** The cloud runners collect output
     only after the task stops.
   - **Become methods work.** The cloud runners refuse them: "no channel for play
     variables".
   - **Every asset type runs on the agent:** `.sh`, `.ps1`, `.rpm`, `.deb`, Windows packages
     and local or UNC asset storage.
     - The cloud runner takes only bare-IP `.yml` playbooks.
     - Anything else quietly falls back to the dashboard host's local runner, which may not
       reach the VM at all.
4. **Stronger credential delivery.**
   - **On the worker,** the SSH key and Password Safe values go in as environment
     variables:
     - in the ECS RunTask override, which is readable through `DescribeTasks`;
     - as ACI `secure_value`;
     - as a plain variable on the per-run Cloud Run Job, deleted afterwards.
   - **On the agent,** the same key, from the same `cloud_ssh_key_for_deploy`, is assembled
     into a typed bundle and sealed for that agent, that audience, that job and that
     endpoint (X25519, HKDF and AES-256-GCM).
     - It is copied into the run container through the Docker archive API, never as an
       environment variable.
     - With `dashboard_secrets_require_spire`, only an agent whose key came from an
       attestation receives it.
5. **One path everywhere.** The same agent, policy, sealing and Live Output serve on-prem
   and every cloud. The worker needs a cluster, subnet, security group and roles in each
   cloud, and each cloud's runner has its own quirks:
   - ECS needs the playbook in the task definition because of an 8 KB override cap;
   - Cloud Run's `/tmp` is memory-backed;
   - ACI needs a delegated subnet.
6. **The identity story is now complete.**
   - Every start attests, and an attested agent re-attests daily.
   - A second copy is caught, and revoking evicts it from SPIRE.
   - Upgrades are signed and gated by the host.
   - A deployed host proves itself with its cloud's identity document.

   A persistent process on a network used to be the weak point in the argument for one-shot
   runners ([runners.md](../operations/config-management/runners.md#why-one-shot-runners-the-security-argument)).
   It now carries more evidence of what it is than a Fargate task's role does.

## What it costs

Moving work to agents is not free, and some of these costs are why the worker stays.

1. **A persistent host with a Docker socket.**
   - To run one-shot siblings the agent needs the Docker socket, which is root on that host.
   - A worker task has no host to compromise between jobs.
   - The run itself is still one-shot: a sibling container per job, read-only root, all
     capabilities dropped, 1 GiB, 512 PIDs. But the thing that launches it now persists.
   - For dashboard-deployed agents the host is the dashboard's and can be torn down between
     change windows. For a customer's agent it is theirs to patch.
2. **Always-on cost, or a slow first run.**
   - **An agent in `always` mode** is a VM running all the time: `t3.medium`,
     `Standard_B2s` or `e2-medium` by default, plus NAT egress while it is up.
   - **In `window` mode** nothing runs between windows, but the host starts 20 minutes
     before a window. An ad-hoc run outside a window is refused rather than started, so
     window mode trades the cold start for a much colder one.
   - The worker costs nothing when idle and scales per job.
3. **Fixed capacity per network.**
   - **The worker is elastic.** A 200-VM bulk run fans out to as many tasks as the
     dashboard's slots allow.
   - **An agent is capped.** It runs at most 8 at once at about 1 GiB each, so a large
     burst queues on it.
   - Scaling an agent means a bigger host or more agents with split routes. Today that is
     manual.
4. **One agent per network, and a hard failure mode.**
   - An agent reaches only what its network reaches, so every isolated VPC or VNet needs its
     own.
   - **A run routed to an offline agent is refused.** "this run would sit queued
     indefinitely". It does not fall back to the worker. A dead agent stops
     configuration for its whole range until someone notices.
   - The worker has no equivalent single point.
5. **SPIRE becomes infrastructure you depend on.**
   - The efficient, secure version of the agent is the SPIRE-attested one. That needs:
     - the dashboard's SPIRE server up;
     - its port reachable from every agent host;
     - for cloud attestation, the server's own read-only cloud access.
   - A site behind a TLS-inspecting proxy can't reach SPIRE. It falls back to Ed25519, and
     with `dashboard_secrets_require_spire` on it gets no credentials, so it can't take
     cloud-VM runs at all.
6. **Log volume on the database is not reduced.**
   - [Where work runs](../platform/where-work-runs.md#scaling-configuration-in-the-cloud)
     says agents send Live Output "in batches … rather than one database write per line".
     **That is not what the code does.**
     - `agent_service.append_logs` writes and commits each line individually.
     - Batching saves HTTP requests, not database writes.
     - The cloud runners write no per-line logs at all: they store the output once, at the
       end.
   - So moving runs to agents trades a held worker slot for per-line log writes. Streaming
     is the feature; it is not free.
   - If agents carry most runs, `append_logs` should write each batch in one transaction.
     That is a small change, and worth making first.
7. **The trust model limits what can move.**
   - An agent runs on a network the dashboard does not control, and the design assumes the
     dashboard may not trust it.
   - Kubernetes operations send a cluster-admin kubeconfig. Image promotion sends cloud
     write credentials. Neither belongs on a customer-hosted agent.
   - That is why phase 3 is limited to agents the dashboard deploys and owns.

### Why image promotion stays on the worker

- **The job is heavy and rare.** One conversion wants a 100 GiB scratch disk, 4–16 GiB of
  memory and libguestfs, for 15 to 60 minutes. An agent host would have to be sized for
  that all the time, or the job would starve its Config Management slots. That is exactly
  the burst a per-job container exists for.
- **It writes into a cloud with that cloud's credentials:** an S3 put role, an Azure
  service principal, OCI API keys.
  - Moving it to an agent means either:
    - shipping those credentials to it; or
    - federating the agent's SVID into each cloud's IAM, which the dashboard already does
      for its own SPIFFE identity.
  - The second is possible, but it is a cloud-trust design of its own. It would serve a
    workload that gains nothing from persistence.

## Recommendation: agent-first, worker kept

Keep both paths: the user prefers that, and the costs above justify it. Make the agent the
default wherever one is present and healthy, and keep the worker as the fallback and for
heavy, bursty work.

### Phase 1: make the existing overlap agent-first (small, built)

1. **Fall back to the worker when the routed agent is offline.**
   - A cloud route has an opt-in: **Fall back to the remote worker when the agent is
     offline** (`fallback_to_worker`). An on-premises route cannot have it, because the
     worker has no path to an on-premises network.
   - With it on, a run routed to an offline agent goes to the worker instead of being
     refused. A deployed agent's run booked into a time its host will be up stays on the
     agent. The job records `fallback_from_agent`, and Live Output says so.
   - Only a playbook falls back; anything else is refused by name, since the worker runs
     only playbooks against a cloud VM.
   - This removes the single point of failure without giving up agent-first.
2. **Write Live Output batches in one transaction.** `agent_service.append_logs` calls
   `job_service.append_job_logs`: one commit per request, not one per line.
3. **Correct the docs** that this note found wrong. All four are fixed.
   - **Live Output (fixed):** the batching claim in
     [Where work runs](../platform/where-work-runs.md).
   - **The agent's credentials (fixed):**
     [runners.md](../operations/config-management/runners.md) said the agent "holds no
     target credentials at all today". It now receives sealed per-job SSH keys and
     dashboard secrets.
   - **The runner's network (fixed):** `shared-cloud.md` recommended a private subnet with
     NAT, but the AWS runners always set `assignPublicIp: ENABLED`. The new setting
     `ansible_ecs_assign_public_ip` (on by default, so nothing changes) lets a private
     subnet with NAT work, and the doc now describes both.
   - **Where the key lives (fixed):** runners.md said the SSH key "lives in the side-car
     container's tmpfs". It now says what each runner does: the cloud task's ephemeral
     filesystem, the local runner's 0600 per-run temp directory, and the agent's run
     container through the archive API.

### Phase 2: databases in a cloud, through routes

- **The change.** Let config routes cover cloud-provisioned databases, as they cover cloud
  VMs: match by the database's private address within its cloud scope, and send the run as
  run kind `database` to the routed agent.
- **What it reuses.** The agent's `ansible.db_image` (`chrweav/ansible-cloud`) and the
  sealed `login_*` bundle already exist.
- **Where it goes.** Through the same routes table and picker, so cloud-VM routes and
  cloud-DB routes stay one concept.

### Phase 3: Kubernetes on dashboard-owned agents

This is the only phase that changes the trust model, so it needs a decision before it is
built.

- **A new class of agent.**
  - An agent the dashboard deployed (`AgentHost`), attested through SPIRE with its cloud's
    instance identity, runs on a host the dashboard owns and created.
  - Call it an **owned agent**.
  - It may receive what a customer's agent never may: a sealed cluster-admin kubeconfig for
    one job.
- **A new handler,** `agent_k8s`.
  - It launches a kubectl/helm image pinned in the owned agent's generated policy.
  - The image is pinned by digest and signed, like the agent.
  - It takes a kubeconfig and a manifest or Helm call, sealed per job, the same way the
    Ansible bundle is.
  - The handler set stays closed: only a fixed set of operations the dashboard already
    performs, never an arbitrary command.
- **What then moves.** These run through the owned agent in the cluster's VPC:
  - the Kubernetes runner operations (ESO, Entitle, SA tokens, RBAC, bindings);
  - Kubernetes localhost plays;
  - the Rancher runner transport.

  They get no cold start and no held worker slot, and they reach private API endpoints
  without the worker's cluster allow-list entry.
- **The hard rule.** A customer-hosted agent never qualifies, whatever its policy says. The
  dashboard decides "owned" from its own `agent_hosts` row, never from anything the agent
  reports.

### Not planned

- **Moving image promotion,** for the reasons above.
- **Retiring the worker.** It is the only path that:
  - costs nothing when idle;
  - is elastic under a burst;
  - has no host to compromise between jobs;
  - needs no SPIRE.

  Those are real properties, and the docs should keep presenting both paths as first-class.

## The decision

Phase 3's **owned agent** is a new trust class: an agent that may receive cluster-admin
credentials because the dashboard built and owns its host. **Decided: owned agents only.**
An agent the dashboard deployed may receive a sealed cluster-admin kubeconfig for one job;
a customer-hosted agent never may. The dashboard decides "owned" from its own
`agent_hosts` row, never from anything the agent reports.
