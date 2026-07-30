# Remote Agents

Every other execution path in this dashboard dials **out** to its target. That works
for a cloud API, and it works for a LAN the dashboard container happens to sit on. It
cannot work for a private network the dashboard is not in — which is why the local
Ansible runner says so outright when its Docker socket is missing:

> On-prem Kubernetes targets run in a sibling container because only this host has a
> route to the cluster — a dashboard deployed in a cloud (ECS / ACI / Cloud Run) cannot
> run them.

A remote agent inverts the direction. It is a container you run *inside* the private
network; it dials out to the dashboard, asks for work, does it, and reports back. No
inbound firewall rule, no VPN, no port forward, no exposed management interface.

## Philosophy

**1. The dashboard is not trusted by the agent.**
This is the unusual part and everything else follows from it. Elsewhere in this
codebase there is one trust domain — you run the dashboard, it holds your credentials,
and `SECURITY.md` puts a malicious administrator out of scope. An agent breaks that,
because it puts a dashboard-controlled process inside a network whose owner may not be
the dashboard's operator. So the design assumes the dashboard may be compromised and
makes that insufficient to execute anything meaningful on the LAN.

**2. The security boundary is a file you own.**
`policy.yaml` is mounted read-only into the agent and no dashboard API can read, write
or override it. It names what may be touched. It fails closed.

**3. Nothing reusable crosses the wire.**
The agent authenticates with an Ed25519 signature over one method, one path, one body,
valid for sixty seconds — not a bearer token. This is what makes it safe through a
TLS-inspecting corporate proxy, where an `Authorization` header would be captured in
the clear on every poll.

### How the dashboard implements these

| Principle | Where it shows up |
|---|---|
| Dashboard is untrusted | Job envelopes signed by a key the agent pins at enrolment (`services/agent_signing.py`); the agent verifies before parsing the payload |
| Dashboard is untrusted | No protocol field can carry executable content — `services/agent_job_meta.py` is a closed allowlist of scalars, enums and network addresses |
| Dashboard is untrusted | `HANDLERS` in `runners/agent/agent.py` is a closed dict, not a dispatch on a wire string |
| Your file is the boundary | `Policy` in `runners/agent/agent.py`; the sha256 is reported and shown on the Agents page |
| Fails closed | Missing/corrupt policy → the agent exits non-zero and runs nothing |
| Nothing reusable on the wire | `X-Agent-Signature` over method + path + sha256(body) + audience + timestamp + nonce |
| Least privilege | Agents never touch `require_permission` — its "empty permissions means unrestricted" rule is right for a pre-OIDC human, dangerous for a machine principal |
| Auditable | Every lease and completion is a hash-chained `audit_log` entry with actor `agent:{name}` |

## Architecture

```
   private network                        │            dashboard
                                          │
  ┌───────────────┐   outbound HTTPS 443  │   ┌────────────┐   ┌──────────┐
  │ dashboard-    │ ────────────────────► │   │   Caddy    │──►│   app    │
  │ agent         │ ◄──────────────────── │   │ agents.… │   │          │
  └───────┬───────┘   signed job envelope │   └────────────┘   └────┬─────┘
          │                               │                         │
          │ unauthenticated probes        │                    ┌────▼─────┐
          ▼                               │                    │  jobs    │
  k3s :6443   postgres :5432              │                    │  table   │
```

The `jobs` table is the queue in both directions. An agent job is created `queued` with
an `agent_id`; the agent leases it with the same atomic rowcount claim the local job
runner uses, streams Live Output into `job_logs`, and completes it. Because it writes
to the same tables, **an agent's output appears in the existing Live Output pane and
the existing Cancel button works** — there is no separate UI for watching one.

### Why the local runner and an agent never collide

Three independent guards, all pinned by `tests/test_agent_lease_invariants.py`:

- `agent_service.AGENT_JOB_TYPES` is disjoint from `jobs_worker.HANDLED_TYPES`, so the
  local runner's claim query never even sees an agent job;
- `job_service.create_job(agent_id=…)` **forces** `status="queued"`, in the one funnel
  every job row passes through, so no call site can forget;
- the lease filters on `agent_id` in both the select and the claiming `UPDATE`.

### Reconciling with "why one-shot runners"

