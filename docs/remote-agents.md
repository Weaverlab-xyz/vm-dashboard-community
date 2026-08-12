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
| Bounded, even when authenticated | `services/agent_guard.py` caps signed requests per *agent* and failed enrolments per address — the only throttle in the app, on the only surface that faces a hostile network |
| Only a trusted caller pins the audience | `_resolve_audience(persist=…)`: an admin minting a code, or a valid enrolment. An unauthenticated poll cannot freeze a value of its choosing into the config |

## The agent's private key

The agent generates an Ed25519 keypair on first start and keeps the private half. Only
the public half is ever sent, so **a dump of the dashboard's database cannot impersonate
an agent** — there is nothing in it to replay.

Where it lives, exactly: `$AGENT_STATE_DIR/identity.json`, created mode 0600 inside a
0700 directory owned by uid 10001, written to a temporary file and renamed so it never
exists world-readable even for an instant. It is **not** encrypted at rest: no passphrase,
no TPM, no keyring. That is a deliberate limit rather than an oversight — a passphrase an
unattended container must read at boot has to be stored next to the key it protects.

Who can read it:

| Who | Can read the key |
|---|---|
| `root` on the agent host | **Yes.** `docker cp`, `docker exec`, the volume on disk, `/proc/<pid>/mem`. File modes do not constrain root. |
| Anyone in the `docker` group | **Yes** — socket access is root-equivalent, with or without this agent. |
| Another container on the host | No, unless it mounts the state volume or the Docker socket. |
| Anyone on the network, including a TLS-inspecting proxy | No. The key never crosses it, and a signature is good for one method, one path, one body, sixty seconds. |
| The dashboard, or whoever compromises it | No. It only ever received the public half. |

**What a stolen key actually grants**, which is the part worth being precise about: it
authenticates five `POST /api/agent/*` routes and nothing else. The holder can lease jobs
an operator already queued *for that agent*, heartbeat them, push log lines, and complete
them. They cannot create a job (that needs an admin session), learn anything about what
else the dashboard manages, reach any user-facing route, or touch another agent's jobs —
`owned_job` filters on `agent_id` and answers "not found" identically for "not yours", so
the API is not even an oracle for their existence. In this phase the agent holds no target
credentials, so there is nothing on the LAN to inherit either.

That leaves two real consequences: denial of service against that one agent's queue, and
the ability to **report false findings**. Findings are data a human reads and acts on, so
treat them as untrusted input — which is the other reason nothing is auto-registered.

Revocation does not depend on reaching the container. **Revoke** clears the stored public
key, so the next poll fails verification whatever that container is still doing, and any
job it held is failed immediately rather than at the next reconcile.

### Running under Podman

Rootless Podman is a real improvement here and worth preferring — as long as it is
recommended for the right reason. It changes *who* can steal the key. It does not change
whether host root can.

What it actually buys:

- **No daemon socket**, so the `docker` group row above disappears entirely. In most
  shops that group is the larger of the two exposures.
- The state volume lives under `~/.local/share/containers/storage/volumes/…`, owned by one
  unprivileged user rather than by root, with uid 10001 mapped through that user's subuid
  range.
- Every hardening flag in the emitted command works unchanged — `--read-only`,
  `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user`, `--tmpfs`. Substitute
  `podman run` for `docker run` and paste it as-is.

Two things to know before you do:

