# Enrolling an agent

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are registering an agent from the dashboard and adding its connections.

Part of [Remote Agents](../remote-agents.md). Publishing the endpoint, the enrolment code, the signing audience, and adding connections.

## 1. Publish the agent endpoint

```bash
# Bash (WSL / Linux / macOS)
AGENT_HOSTNAME=agents.example.com docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
```

```powershell
# PowerShell (Windows) — the inline VAR=value prefix is a parse error here
$env:AGENT_HOSTNAME = 'agents.example.com'
docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
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
# Bash (WSL / Linux / macOS)
AGENT_HOSTNAME=agents.lab.internal AGENT_TLS_INTERNAL=1 \
  docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
```

```powershell
# PowerShell (Windows)
$env:AGENT_HOSTNAME = 'agents.lab.internal'
$env:AGENT_TLS_INTERNAL = '1'
docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml up -d
```

Then give the agents that CA — it is in no system trust store:

```bash
# Bash (WSL / Linux / macOS)
docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml \
  cp agent-gateway:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

```powershell
# PowerShell (Windows)
docker compose -f docker-compose.hub.yml -f docker-compose.agent.yml `
  cp agent-gateway:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Copy it to the agent host, mount it, and set `AGENT_CA_BUNDLE` to the mounted path.

Prefer this over `AGENT_INSECURE_TLS=1`. The insecure switch also disables verification,
which is the part most worth exercising, and it masks a wrong `PUBLIC_BASE_URL` or
`TRUSTED_PROXY_HOSTS` until an agent starts 401ing for reasons that look like
revocation. The root is generated on first start and lives in the `caddy_data` volume —
it survives restarts but not `docker compose down -v`, and wiping it means re-issuing
the root to every agent.


## 3. Register and enrol

**Agents → Register Agent.** You get a single-use code, valid 15 minutes, and a
copy-paste `docker run` built from the dashboard's own URL. On first start the agent
generates an Ed25519 keypair, redeems the code, pins the dashboard's public key, and
writes its identity to the state volume. The code is never needed again.

The command comes in two shell flavours, picked with the **Linux / macOS** and **Windows
(PowerShell)** toggle above it; the container is identical either way, and on a Windows
browser the PowerShell form is pre-selected. See
[Running on Windows](agent-host.md#running-on-windows) for what differs on that host.

The private key never leaves the agent host. The dashboard only ever stores the public
half, so a dashboard database dump does not let anyone impersonate an agent.

The default command passes the code as `-e AGENT_ENROLLMENT_CODE`, which is convenient and
has one drawback worth knowing: an environment variable is baked into the container's
config, so `docker inspect` still shows the code long after it has been spent. The modal
offers a second form that mounts it as a file and sets `AGENT_ENROLLMENT_CODE_FILE`
instead, leaving nothing durable in Docker's metadata; delete the file once the agent shows
Online. To keep the code out of your shell history as well, create that file with an editor
and paste only the `docker run` that follows it — on Windows that is the *only* way, because
PowerShell writes its history to disk as you type.

If you do write it by hand, save it as plain ASCII or UTF-8. The emitted PowerShell command
uses `Set-Content -Encoding ascii -NoNewline` for that reason, and the agent is
belt-and-braces about it from its side: a **UTF-8 BOM is stripped** rather than sent as part
of the code, and a **UTF-16 file** — what PowerShell 5.1's `>` and `Out-File` produce, and
what Notepad calls "Unicode" — is refused with a message naming the encoding and the
`Set-Content` flags that fix it, rather than the raw decode traceback it used to be. Either
way the code is not spent, so the same one still works once the file is rewritten.

Because the container runs as uid 10001, that file has to be readable by it — a mode 0600
file owned by your host user is *not*, and the agent says so and exits rather than failing
mysteriously. A single-use secret with a fifteen-minute lifetime in a directory you chose
is an acceptable trade for that; a long-lived one would not be. (On Windows this cannot
arise: Docker Desktop presents bind mounts with permissive ownership.)

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

#### The BeyondTrust Gateway

`agent_gateway` lets an agent run a **Gateway node on its own host** — long-lived, and
`--privileged`. Read that second word before enabling it: a Gateway carries protocol
tunnels, which needs `NET_ADMIN`, `NET_RAW`, `IPC_LOCK` and `/dev/net/tun`, and Docker has
no granular way to grant that set. The agent refuses to start one without the flag rather
than start one that cannot work — without those capabilities a Gateway registers **online**
and every tunnel silently times out, which reads as a firewall problem for days.

Four things must agree, the same shape Config Management uses:

1. the dashboard grants this agent the `agent_gateway` job type;
2. `agent_gateway` is in `job_types:` in your policy.yaml;
3. a `gateway:` block naming the image and setting `privileged: true`;
4. the deploy key, which the dashboard holds and the agent fetches per job, sealed.

**Requires an agent image of 2.4.0 or newer.** Below that, `agent_gateway` is not in the
agent's closed `HANDLERS` dict at all, so it refuses the job *by name* — and "unknown job
type" in Live Output reads as a dashboard bug rather than as "this container is a build
behind". The dashboard refuses at enqueue instead, and the refusal names both halves,
because on a POV both are usually true at once: the image has to be newer **and** the
policy has to grant `agent_gateway`. On a POV that file is generated rather than edited, so
the remedy for the second half is the **Broker** button — which is the difference between a
one-click fix and an SSH session into a customer's environment. Pull
`chrweav/dashboard-agent:latest` and restart; the agent keeps its identity, so no
re-enrolment.

The image comes from your file and never from a job — a job says only *install* or
*remove*. Pull it yourself; the agent will not. Pin a tag rather than tracking `latest`
under a registered Gateway.

**A green job does not mean the node registered.** All the agent proves is that the
container did not exit within the first minute — which is the failure a wrong image tag or a
kernel without `/dev/net/tun` produces. Whether PRA *accepted* the node is a question only
the dashboard can ask, against the tenant's own API, so it asks it separately; an agent that
guessed would report green for a Gateway with a bad deploy key.

**Re-running replaces the container rather than reusing it.** A changed deploy key is the
usual reason to run this again, and a running container holds the key it started with. The
container name is generated (`pov-gateway`), never taken from the job — there is no field in
the protocol through which a dashboard string could name something on your host.

**Removing does not need the grant that installing does.** A `remove` skips the policy check
and the key fetch entirely, so a POV whose policy was later narrowed does not become
un-teardownable. In `audit` mode nothing is fetched and nothing is started — the deploy key
is never released.

It needs the Docker socket, which is root on the host — the same requirement and the same
overlay as the other runners.

The dashboard-side driver for this today is the POV feature, which generates the whole
policy for a broker VM it created; see
[pov-instance.md](../profiles/pov/gateway-and-broker.md#the-pov-gateway). The same feature uses `agent_ansible`
to install a [Password Safe Resource Broker](../profiles/pov/gateway-and-broker.md#the-resource-broker) on a
Windows VM beside it — a `.exe` asset, which the Config-Management runner now installs with
`win_package`.


## 4. Keep an eye on who is enrolled

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


## 5. Discover

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
[Import from Password Safe](../databases.md#importing-from-password-safe) replaced that
path. Kubernetes clusters are registered from a kubeconfig, which no credential-less
probe could ever supply.


## 6. Add the connections

Findings are **never auto-registered**, and that is not a limitation — it is infeasible
by construction. A connection needs a credential, and a probe by definition has none.

So you review the findings and click **Add connection…**, which prefills the
[Connections](hypervisors.md#hypervisor-connections) form with the host, port and a suggested name and
runs under your own credentials and permission checks. Findings the dashboard already
has a connection for are marked `already_registered` — computed server-side, because an
agent should never be handed the inventory of everything else the dashboard manages.

One limitation worth knowing: a connection configured with an FQDN but discovered by IP
reads as new, because the dashboard cannot resolve your private DNS — it is not on that
network, which is the whole reason the agent exists. The prefill uses the IP the probe
connected to, so a second scan matches.