[`config-management.md`](config-management.md) argues against long-lived runners. Read
its wording precisely: *no escape hatch for "give me a long-lived worker **for
performance reasons**"*. An agent is not asking for persistence for performance. It
asks for it for **reachability** — you cannot launch a one-shot container inside a
network you cannot reach, so something must already be there.

The doctrine's actual invariants are preserved. Today the agent holds no target
credentials at all (discovery does not authenticate). When agent-executed Ansible
lands, the agent will spawn a **one-shot sibling container** per job, so the persistent
process still never holds a credential and never executes operator-supplied code. The
runner stays one-shot; only the thing that launches it moved.

## Prerequisites

| Requirement | Details |
|---|---|
| TLS in front of the dashboard | Not optional. Signing gives integrity, not confidentiality. Use `docker-compose.agent.yml`. |
| A hostname agents can resolve | Pinned as the signing *audience* on first use. Changing it later means re-enrolling. |
| `remote_agents_enabled` | Setup wizard, or Settings. Off by default. |
| Docker on the agent host | Or any OCI runtime. ~80 MB image, no privileges. |
| Outbound 443 from the agent | One FQDN. Nothing else. |

## Workflow

### 1. Publish the agent endpoint

```bash
AGENT_HOSTNAME=agents.example.com docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
```

This adds Caddy on a **separate vhost that proxies only `/api/agent/*`**. The UI, the
login form, the OAuth callbacks and `/setup` stay on the internal address. The whole
internet-facing surface becomes one machine-only, HTML-free, session-free prefix.

The base stack still publishes 8001; bind it to loopback or firewall it, or the
plain-HTTP dashboard sits beside the TLS vhost and the split buys you nothing.

The overlay also sets two things you would otherwise have to reason about yourself, and
both matter here:

- **`TRUSTED_PROXY_HOSTS`** — the literal IP of the Caddy container, which is why the
  overlay pins a subnet and gives the gateway a static address. Without it the app
  ignores the proxy's headers and every agent appears to come from the gateway, which
  also flattens the login throttle's per-address cap. It must be a literal: uvicorn 0.27
  understands neither hostnames nor CIDR. Get it wrong and the app logs a warning naming
  the peer to add.
- **`PUBLIC_BASE_URL`** — the origin agents reach you on. The signing audience is
  derived from it, and an audience derived instead from an untrusted request would pin
  `http://…` into the config permanently, making every agent signature fail to verify
  with a 401 that looks exactly like a revoked agent.

### 2. Write the policy

Copy [`examples/remote-agent/policy.example.yaml`](../examples/remote-agent/policy.example.yaml)
and edit the ranges:

```yaml
targets:
  - cidr: 10.20.0.0/24
    ports: [6443, 5432, 3306, 1433]
deny:
  - 169.254.0.0/16
job_types:
  - agent_discover
limits:
  max_hosts: 1024
```

Omitting `ports` allows any port in the range. Naming them is the difference between
"may look for databases here" and "may reach anything here".

### 3. Register and enrol

**Agents → Register Agent.** You get a single-use code, valid 15 minutes, and a
copy-paste `docker run` built from the dashboard's own URL. On first start the agent
generates an Ed25519 keypair, redeems the code, pins the dashboard's public key, and
writes its identity to the state volume. The code is never needed again.

The private key never leaves the agent host. The dashboard only ever stores the public
half, so a dashboard database dump does not let anyone impersonate an agent.

### 4. Discover

**Discover** on an online agent, pick what to look for, optionally narrow the networks.
The job page streams findings as they arrive.

Discovery is unauthenticated probing, always:

| Target | Probe | Yields |
|---|---|---|
| Kubernetes | `GET /version` over TLS. 200, 401 or 403 all identify an apiserver. | version, distro (k3s/rke2/kubeadm/eks/gke/aks), cert CN |
| PostgreSQL | SSLRequest → `S`/`N` | engine, TLS support |
| MySQL / MariaDB | server greeting (sent first, pre-auth) | engine, version |
| SQL Server | TDS PRELOGIN | engine, version |
| Oracle | TNS CONNECT → Accept/Refuse/Resend | engine |
| Mounted kubeconfig | contexts enumerated, `/version` on each | version; **credentials stay in the file** |

**No probe ever attempts a login.** Authenticated probing of unknown hosts locks out
service accounts and reads like credential spraying in a customer's SIEM.

### 5. Register the findings