- **On SELinux hosts the bind mounts need a label suffix** — `:ro,Z` — or the container
  cannot read `policy.yaml`. Podman's natural home is RHEL-family hosts, where SELinux is
  enforcing by default, so this is the common case rather than the exotic one. The emitted
  command already carries it, and Podman ignores `Z` where SELinux is absent; if you are
  pasting an older command, add it. That fails closed (`Cannot read the policy file`,
  exit 2), so it is a confusing five minutes rather than a hole — see
  [Troubleshooting](#troubleshooting) for how to recognise it, because the dashboard-side
  symptoms all look like networking.
- `--restart unless-stopped` does not survive a reboot on its own; rootless Podman needs
  `systemctl --user enable podman-restart` (and `loginctl enable-linger`) because there is
  no daemon to do it for you.

What neither runtime fixes is host root. If that is the threat you are defending against,
the answer is not a different container runtime — it is a credential that cannot be
copied, sealed to hardware, which is a future feature rather than a configuration. Until
then the honest position is the blast-radius table above plus fast revocation.

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
  vCenter :443  Proxmox :8006            │                    │  table   │
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
| `remote_agents_enabled` | **Settings → Integrations → Remote Agents**, or the setup wizard. Off by default — the nav link and the whole `/api/agent` router are hidden until you turn it on. |
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

  Only two callers are allowed to pin that value: an admin minting an enrolment code, and
  a successful enrolment. An unauthenticated poll — including one arriving at the
  plain-HTTP 8001 with a forged `Host` — resolves an audience but never writes it down, so
  a stranger cannot pin one of their choosing and 401 your whole fleet. Set
  `PUBLIC_BASE_URL` regardless: it is what makes the pinned value *correct* rather than
  merely trusted, and it is the only way the value is right when the operator's browser
  and the agents reach the dashboard on different names.

#### If the hostname is internal

Caddy's automatic HTTPS means ACME, and ACME needs the name to resolve publicly with
port 80 reachable. A lab name — `agents.lab.internal`, a split-horizon domain — can
never satisfy that, so Caddy retries forever and serves nothing, and the agent (which
refuses plain HTTP) cannot connect at all. This is the most likely reason a first run
never gets started.

Set `AGENT_TLS_INTERNAL=1` and Caddy issues from its own CA instead, immediately and
offline:

```bash
AGENT_HOSTNAME=agents.lab.internal AGENT_TLS_INTERNAL=1 \
  docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
```

Then give the agents that CA — it is in no system trust store:

```bash
docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml \
  cp agent-gateway:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Copy it to the agent host, mount it, and set `AGENT_CA_BUNDLE` to the mounted path.

Prefer this over `AGENT_INSECURE_TLS=1`. The insecure switch also disables verification,
which is the part most worth exercising, and it masks a wrong `PUBLIC_BASE_URL` or
`TRUSTED_PROXY_HOSTS` until an agent starts 401ing for reasons that look like
revocation. The root is generated on first start and lives in the `caddy_data` volume —
it survives restarts but not `docker compose down -v`, and wiping it means re-issuing
the root to every agent.

### 2. Write the policy

Copy [`examples/remote-agent/policy.example.yaml`](../examples/remote-agent/policy.example.yaml)
and edit the ranges:

```yaml
targets:
  - cidr: 10.20.0.0/24
    ports: [443, 8006, 9440, 5985, 5986]
deny:
  - 169.254.0.0/16
job_types:
  - agent_discover
limits:
  max_hosts: 1024
```

Omitting `ports` allows any port in the range. Naming them is the difference between
"may look for databases here" and "may reach anything here".

Mount it `:ro,Z`, as the emitted command does. The `Z` is the SELinux relabel and it is not
cosmetic: on Fedora, RHEL, CentOS, Rocky or Alma a bind mount keeps its host label, and the
container may not read a file in your home directory however permissive its mode is — the
agent then exits 2 on `Permission denied` against a mode 644 file. Docker and Podman ignore
`z`/`Z` where SELinux is absent, so it is safe on every host.

### 3. Register and enrol

**Agents → Register Agent.** You get a single-use code, valid 15 minutes, and a
copy-paste `docker run` built from the dashboard's own URL. On first start the agent
generates an Ed25519 keypair, redeems the code, pins the dashboard's public key, and
writes its identity to the state volume. The code is never needed again.

The private key never leaves the agent host. The dashboard only ever stores the public
half, so a dashboard database dump does not let anyone impersonate an agent.

The default command passes the code as `-e AGENT_ENROLLMENT_CODE`, which is convenient and
has one drawback worth knowing: an environment variable is baked into the container's
config, so `docker inspect` still shows the code long after it has been spent. The modal
offers a second form that mounts it as a file and sets `AGENT_ENROLLMENT_CODE_FILE`
instead, leaving nothing durable in Docker's metadata; delete the file once the agent shows
Online. To keep the code out of your shell history as well, write that file with an editor
rather than pasting the `printf` line.

Because the container runs as uid 10001, that file has to be readable by it — a mode 0600
file owned by your host user is *not*, and the agent says so and exits rather than failing
mysteriously. A single-use secret with a fifteen-minute lifetime in a directory you chose
is an acceptable trade for that; a long-lived one would not be.

#### The signing audience is pinned by that first code

The URL in the command you just copied is the **signing audience**: the value every agent
signature is checked against from then on. The first enrolment code minted on an install
pins it, permanently, and nothing later overwrites it — see
[§1](#1-publish-the-agent-endpoint) for which two callers may pin and why an
unauthenticated poll must not.

Which means the *first* code decides it, and on a split-vhost deployment the default is
wrong. Settings → Integrations → Remote Agents pre-fills **Public base URL** from the origin
your own browser is on, and on the layout §1 recommends — UI on the internal address,
`/api/agent/*` on its own vhost — that is the **UI** hostname. Pin that and the agent dials
a vhost which does not serve `/api/agent`, gets a 404, and the console shows it `enrolling`
with *Last seen: never* and no policy hash. Nothing distinguishes it from a revoked agent,
and there is no `/api/agent/enroll` line in the dashboard log to contradict you, because no
request ever arrived. Correcting Public base URL afterwards appears to work and changes
nothing, because the pin wins.

So the panel shows the pinned audience read-only, beside — and clearly distinct from — the
Public base URL field:

- **Pinned to** — the audience itself, and the number of enrolled agents signing against it.
- **A warning** when it is the same origin you are browsing (right for a single-hostname
  install, the failure above for a split vhost), when nothing is pinned yet and the next
  code is about to decide it, or when it is `http://`.
- **Reset signing audience** — clears the pin so the next enrolment code pins it again from
  Public base URL. This is the only mutation offered: there is no way to *set* the audience
  from an API, because a settable audience is the write-once rule with extra steps. Admin
  only, the same gate as minting a code.

  It **invalidates every enrolled agent.** Each signs against the audience it was handed at
  enrolment, so all of them start getting 401s — which an agent reads as "revoked" and stops
  on. Every one needs a fresh enrolment code afterwards. The confirm dialog names the count.

Registering an agent is **refused with a 409** while the pin contradicts Public base URL,
rather than handing you a command built from the stale value. Nothing is created by the
refusal — the check runs before the agent row exists, and before a re-issue clears the
existing key — so a blocked re-issue leaves the running container working. Resolve it by
setting Public base URL back to the pinned value, or by resetting the audience.

A divergence is not *always* a mistake, though, which is why the refusal is not a dead end.
`PUBLIC_BASE_URL` is one value doing two jobs — the OAuth callback origin as well as the
agent audience — and a split vhost is exactly the case where those want different hostnames.
Confirming the prompt re-issues with `?acknowledge_audience=true`, which mints against the
**pinned** audience, labels the command with which URL it used, and logs and audits the
exception. It does not move the pin: the override is permission to use the pinned audience,
not to re-pin, because re-pinning would invalidate every enrolled agent as a side effect of
registering one new one.

The same information is available to a script at `GET /api/agent/audience`, and the reset at
`DELETE /api/agent/audience`; both are admin-only.

### 4. Keep an eye on who is enrolled

The agents table answers "is this one mine?" without leaving the page:

| Column | What it tells you |
|---|---|
| **Registered by** | The admin who created the row. Highlighted amber when it was not you — nothing can appear here that an admin did not deliberately register, so an unfamiliar name means *another operator*, not an intruder. |
| **Source IP** | Where the container is actually polling from. An address you do not recognise is the clearest signal to look closer. |
| **Status** | Derived from the last poll, never stored. `enrolling` means a code was issued but never redeemed. |

Expand a row for the agent id, when it was registered and enrolled, and its
**policy hash** — the sha256 of the `policy.yaml` on the agent host, self-reported on
every poll. The dashboard cannot change it, so a value that moves means the file was
edited on the host. That is the only signal you get that a compromised agent rewrote its
own allow-list.

**Revoke** stops an agent immediately: its key is cleared, its running job is failed and
its queued ones are cancelled, and no revoked agent can ever come back. The row stays, so
the history of what it did stays with it.

Once revoked, **Remove** deletes the row. Its only purpose is to free the name —
registration enforces uniqueness across every row, so a revoked `lab-dc1` would otherwise
squat that name forever. Job history survives the deletion; those jobs simply stop naming
an agent, and the audit log records agents by name so it is untouched. Revoked rows are
hidden by default; untick **Hide revoked** to see them.

### 5. Discover

**Discover** on an online agent, pick what to look for, optionally narrow the networks.
The job page streams findings as they arrive.

Discovery looks for **hypervisor management endpoints**, and it is unauthenticated
probing, always:

| Target | Port | Probe | Version? | Confidence |
|---|---|---|---|---|
| vCenter / ESXi | 443 | `GET /sdk/vimServiceVersions.xml`, then a constant `RetrieveServiceContent`. `apiType` separates vCenter from a bare ESXi host. | **yes** — full name, API version, build | confirmed |
| XCP-ng / XenServer | 443 | `GET /` → the `Server: Xapi/…` header | **yes** | confirmed |
| Proxmox VE | 8006 | `GET /` → `Server: pve-api-daemon` + the page title | no — `/api2/json/version` needs auth | confirmed |
| Nutanix Prism | 9440 | `GET` on the v3 API → a 401, plus the certificate | no — nothing anonymous reports AOS | confirmed |
| WinRM | 5985 / 5986 | `GET /wsman` → 401 + `Server: Microsoft-HTTPAPI` | no | **possible only** |

443 is shared by vSphere and XCP-ng, so the agent probes each host:port **once** and
classifies whichever answered.

**WinRM is the honest limit of the set.** It identifies WinRM on Windows — and nearly
every domain-joined Windows Server has WinRM enabled, the overwhelming majority of them
not hypervisors. Those findings are marked *possible only* and rendered differently.
There is a way to read the OS build, NetBIOS name and DNS domain anonymously (an NTLM
type-1 negotiate token carries no credential, and the type-2 challenge answers with AV
pairs) and the agent deliberately does not: it initiates an authentication exchange and
lands in the Windows Security log as a logon event. A test bans the token by name.

**No probe ever attempts a login.** Authenticated probing of unknown hosts locks out
service accounts and reads like credential spraying in a customer's SIEM.

Not probed, and not by omission: **Proxmox Backup Server** (8007) and **oVirt/RHV**
answer on ports in this list and are returned as *unidentified* rather than mislabelled.
**VMware Workstation** is absent because a desktop hypervisor exposes nothing on the
network — a probe for it would be code that never returns anything.

Databases and Kubernetes clusters are **no longer discovered by an agent**. Password
Safe already finds databases with managed credentials — it knows the platform, port and
accounts, which a socket probe never could — so
[Import from Password Safe](databases.md#importing-from-password-safe) replaced that
path. Kubernetes clusters are registered from a kubeconfig, which no credential-less
probe could ever supply.

### 6. Add the connections

Findings are **never auto-registered**, and that is not a limitation — it is infeasible
by construction. A connection needs a credential, and a probe by definition has none.

So you review the findings and click **Add connection…**, which prefills the
[Connections](#hypervisor-connections) form with the host, port and a suggested name and
runs under your own credentials and permission checks. Findings the dashboard already
has a connection for are marked `already_registered` — computed server-side, because an
agent should never be handed the inventory of everything else the dashboard manages.

One limitation worth knowing: a connection configured with an FQDN but discovered by IP
reads as new, because the dashboard cannot resolve your private DNS — it is not on that
network, which is the whole reason the agent exists. The prefill uses the IP the probe
connected to, so a second scan matches.

## Hypervisor connections

Once an agent is enrolled it can also **broker hypervisor operations** — inventory sync
on a schedule, and power verbs on demand — for vCenter, Proxmox, Nutanix and XCP-ng
endpoints the dashboard has no route to.

### The credential is the agent's, never the dashboard's

A dashboard connection row bound to an agent stores the agent's **name for it** and
nothing else: no host, no username, no password. The job says *"run `inventory_sync` on
`dc1-vcenter`"* and the agent resolves the rest from its own
[`connections.yaml`](../examples/remote-agent/connections.example.yaml).

That is the difference between this and a proxy. Shipping credentials in the job
envelope would put a standing vCenter administrator password in the dashboard's
database, in every job payload, in the agent's memory — and, because **envelopes are
signed rather than encrypted**, in the logs of any TLS-inspecting proxy in between. The
[whole feature is sold](#behind-a-tls-inspecting-proxy) on the opposite property.

The cost is two files joined by a string, and a typo in either yields
`unknown connection 'dc1-vcenter'`. The connection form mitigates that by offering an
agent picker rather than a free-text uuid.

### Three grants, all required

Nothing runs unless all three agree, and they belong to different people:

| Grant | Who owns it | Where |
|---|---|---|
| this agent may run `agent_hypervisor` | the dashboard operator | Agents page |
| this verb is allowed on this connection | **you** | `policy.yaml` → `connections:` |
| this connection exists, with a credential | **you** | `connections.yaml` |

Withhold any one and nothing happens. A refusal from the second or third arrives in
Live Output naming the file and the line to add — the dashboard cannot fix it and does
not pretend to.

### The verbs

`inventory_sync` is read-only and runs on a schedule (default every 30 minutes;
override per connection with `options.sync_interval_minutes`). `power_on`, `power_off`,
`power_reset` and `restart` are on-demand, issued by the existing power buttons on the
hypervisor pages when the resolved connection is agent-bound.

**`snapshot`** creates a snapshot named `dash-<job id>`. The name is *generated*, never
supplied — which is exactly why it was held back at first: a created thing needs a name,
and a name is a free-form string. There is still no field in the protocol through which
operator text could reach a hypervisor, and the job id makes the snapshot traceable back to
the row that made it, which a typed name would not be.

Deliberately absent: **deploy / clone / delete / console** — they need sizes, networks and
cloud-init, a payload shape indistinguishable from a config file, and a config file is one
step from a script. Those stay dashboard-direct.

`power_off` and `power_reset` are separate verbs rather than one verb with a `force`
flag, because a boolean on a destructive verb gets defaulted wrong exactly once.

### What the agent can and cannot reach

| Product | Transport | Inventory | Power |
|---|---|---|---|
| vCenter | vSphere Automation REST API | yes | yes |
| Proxmox VE | `/api2/json` + API token | yes | yes |
| XCP-ng | XAPI (stdlib XML-RPC) | yes | yes |
| Nutanix Prism | Prism v3 REST | yes | no — a v3 power change is a full spec PUT with a metadata version, not an action |
| VMware Workstation Pro | `vmrest`, on the same host | yes | on/off only — vmrest has no reset, reboot or snapshot |
| bare ESXi | SOAP, via the [sibling runner](#the-sibling-runner) | yes | yes |
| Hyper-V | WinRM, via the [sibling runner](#the-sibling-runner) | yes | yes |

`esxi` is a distinct connection kind from `vsphere` on the agent's side even though the
dashboard has only `vsphere`: same product, different transport, and the agent is the only
side that knows which one a given endpoint is. A job for `vsphere` is served by an `esxi`
connection; the reverse is refused, because that direction would send SOAP work to a
vCenter.

Four of six products over REST, and **no new agent dependency for any of them**. That is
why the agent image still installs only `requests`, `PyYAML` and `cryptography`: those two
audit tests are the security argument in executable form. The remaining two get their heavy
dependencies in a container that lives for seconds, which keeps the long-lived supervisor
inert — the property `test_the_agent_imports_no_execution_machinery` protects.

### Where the credential comes from

Three sources, and the order matters: `ps_managed_account` beats `password_file` beats an
inline `password`. An operator who has moved a connection to Password Safe should not
silently keep authenticating with a stale literal left in the file underneath it.

With `ps_managed_account`, **the agent holds no hypervisor credential at all** — only a
Password Safe OAuth client whose single power is to ask for one. Each job checks a
credential out and checks it back in, so every use lands in Password Safe's audit trail
and is subject to its policy and approval workflow. That is the end state this design was
heading for: an on-prem agent with zero standing target credentials.

Mount the client alongside the other two files — see
[`passwordsafe.example.yaml`](../examples/remote-agent/passwordsafe.example.yaml). The
dashboard never issues it and never sees it.

A check-in failure is swallowed deliberately: the credential has already been used and the
request expires on its own duration, so failing a completed job over it would be the wrong
trade. An account that requires human approval will not release, and the job fails rather
than hanging — use auto-approve policies for accounts an unattended agent needs.

### The sibling runner

Hyper-V and **bare ESXi** are the only two the agent cannot talk to itself: WinRM is SOAP
with NTLM/Negotiate, and a standalone ESXi host serves only the SOAP API. Rather than put
a real auth stack and pyVmomi into an image whose three-dependency restraint *is* the
security argument, those two run in a one-shot container the agent creates, reads one line
of JSON from, and deletes.

**This needs the Docker socket, and the socket is root on the host.** It is not mounted by
the default deployment — [`docker-compose.yml`](../examples/remote-agent/docker-compose.yml)
still promises the agent launches nothing, and that stays true. Turning it on is a
separate, deliberate act:

| Grant | Who | Where |
|---|---|---|
| the `agent_hypervisor` job type | dashboard operator | Agents page |
| `sibling: {enabled, image}` | **you** | `policy.yaml` |
| the socket itself | **you** | [`docker-compose.sibling.yml`](../examples/remote-agent/docker-compose.sibling.yml) |

Withhold any one and nothing runs. Prefer the rootless Docker or Podman user socket; the
example overlay uses the rootless path deliberately, so reaching for the root socket has to
be a conscious edit.

What the agent does with it is deliberately narrow. Every field of the container spec is a
constant or comes from your policy — the image, the network, and a `HostConfig` with no
bind mounts, no capabilities, a read-only root filesystem and `no-new-privileges`. **None
of it is derived from anything the dashboard sends**, because there is no field through
which to ask; a test asserts that. The credential rides in the environment of the create
call rather than argv, so it never appears in `ps` on the host. Containers are labelled and
orphans from a crashed agent are swept at startup.

The agent will not pull the image for you. A pull is a network fetch of executable content,
and that is your decision rather than a job's:

```
docker pull chrweav/hypervisor-runner:latest
```

### VMware Workstation Pro

Workstation is the one hypervisor here that runs on somebody's desktop, and it was twice
written off as unreachable because the dashboard drives it with `vmrun` against local VMX
paths. That was true of `vmrun` and wrong overall: Workstation **Pro** ships `vmrest`, a
REST daemon that is plain JSON over HTTP. An agent on that host reaches it with no extra
dependency and no container.

On the Workstation host:

```
vmrest -C     # set the API credentials, once
vmrest        # run the daemon — 127.0.0.1:8697
```

Then add a `workstation` connection bound to that host's agent. It is **agent-bound
only**: the dashboard has no transport for Workstation, so a connection without an agent
is refused rather than created and left broken.

**It needs `allow_loopback`.** vmrest binds `127.0.0.1`, and the agent denies loopback
unconditionally — that deny is what stops a discovery sweep probing the agent's own
container or a cloud metadata endpoint, and it is re-added even if an operator deletes
it. So a co-located connection opts in explicitly, in `policy.yaml`, next to its verbs:

```yaml
connections:
  - name: my-workstation
    verbs: [inventory_sync, power_on, power_off]
    allow_loopback: true
```

That exempts **that connection**, on the port its `connections.yaml` entry names, and
nothing else. Discovery still refuses loopback however this is set. If you can make
vmrest listen on a routable address instead, do that and skip the exception entirely.

**Inventory plus power on and off — and nothing more.** vmrest's API is
`on/off/shutdown/suspend/pause/unpause` with **no reset, no reboot and no snapshot**, so
`restart`, `power_reset` and `snapshot` are refused with a message saying so. Mapping
`restart` onto `shutdown` would quietly do something other than what was asked.

Synced VMs appear on the **Workstation** page alongside the ones this host scans locally,
badged with the agent's name. They are tagged into workgroups the same way Proxmox and
Nutanix VMs are, and an untagged VM is admin-only — which is what stops an agent widening
what a non-admin can see.

### Large inventories

A sync returns one **page** plus an opaque cursor; the dashboard applies it and enqueues
the next. The chain is capped at 40 pages (10 000 VMs), which is what stops a
misbehaving agent making the dashboard enqueue work forever. Every page of one sync
shares a `batch_id`, so N job rows roll up as one run on `/jobs`.

The cap that forces this is `MAX_RESULT_BYTES` (256 KB), and raising it is not an
option — it is the only bound on an agent's write path into the database.

## Best practices

- **Run in audit mode first.** `AGENT_MODE=audit` logs every job it *would* run, in
  full, and executes nothing. Two weeks of that, diffed against the policy, is usually
  what gets an agent approved by a security team.
- **Ship the agent's stdout to your SIEM.** The dashboard is not the authoritative
  audit record for an agent — if it were, a compromised dashboard could delete the
  evidence of its own compromise. The container log is the copy it cannot reach.
- **Prefer rootless Podman** on hosts where more than one person is in the `docker`
  group — see [Running under Podman](#running-under-podman) for what it does and does not
  buy you.
- **Name ports in the policy**, not just networks.
- **One agent per site**, named for the site.
- **Watch the policy hash** on the Agents page. It only changes when the file does.
- **Re-enrol rather than reuse.** Issuing a new code clears the stored public key, so
  the container being replaced stops being able to lease work immediately.

## Rate limits

The agent vhost is the only surface this dashboard deliberately publishes to a network it
does not own, so it is the only one with a throttle of its own
(`services/agent_guard.py`). Nothing else in the app is rate limited — the SlowAPI limiter
in `main.py` is inert on purpose, because a blanket per-address cap would break a UI that
fires many calls per page load.

Three caps, keyed differently because only one of these routes has a principal to key on:

| What | Key | Default | Config key |
|---|---|---|---|
| Signed requests | the **agent id**, proven by its signature | 240 / minute | `agent_max_requests_per_minute` |
| Failed enrolments | client address | 10 / 15 min | `agent_enroll_max_per_ip` |
| Failed enrolments, all sources | — | 200 / 15 min | `agent_enroll_max_global` |

Set `agent_throttle_enabled` false to disable, or any cap to `0`. The per-agent cap keys on
the agent id rather than the address for the same reason the login throttle keys on the
username: an address is only as trustworthy as `TRUSTED_PROXY_HOSTS`, while an agent id is
proven by a signature the caller cannot forge.

240/minute is about three times what a *busy* agent does. While a scan runs it reports
every two seconds — roughly 60 requests a minute — and idle polling at the default
interval is 12. If you lower this, lower it toward 60, not toward the poll interval.

Over a cap the answer is **429 with `Retry-After`**, never 401, and the agent waits the
interval it was given with jitter so a fleet throttled together does not come back in
lockstep. Only *failed* enrolments count, so enrolling a fleet back to back trips nothing.

Neither cap is brute-force protection — an enrolment code is 256 bits and every other
route needs a valid signature. They bound denial of service and table growth, which is
what an unauthenticated internet-facing endpoint actually needs.

## Behind a TLS-inspecting proxy

Mount the inspection CA and point `AGENT_CA_BUNDLE` at it —
[`docker-compose.corp-ca.yml`](../docker-compose.corp-ca.yml) is the same pattern for
the dashboard. `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are honoured automatically.

They apply to the **dashboard connection only**. The discovery probes are raw sockets
and ignore proxy variables entirely, so `NO_PROXY` is not needed to keep a scan off the
corporate proxy — a scan was never going through it. What `NO_PROXY` is for here is the
narrower case of the dashboard itself being on a network the proxy should not be used
for.

A bad `AGENT_CA_BUNDLE` path is worth knowing about because it fails badly: `requests`
raises a bare `OSError("Could not find a suitable TLS CA certificate bundle, invalid
path: …")`, which is not a `RequestException` and so is not caught — the agent dies with
a traceback and exit 1 rather than a readable message.

Worth saying plainly: an inspecting proxy sees the full content of every request. That
is exactly why the agent's credential is a per-request signature rather than a bearer
token — the proxy captures nothing it could replay. This turns the corporate proxy from
an objection into a demonstration.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Cannot read the policy file … No such file or directory` and the container exits 2 | No policy mounted. Fail-closed is deliberate. |
| `Cannot read the policy file … Permission denied` on a file `ls -l` already shows world-readable | **SELinux**, on Fedora/RHEL/CentOS/Rocky/Alma. A file in your home directory is labelled `user_home_t`, which the container may not read whatever its mode bits say — 644 is not enough here. Mount it `:ro,Z` (the emitted command does; Docker and Podman ignore `Z` on hosts without SELinux) or relabel by hand: `chcon -t container_file_t policy.yaml`. Verify with `ls -lZ policy.yaml`, and `ausearch -m avc -ts recent` for the denial itself. Worth recognising because the dashboard-side symptoms are all misleading: the agent exits *before* its first network call, so **no request whatsoever reaches the dashboard** — no 4xx, nothing in the app or ingress logs — and the row sits at `enrolling` with `Last seen: never`, no source IP and no policy hash, which reads exactly like a wrong URL or a blocked egress. The enrolment code is **not** spent either, so the same one still works once the mount is fixed. |
| `DASHBOARD_URL is http://` and it exits 2 | The agent will not sign over plaintext. Terminate TLS, or `AGENT_INSECURE_TLS=1` for a throwaway lab. |
| `the dashboard rejected this agent's signature` | Revoked, re-enrolled elsewhere, or the dashboard URL changed (the audience is pinned). Issue a fresh code. |
| Enrolment returns 400 | The code is single-use and expires in 15 minutes. Issue another. |
| Enrolment returns 404 | `remote_agents_enabled` is off. Settings → Integrations → Remote Agents. |
| No **Agents** link in the nav | Same flag. It gates the nav entry as well as the router, and the link is admin-only on top of that. |
| `/agents` says "Remote agents are not enabled" | Same again — the page itself is not gated, so it loads and then tells you which switch to flip. |
| Enrolment returns 429, and the container exits 2 | Too many *failed* enrolments recently from this address. Nothing is wrong with this agent; the restart policy retries after `Retry-After`. |
| `the dashboard asked us to slow down` | Over the per-agent cap. Expected during a burst, and it recovers on its own. If an ordinary scan trips it, raise `agent_max_requests_per_minute` — see [Rate limits](#rate-limits). |
| `AGENT_ENROLLMENT_CODE_FILE … cannot be read` | The container runs as uid 10001; a mode 0600 file owned by your host user is unreadable inside it. Make it world-readable or use the environment variable. If it is already world-readable, this is the SELinux label, same as the policy row above — mount it `:ro,Z`. |
| Agent polls, gets 503 | Dashboard setup is not complete. On `lease` the agent backs off and keeps trying; it is deliberately not redirected to the HTML wizard. **On enrolment a 503 is fatal** — the agent exits 2 and the container's restart policy is the retry, so finish setup before issuing a code. |
| `Policy refused N of M host:port combinations` | Working as intended — the request was wider than the policy. The counts tell you which to widen. |
| Stuck `queued`, agent online | The job type is not in the agent's `job_types`. Check the policy. |
| Nothing found on a subnet you expect | Confirm the ports are in `targets`, and remember `already_registered` findings still appear. |
| Clock skew errors | Signatures are valid ±60s. Run NTP on the agent host. |
| Every agent 401s right after adding a proxy, and the dashboard logs `Ignoring X-Forwarded-*` | The proxy is not in `TRUSTED_PROXY_HOSTS`, so the audience was pinned as `http://…`. Set the variable **and** `PUBLIC_BASE_URL`, then **Reset signing audience** in Settings → Integrations → Remote Agents and re-enrol every agent. |
| Agent stuck at `enrolling`, *Last seen: never*, no policy hash — and **no `/api/agent/enroll` line in the dashboard log at all**, while the container log shows it *reaching* a URL and getting 404 | The signing audience is pinned to a hostname that does not serve `/api/agent` — on a split-vhost install, almost always the UI hostname, because that is the origin the admin's browser was on when the first code was minted. Every dashboard-side signal here is identical to the SELinux policy-mount row above, so **read the container log to tell them apart**: SELinux exits 2 on `Cannot read the policy file` before any network call, whereas this one dials out and is refused. Settings → Integrations → Remote Agents now shows the pin; fix `PUBLIC_BASE_URL`, reset the audience, and re-enrol. See [The signing audience](#the-signing-audience-is-pinned-by-that-first-code). |
| **Register Agent** returns 409 naming two URLs | Deliberate: the pinned audience contradicts Public base URL, so the command would have carried the stale one. Nothing was created. Set Public base URL back to the pinned value, reset the audience, or confirm the prompt to issue against the pin anyway. |
| `The state directory … is not writable` | No volume mounted, or one not writable by uid 10001. Caught before enrolling, so **the code is still good** — fix the mount and start again. |
| `AGENT_INSECURE_TLS` appears to be ignored | Case-sensitive: only `1`, `true` or `yes`. `True` and `on` are read as unset. |
| `dashboard unreachable (404 …)` on an agent that was working | Not a network fault — `remote_agents_enabled` was turned off. The agent reports every non-2xx this way. |
| `enrolment refused (400) … Invalid enrolment code` repeating | The code is single-use with a 15-minute TTL, and a crash loop re-spends it on every restart. `docker stop` first, fix the cause, then issue **one** code and start **once**. |
| `the dashboard is throttling enrolment` | Ten failed enrolments from one address in 15 minutes. Stop the container, wait out the `Retry-After`, fix the real cause before retrying — restart loops are what get you here. |
| Discovery reports `Policy refused N of M` | Expected, not a fault. The scan asks for a port the policy does not list, so a /24 yields one refusal per host. Widen `ports:` only if you mean to. |
| `Agent … reports version 1.x` on a 409, and nothing is queued | A 1.x agent scans for Kubernetes and databases. Given a hypervisor scan it would probe nothing, complete **green**, and report zero findings — indistinguishable from a clean network. Pull `chrweav/dashboard-agent:latest` and restart the container; re-enrolment is not needed. |
| `Agent … does not offer discovery` | The agent is current but its `policy.yaml` omits `agent_discover` from `job_types`. |
| `unknown connection 'x'` | The name in the dashboard's connection row does not match any `name:` in that agent's `connections.yaml`. The dashboard holds no credential for it; the string is the whole join. |
| `policy.yaml does not grant 'power_off' on 'x'` | Working as intended — the customer's file is the authority. Add the verb under that connection's `verbs:` list and restart the agent. |
| One sync produced a dozen job rows | Expected for a large inventory: one row per page, all sharing a `batch_id`. See [Large inventories](#large-inventories). |
| `policy.yaml does not enable the sibling runner` | Hyper-V and bare ESXi need it. Add the `sibling:` block, and apply `docker-compose.sibling.yml` so the socket is present. Read that file first — it mounts the Docker socket. |
| `the sibling image … is not present on this host` | `docker pull chrweav/hypervisor-runner:latest`. The agent will not pull it for you, deliberately. |
| `cannot reach the Docker socket` | The overlay is not applied, or `AGENT_DOCKER_SOCKET` does not match the mount's container-side path. Settle which with `docker compose -f docker-compose.yml -f docker-compose.sibling.yml config`: the output must show **one** service, `agent`, carrying both the bind mount and the variable. Two services means an overlay whose service key does not match the base file's — Compose merges by service key, not by `container_name`. |
| A Password Safe checkout fails `4031 … 403` | The OAuth client's user needs the **Requestor** role plus a View access policy on a Smart Rule containing that managed account. Membership is recomputed on a schedule, so a new account is not requestable immediately. |
| `could not reach vmrest at 127.0.0.1:8697` | Either `vmrest` is not running, or the connection lacks `allow_loopback: true` in policy.yaml. The agent denies loopback by default. |
| `vmrest rejected the credential` | Set them with `vmrest -C`, and check the username matches connections.yaml. |
| `vmrest has no 'restart' operation` | Working as intended — its API has no reset, reboot or snapshot. Use power_off then power_on. |
| A sync never runs, and the connection shows an error | Read it — the enqueuer records why rather than queueing a job that would wait indefinitely. Usually the bound agent is offline or lacks the `agent_hypervisor` grant. |
| Caddy never serves; logs show ACME retries | The hostname is internal and cannot satisfy an ACME challenge. Set `AGENT_TLS_INTERNAL=1` — see [above](#if-the-hostname-is-internal). |

## Where this is heading

Discovery was the first slice, chosen because no credential crosses the wire at all.
Hypervisor brokering followed it and is described above. Next:

- **Agent-executed Ansible** against private targets, spawning one-shot
  `chrweav/ansible-cloud` siblings and reusing the existing
  `PLAYBOOK_B64` / `CONN_VARS_B64` env contract byte for byte, plus a just-in-time
  secret fetch gated on refs the job declared at enqueue time.
- **Password Safe JIT checkout by the agent**, so it holds exactly one credential whose
  only power is to ask Password Safe — subject to its policy, approval workflow and
  session recording. An on-prem agent with zero standing target credentials.
- **Nutanix power verbs and snapshots.** Both are full spec PUTs carrying a metadata
  version rather than simple actions, so getting one wrong writes to the VM instead of
  failing. Worth doing carefully rather than quickly.
- **Retiring `POWERSHELL_EXECUTION_MODE=ssh`,** now that a co-located agent does the
  same job by polling outward instead of the dashboard holding an inbound SSH key to a
  Windows desktop.
- ~~VMware Workstation over a co-located agent~~ — shipped; see
  [VMware Workstation Pro](#vmware-workstation-pro).
- **The rest of what was deferred.** Not network-reachable, so this is a
  new *deployment shape* rather than one more connection kind — the agent would have to
  run on the desktop host and speak to `vmrest` on localhost. It would replace the
  existing `POWERSHELL_EXECUTION_MODE=ssh` dev escape hatch, which does the same job
  today with an inbound SSH key.

`config.py`'s singleton hypervisor keys (one `proxmox_host`, one `vsphere_host`) were
the blocker for all of this. They are now a one-time **seed** for the
`hypervisor_connections` table rather than the source of truth; the old Settings panels
are read-only and say so.