Findings are **never auto-registered**, and that is not a limitation — it is infeasible
by construction. `register_cluster` needs a full kubeconfig; `register_database` needs
a Password Safe managed account. An agent able to supply either would have to hold
cluster-admin or a privileged credential, which is the thing this design exists to
avoid.

So you review the findings and click **Register**, which posts to the *existing*
`POST /api/k8s/clusters` and `POST /api/databases/register` under your own credentials
and permission checks. Findings the dashboard already knows about are marked
`already_registered` — computed server-side, because an agent should never be handed
the inventory of everything else the dashboard manages.

## Best practices

- **Run in audit mode first.** `AGENT_MODE=audit` logs every job it *would* run, in
  full, and executes nothing. Two weeks of that, diffed against the policy, is usually
  what gets an agent approved by a security team.
- **Ship the agent's stdout to your SIEM.** The dashboard is not the authoritative
  audit record for an agent — if it were, a compromised dashboard could delete the
  evidence of its own compromise. The container log is the copy it cannot reach.
- **Name ports in the policy**, not just networks.
- **One agent per site**, named for the site.
- **Watch the policy hash** on the Agents page. It only changes when the file does.
- **Re-enrol rather than reuse.** Issuing a new code clears the stored public key, so
  the container being replaced stops being able to lease work immediately.

## Behind a TLS-inspecting proxy

Mount the inspection CA and point `AGENT_CA_BUNDLE` at it —
[`docker-compose.corp-ca.yml`](../docker-compose.corp-ca.yml) is the same pattern for
the dashboard. `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are honoured automatically.

**Set `NO_PROXY` for your private ranges**, or the agent tries to reach your database
subnet through the corporate proxy and fails in a confusing way.

Worth saying plainly: an inspecting proxy sees the full content of every request. That
is exactly why the agent's credential is a per-request signature rather than a bearer
token — the proxy captures nothing it could replay. This turns the corporate proxy from
an objection into a demonstration.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Cannot read the policy file` and the container exits 2 | No policy mounted. Fail-closed is deliberate. |
| `DASHBOARD_URL is http://` and it exits 2 | The agent will not sign over plaintext. Terminate TLS, or `AGENT_INSECURE_TLS=1` for a throwaway lab. |
| `the dashboard rejected this agent's signature` | Revoked, re-enrolled elsewhere, or the dashboard URL changed (the audience is pinned). Issue a fresh code. |
| Enrolment returns 400 | The code is single-use and expires in 15 minutes. Issue another. |
| Enrolment returns 404 | `remote_agents_enabled` is off. |
| Agent polls, gets 503 | Dashboard setup is not complete. The agent backs off; it is deliberately not redirected to the HTML wizard. |
| `Policy refused N of M host:port combinations` | Working as intended — the request was wider than the policy. The counts tell you which to widen. |
| Stuck `queued`, agent online | The job type is not in the agent's `job_types`. Check the policy. |
| Nothing found on a subnet you expect | Confirm the ports are in `targets`, and remember `already_registered` findings still appear. |
| Clock skew errors | Signatures are valid ±60s. Run NTP on the agent host. |
| Every agent 401s right after adding a proxy, and the dashboard logs `Ignoring X-Forwarded-*` | The proxy is not in `TRUSTED_PROXY_HOSTS`, so the audience was pinned as `http://…`. Set the variable **and** `PUBLIC_BASE_URL`, then clear the stale `agent_base_url` config key and re-enrol. |

## Where this is heading

Discovery is the first slice, chosen because no credential crosses the wire at all.
Next:

- **Agent-executed Ansible** against private targets, spawning one-shot
  `chrweav/ansible-cloud` siblings and reusing the existing
  `PLAYBOOK_B64` / `CONN_VARS_B64` env contract byte for byte, plus a just-in-time
  secret fetch gated on refs the job declared at enqueue time.
- **Password Safe JIT checkout by the agent**, so it holds exactly one credential whose
  only power is to ask Password Safe — subject to its policy, approval workflow and
  session recording. An on-prem agent with zero standing target credentials.
- **On-prem hypervisors.** Blocked on something upstream: `config.py` holds hypervisor
  connections as global singletons (one `proxmox_host`, one `vsphere_host`), so N sites
  × M hypervisors needs a multi-instance config refactor that is larger than the agent
  itself. When it lands, the shape is scheduled inventory sync plus a closed verb
  allowlist — not a generic proxy, which is remote code execution with extra steps.
