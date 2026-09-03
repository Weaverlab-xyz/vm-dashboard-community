# Remote Agents

> **Audience:** operator · **Profile:** `both` · **Read this when:** your hypervisors, databases or clusters live somewhere the dashboard cannot reach.

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

The doctrine's actual invariants are preserved. An agent may hold a target's
credential only where the host owner sealed it there — see
[Credentials an agent uses](remote-agents/credentials.md) — and a Config Management run
spawns a **one-shot sibling container** per job, so the persistent process never executes
operator-supplied code. The runner stays one-shot; only the thing that launches it moved.


## The pages

| Page | What's in it |
|---|---|
| [Enrolling an agent](remote-agents/enrolment.md) | Publishing the endpoint, the enrolment code, the signing audience, and adding connections. |
| [Running the agent on your host](remote-agents/agent-host.md) | The private key, the policy file, Windows and Podman, and a TLS-inspecting proxy. |
| [Hypervisor connections through an agent](remote-agents/hypervisors.md) | The verbs, what the agent can reach, the sibling runner and VMware Workstation Pro. |
| [Credentials an agent uses](remote-agents/credentials.md) | What the dashboard holds, what the host seals, and where a credential comes from at run time. |
| [Agent-executed Config Management](remote-agents/config-runs.md) | What the dashboard sends, the grants it needs, and what a run looks like. |
| [Agent-brokered file shares](remote-agents/file-shares.md) | A name rather than a path, the grants, and why it needs no Docker socket. |

The first two split by **who is reading**: the operator enrols and manages agents from
the dashboard, and the host owner runs the container and writes its policy. They are
usually different people, often at different companies.

## Prerequisites

| Requirement | Details |
|---|---|
| TLS in front of the dashboard | Not optional. Signing gives integrity, not confidentiality. Use `docker-compose.agent.yml`. |
| A hostname agents can resolve | Pinned as the signing *audience* on first use. Changing it later means re-enrolling. |
| `remote_agents_enabled` | **Settings → Integrations → Remote Agents**, or the setup wizard. Off by default — the nav link and the whole `/api/agent` router are hidden until you turn it on. |
| Docker on the agent host | Or any OCI runtime — Docker Engine, Docker Desktop or Podman. The image is Linux (`linux/amd64`, `linux/arm64`); on a Windows host it runs in Docker Desktop's Linux VM, which is supported and ordinary — see [Running on Windows](remote-agents/agent-host.md#running-on-windows). ~80 MB image, no privileges. |
| Outbound 443 from the agent | One FQDN. Nothing else. |


## Best practices

- **Run in audit mode first.** `AGENT_MODE=audit` logs every job it *would* run, in
  full, and executes nothing. Two weeks of that, diffed against the policy, is usually
  what gets an agent approved by a security team.
- **Ship the agent's stdout to your SIEM.** The dashboard is not the authoritative
  audit record for an agent — if it were, a compromised dashboard could delete the
  evidence of its own compromise. The container log is the copy it cannot reach.
- **Prefer rootless Podman** on hosts where more than one person is in the `docker`
  group — see [Running under Podman](remote-agents/agent-host.md#running-under-podman) for what it does and does not
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


## Troubleshooting

| Symptom | Cause |
|---|---|
| `Cannot read the policy file … No such file or directory` and the container exits 2 | No policy mounted. Fail-closed is deliberate. |
| `policy.yaml: targets must be a list of - cidr: / - fqdn: entries`, and the container exits 2 | A **missing space after the dash**: `-cidr: 10.20.0.0/24` is valid YAML for a key *named* `-cidr` rather than a list entry, so the list parses as a mapping and the grant is empty. Write `- cidr: 10.20.0.0/24`. The message echoes the offending key, which is the fastest way to spot it. Applies to `ansible.targets:` the same way. An agent image predating this fix skipped what it could not read instead of refusing to start, so the only symptom there is every job being refused against a range you can see written down — the fix is the same. |
| `policy.yaml: … is written twice in the same block` | A duplicated key. YAML keeps only the **last** one, so everything under the earlier copy is discarded silently — and in this file a key is a grant. Merge them into one. If the duplicated key begins with a dash (`-cidr`), it is the row above: the missing space, twice. |
| `Cannot read the policy file … Permission denied` on a file `ls -l` already shows world-readable | **SELinux**, on Fedora/RHEL/CentOS/Rocky/Alma — a **Linux-host-only** cause; on Windows see the row below instead. A file in your home directory is labelled `user_home_t`, which the container may not read whatever its mode bits say — 644 is not enough here. Mount it `:ro,Z` (the emitted command does; Docker and Podman ignore `Z` on hosts without SELinux) or relabel by hand: `chcon -t container_file_t policy.yaml`. Verify with `ls -lZ policy.yaml`, and `ausearch -m avc -ts recent` for the denial itself. Worth recognising because the dashboard-side symptoms are all misleading: the agent exits *before* its first network call, so **no request whatsoever reaches the dashboard** — no 4xx, nothing in the app or ingress logs — and the row sits at `enrolling` with `Last seen: never`, no source IP and no policy hash, which reads exactly like a wrong URL or a blocked egress. The enrolment code is **not** spent either, so the same one still works once the mount is fixed. |
| `no matching manifest for windows/amd64`, or `image operating system "linux" cannot be used on this platform` | Docker Desktop is in **Windows containers** mode. The agent image is Linux, as is almost everything else you would run; right-click the tray whale → *Switch to Linux containers*. Neither message names the mode, which is why this is worth recognising rather than debugging. |
| **On Windows**, `Cannot read the policy file`, on a path that plainly exists | Docker Desktop file sharing, **not** SELinux — ignore the `chcon` advice above, it cannot apply. Either the working directory is a UNC path or a mapped network drive (neither can be bind-mounted), or the drive is not shared: *Settings → Resources → File sharing*. Note that on the Hyper-V backend a bind source Docker Desktop cannot reach is materialised as an empty **directory** inside the container rather than failing outright, so the message you get talks about mount flags and points nowhere near the real cause. Keep `policy.yaml` under your user profile and it does not arise. The agent detects this host from its own kernel (`-microsoft-standard-WSL2` or `-linuxkit` in `/proc/version`) and says all of the above itself rather than repeating the SELinux advice, so the container log should already be pointing you here. |
| **On Windows**, `AGENT_ENROLLMENT_CODE_FILE … contents are not text this agent can decode` | The file is UTF-16 — PowerShell 5.1's `>` and `Out-File` write that by default, and Notepad's "Unicode" option does too. Write it with `Set-Content -Encoding ascii -NoNewline`, as the emitted PowerShell command does, or save it from an editor as ASCII/UTF-8. Nothing reached the dashboard, so the code is unspent and the same one works once the file is rewritten. A UTF-8 BOM used to be the quieter version of this — it survives the whitespace trim, so the code was simply rejected as invalid — and is now stripped on read instead. Before either was handled the symptom was a raw Python traceback, so an older agent image on this fault looks like a crash rather than a message. |
| **On Windows**, every agent goes offline when you log off | Docker Desktop runs inside a logged-in user session, so `--restart unless-stopped` cannot help — there is no engine left to restart anything. Enable *Start Docker Desktop when you log in* and stay logged in, or move the agent to Windows Server with the WSL2 engine or a Linux VM. See [Running on Windows](remote-agents/agent-host.md#running-on-windows). |
| `DASHBOARD_URL is http://` and it exits 2 | The agent will not sign over plaintext. Terminate TLS, or `AGENT_INSECURE_TLS=1` for a throwaway lab. |
| `the dashboard rejected this agent's signature` | Revoked, re-enrolled elsewhere, or the dashboard URL changed (the audience is pinned). Issue a fresh code. |
| Enrolment returns 400 | The code is single-use and expires in 15 minutes. Issue another. |
| Enrolment returns 404 | `remote_agents_enabled` is off. Settings → Integrations → Remote Agents. |
| No **Agents** link in the nav | Same flag. It gates the nav entry as well as the router, and the link is admin-only on top of that. |
| `/agents` says "Remote agents are not enabled" | Same again — the page itself is not gated, so it loads and then tells you which switch to flip. |
| Enrolment returns 429, and the container exits 2 | Too many *failed* enrolments recently from this address. Nothing is wrong with this agent; the restart policy retries after `Retry-After`. |
| `the dashboard asked us to slow down` | Over the per-agent cap. Expected during a burst, and it recovers on its own. If an ordinary scan trips it, raise `agent_max_requests_per_minute` — see [Rate limits](#rate-limits). |
| `AGENT_ENROLLMENT_CODE_FILE … cannot be read` | **On a Linux host:** the container runs as uid 10001, and a mode 0600 file owned by your host user is unreadable inside it. Make it world-readable or use the environment variable. If it is already world-readable, this is the SELinux label, same as the policy row above — mount it `:ro,Z`. **On Windows** neither applies: Docker Desktop presents bind mounts with permissive ownership, so this is file sharing or encoding — see the two Windows rows above. |
| Agent polls, gets 503 | Dashboard setup is not complete. On `lease` the agent backs off and keeps trying; it is deliberately not redirected to the HTML wizard. **On enrolment a 503 is fatal** — the agent exits 2 and the container's restart policy is the retry, so finish setup before issuing a code. |
| `Policy refused N of M host:port combinations` | Working as intended — the request was wider than the policy. The counts tell you which to widen. |
| Stuck `queued`, agent online | The job type is not in the agent's `job_types`. Check the policy. |
| Nothing found on a subnet you expect | Confirm the ports are in `targets`, and remember `already_registered` findings still appear. |
| Clock skew errors | Signatures are valid ±60s. Run NTP on the agent host. |
| Every agent 401s right after adding a proxy, and the dashboard logs `Ignoring X-Forwarded-*` | The proxy is not in `TRUSTED_PROXY_HOSTS`, so the audience was pinned as `http://…`. Set the variable **and** `PUBLIC_BASE_URL`, then **Reset signing audience** in Settings → Integrations → Remote Agents and re-enrol every agent. |
| Agent stuck at `enrolling`, *Last seen: never*, no policy hash — and **no `/api/agent/enroll` line in the dashboard log at all**, while the container log shows it *reaching* a URL and getting 404 | The signing audience is pinned to a hostname that does not serve `/api/agent` — on a split-vhost install, almost always the UI hostname, because that is the origin the admin's browser was on when the first code was minted. Every dashboard-side signal here is identical to the SELinux policy-mount row above, so **read the container log to tell them apart**: SELinux exits 2 on `Cannot read the policy file` before any network call, whereas this one dials out and is refused. Settings → Integrations → Remote Agents now shows the pin; fix `PUBLIC_BASE_URL`, reset the audience, and re-enrol. See [The signing audience](remote-agents/enrolment.md#the-signing-audience-is-pinned-by-that-first-code). |
| **Register Agent** returns 409 naming two URLs | Deliberate: the pinned audience contradicts Public base URL, so the command would have carried the stale one. Nothing was created. Set Public base URL back to the pinned value, reset the audience, or confirm the prompt to issue against the pin anyway. |
| `The state directory … is not writable` | No volume mounted, or one not writable by uid 10001. Caught before enrolling, so **the code is still good** — fix the mount and start again. |
| `seal` says the state directory `is inside this container rather than a mounted volume` | You ran it without `-v dashboard_agent_state:/var/lib/dashboard-agent`. It refuses rather than sealing, because the key it would create dies with the container and the value could never be opened again. Nothing was written. |
| `this … was sealed with key aabbccdd, but the key in … is 11223344` | The value was sealed against a *different* key — nearly always a `seal` run without the volume, on an image that predates the refusal above, or a state volume that has since been recreated. Seal it again with the volume mounted and paste the new line in. Not a permissions or mount-flag problem. |
| `this sealed … did not authenticate. It is bound to '…'` | Either the entry's `host:` (or `api_url:`) changed since the value was sealed — seal it again against the new address — or the sealed value was moved here from a different entry, which is what the binding exists to stop. See [Sealing a credential this host keeps](remote-agents/credentials.md#sealing-a-credential-this-host-keeps). |
| `connection '…' declares no credential` | None of the five sources is set on that entry. Previously this sent an *empty* password and read back as a wrong one; it is now refused up front. If the entry declares only `password_sealed`, the agent image predates 2.2.0 — pull a newer one. |
| `… is a sealed value, and this is not the field for one` | A `sealed.v1.…` line pasted into `password:`, `client_secret:`, or a `password_file`. Move it to `password_sealed:` / `client_secret_sealed:`. |
| `AGENT_INSECURE_TLS` appears to be ignored | Case-sensitive: only `1`, `true` or `yes`. `True` and `on` are read as unset. |
| `dashboard unreachable (404 …)` on an agent that was working | Not a network fault — `remote_agents_enabled` was turned off. The agent reports every non-2xx this way. |
| `enrolment refused (400) … Invalid enrolment code` repeating | The code is single-use with a 15-minute TTL, and a crash loop re-spends it on every restart. `docker stop` first, fix the cause, then issue **one** code and start **once**. |
| `the dashboard is throttling enrolment` | Ten failed enrolments from one address in 15 minutes. Stop the container, wait out the `Retry-After`, fix the real cause before retrying — restart loops are what get you here. |
| Discovery reports `Policy refused N of M` | Expected, not a fault. The scan asks for a port the policy does not list, so a /24 yields one refusal per host. Widen `ports:` only if you mean to. |
| `Agent … reports version 1.x` on a 409, and nothing is queued | A 1.x agent scans for Kubernetes and databases. Given a hypervisor scan it would probe nothing, complete **green**, and report zero findings — indistinguishable from a clean network. Pull `chrweav/dashboard-agent:latest` and restart the container; re-enrolment is not needed. |
| `Agent … does not offer discovery` | The agent is current but its `policy.yaml` omits `agent_discover` from `job_types`. |
| `unknown connection 'x'` | The name in the dashboard's connection row does not match any `name:` in that agent's `connections.yaml`. The dashboard holds no credential for it; the string is the whole join. |
| `'shutdown' is not available on an agent-bound vsphere connection` (501) | Working as intended, and not a policy or grant problem — no verb in the allowlist performs a graceful guest shutdown, and the nearest one hard-resets the guest on vSphere. See [The verbs](remote-agents/hypervisors.md#the-verbs) for which button works on which product. |
| `policy.yaml does not grant 'power_off' on 'x'` | Working as intended — the customer's file is the authority. Add the verb under that connection's `verbs:` list and restart the agent. |
| One sync produced a dozen job rows | Expected for a large inventory: one row per page, all sharing a `batch_id`. See [Large inventories](remote-agents/hypervisors.md#large-inventories). |
| `policy.yaml does not enable the sibling runner` | Hyper-V and bare ESXi need it. Add the `sibling:` block, and apply `docker-compose.sibling.yml` so the socket is present. Read that file first — it mounts the Docker socket. |
| `the sibling image … is not present on this host` | `docker pull chrweav/hypervisor-runner:latest`. The agent will not pull it for you, deliberately. |
| `cannot reach the Docker socket` | The overlay is not applied, or `AGENT_DOCKER_SOCKET` does not match the mount's container-side path. Settle which with `docker compose -f docker-compose.yml -f docker-compose.sibling.yml config`: the output must show **one** service, `agent`, carrying both the bind mount and the variable. Two services means an overlay whose service key does not match the base file's — Compose merges by service key, not by `container_name`. |
| A Password Safe checkout fails `4031 … 403` | The OAuth client's user needs the **Requestor** role plus a View access policy on a Smart Rule containing that managed account. Membership is recomputed on a schedule, so a new account is not requestable immediately. |
| A Password Safe checkout fails `4034 … 403` | The request is awaiting human approval. An unattended agent does not wait — the job fails rather than hanging. Use an auto-approve access policy for accounts an agent needs. The request it opened is checked straight back in, so it does not hold the account's concurrent-request slot while you fix the policy. |
| `the dashboard refused to release the credential for 'x' (409)` | Read the rest of that line: it carries the dashboard's own reason, and it says explicitly that this is **not** a policy.yaml or connections.yaml problem. Usually the connection has `dashboard_secret: true` here but no credential set on the Connections page. |
| `did not authenticate — sealed to a different key, or for a different agent, job or connection` | The seal was built for something other than what this agent asked for. Almost always a dashboard and agent mid-upgrade against a changed audience; check `AGENT_BASE_URL`/the pinned audience matches on both sides. |
| The dashboard refuses to queue: *"needs at least 2.1"* | This connection takes its credential from the dashboard and the agent image predates that. Pull `chrweav/dashboard-agent:latest` and restart the container; the agent keeps its identity, so no re-enrolment. |
| A connection shows `takes its credential from Password Safe, but this dashboard has no Password Safe API client configured` | Set the BeyondTrust API URL, client id and client secret under Settings. Refused at enqueue rather than as a failed job, because a checkout that cannot authenticate is a configuration state, not a run failure. |
| A job logs *"the `password` left in connections.yaml is IGNORED"* | Exactly what it says — the entry has `dashboard_secret: true` and a leftover local credential. Not fatal, but that plaintext is still sitting on this host, which is the thing you moved the credential to avoid. Delete it. |
| `could not reach vmrest at 127.0.0.1:8697` | Either `vmrest` is not running, or the connection lacks `allow_loopback: true` in policy.yaml — the agent denies loopback by default. **On Docker Desktop `allow_loopback` cannot fix this**, because `127.0.0.1` inside the container is the container: point the connection at `host.docker.internal` and add it as a target instead. See [VMware Workstation Pro](remote-agents/hypervisors.md#vmware-workstation-pro). |
| `vmrest rejected the credential` | Set them with `vmrest -C`, and check the username matches connections.yaml. |
| `vmrest has no 'restart' operation` | Working as intended — its API has no reset, reboot or snapshot. Use power_off then power_on. |
| A sync never runs, and the connection shows an error | Read it — the enqueuer records why rather than queueing a job that would wait indefinitely. Usually the bound agent is offline or lacks the `agent_hypervisor` grant. |
| Caddy never serves; logs show ACME retries | The hostname is internal and cannot satisfy an ACME challenge. Set `AGENT_TLS_INTERNAL=1` — see [above](remote-agents/enrolment.md#if-the-hostname-is-internal). |
| `policy.yaml does not allow Config Management against x:22` | Working as intended, and the row above it in `targets:` does **not** grant this. Config Management has its own `ansible.targets:` list, because "may be port-probed" and "may have a playbook applied as root" are different decisions. Add the range there, with the port named. If it *is* there, check the space after each dash — on an agent image predating that fix, `-cidr:` parsed as a mapping key and the whole list came out empty. |
| `this agent's policy.yaml does not enable Config Management` | No `ansible:` block, or `enabled` is not true. It is a separate grant from `sibling:` even though both need the Docker socket. |
| `policy.yaml enables Config Management but names no ansible.vm_image` (or `db_image`) | A run of that kind has no image. Add the key and `docker pull` it — the agent will not pull for you. `vm_image` is `chrweav/ansible-winrm` (it has pywinrm, so it covers Linux and Windows); `db_image` is `chrweav/ansible-cloud` (it has the database collections). |
| `the Ansible runner image … is not present on this host` | `docker pull` the image named in `ansible.vm_image` / `ansible.db_image`. |
| `could not write the run's files into the container (400): container rootfs is marked read-only` | An agent of 2.3.0 exactly. That build created the runner with a read-only rootfs and no mount, and the Engine refuses an archive extract into one — so every Config-Management run failed here, on every host, a few seconds in. Fixed in 2.3.1 by making `/opt/job` an anonymous volume: pull `chrweav/dashboard-agent:latest` and restart. The agent keeps its identity, so no re-enrolment. |
| A VM is listed in the target picker but **disabled**, hover says "no address" | Its sync reports no guest address. All three of: guest powered on, guest tools installed in it, and `sync_guest_details: true` on that connection in `connections.yaml`. Then **Sync Now**. On Hyper-V this also needs a re-pulled `chrweav/hypervisor-runner` — that image is what asks the guest. |
| A VM does not appear in the picker at all | Either its connection is not agent-bound, or the row is untagged and you are not an admin — an untagged synced VM is admin-only until an admin assigns a workgroup, the same rule every hypervisor page keeps. |
| The dashboard refuses to queue: *"needs at least 2.3"* | The bound agent predates agent-executed Config Management. Pull `chrweav/dashboard-agent:latest` and restart; the agent keeps its identity, so no re-enrolment. |
| The dashboard refuses to queue: *"needs at least 2.4"* | The bound agent predates `agent_gateway`. The refusal names two halves and on a POV both are usually true: pull `chrweav/dashboard-agent:latest` **and** press **Broker** to rewrite the generated policy. Identity survives, so no re-enrolment. |
| `the broker agent … reports it may run … — its policy.yaml predates the Gateway grant` | The image is current but its `policy.yaml` has no `agent_gateway` in `job_types:`. On a POV that file is **generated**, so the fix is the **Broker** button, not an editor — the generic advice ("edit policy.yaml") would mean SSH-ing into a customer's environment. |
| `this dashboard does not permit … to run Gateway jobs` | The dashboard operator's half of the permission. Widen the agent's allowed job types on the Agents page. Distinct from the row above: this one is refused before the agent is consulted. |
| `this agent's policy.yaml does not enable the BeyondTrust Gateway` | No `gateway:` block. Add one with `enabled: true`, the image, and `privileged: true`. Two narrower versions of the same refusal exist and are worth telling apart: *"names no `gateway.image`"* (add the key **and** pull it — the agent will not pull for you) and *"does not set `gateway.privileged: true`"*. |
| `policy.yaml enables the Gateway but does not set gateway.privileged: true` | Refused deliberately rather than attempted. A Gateway needs `NET_ADMIN`, `NET_RAW`, `IPC_LOCK` and `/dev/net/tun`; without them it registers **online** and every tunnel times out, which reads as a firewall for days. The agent will not start one it knows cannot work. |
| `the Gateway image … is not present on this host` | `docker pull` it on the agent host. The agent will not fetch it for you — a pull is a network fetch of executable content, and that is the operator's decision, not a job's. Same rule as the sibling runner. |
| `the Gateway container exited (code N)` | Read `docker logs pov-gateway` on that host. **A wrong image tag and a kernel without `/dev/net/tun` look identical from here**, which is why the message names both. |
| `the dashboard sent an empty Gateway deploy key` | The POV has a key stored but it is blank. Create a Gateway in PRA, copy its deploy key, and paste it onto the POV. Two preflight refusals cover the earlier cases — *"this POV has no Gateway deploy key stored"* and *"this POV names no Gateway"* (the name is what the status check and every later jump item look it up by). |
| A Gateway job goes **green** but PRA shows no node | Expected, and not a contradiction: the agent only proves the container stayed up. The dashboard confirms registration separately against the tenant API. If it stays unregistered, the deploy key is the first suspect — and see [the stale-node case](integrations/gateways.md#troubleshooting) for the other explanation: a rebuilt host registers under the same Gateway **name** from a fresh address, so PRA lists the dead node too — compare node counts, not names. |
| `the Gateway container would not be removed` | Remove it by hand on that host: `docker rm -f pov-gateway`. A removal deliberately does not need the install grant, so a narrowed policy is not the cause. |
| `Agent 'x' is not granted the Config-Management job type` | The dashboard operator's half of the permission. Agents page → that agent → grant `agent_ansible`. Your `policy.yaml` still has to grant it too. |
| `the dashboard sent ansible_connection as extra vars, and this agent refuses them` | Working as intended, and it is the most important refusal here. `ansible_*` variables are connection configuration, not data — `ansible_connection: local` would run the playbook inside the runner container instead of against the target. Use the run form's own user / key / become fields. |
| `this host's Docker logging driver is not file-based … (the Engine answered 501)` | The daemon forbids the per-container `json-file` driver the agent asks for. Check `log-driver` in `/etc/docker/daemon.json`. Without a readable log there is no way to stream the run's output, so this is fatal rather than cosmetic — and it is the default on RHEL, Fedora, Rocky and Alma, which is why the agent pins the driver per container. |
| `the runner was killed for exceeding its 1024 MB memory limit` | A fixed limit in the agent, not a setting. A playbook that needs more than a gigabyte on the *controller* is doing work that belongs on the target. |
| `the run exceeded this agent's ceiling of 30 minutes and was stopped` | Raise `ansible.max_runtime_minutes` in `policy.yaml` if the playbook is legitimately that slow; otherwise something is waiting on an answer that will never come. |
| `policy.yaml sets ansible.network: none` | Refused before the container is created, because with no network every task fails as "unreachable" — which reads as a firewall or credential problem on the target. Use `bridge`, or a network that can reach it. |
| `ansible-playbook exited 4 — the target was unreachable` | The address, the port or the credential. The address came from the agent's own sync, so start with the port: 22 needs sshd, 5985/5986 needs a WinRM listener (`winrm quickconfig`). |
| `ansible-playbook exited 2 — one or more hosts failed a task` | The playbook ran and the play failed; read the output above the recap. Not a wiring problem. |
| `This run's playbook and connection material total N KB, over the 256 KB limit` | Split the playbook, or move the bulk into a role the play fetches itself. The cap exists because the agent's own response ceiling is 1 MB and tripping it produces a far less useful message. |
| A run against an on-prem **database** is refused for a missing endpoint | The registered row has no `private_host`. Re-register it with its address — an agent needs somewhere to connect. |


## Where this is heading

Discovery was the first slice, chosen because no credential crosses the wire at all.
Hypervisor brokering followed it and is described above. Next:

- ~~**Agent-executed Ansible** against private targets~~ — shipped; see
  [Agent-executed Config Management](remote-agents/config-runs.md#agent-executed-config-management). One detail of the
  plan did not survive contact and is worth recording: it was to reuse the cloud runners'
  `PLAYBOOK_B64` env contract byte for byte, and that is not safe. `execve` caps a single
  environment string at 128 KB on a 4 KB-page host and 2 MB on a 64 KB-page arm64 one, so an
  env-delivered playbook works on the machine it was written on and fails on a customer's.
  The files go in through the Docker archive API instead, which also keeps them out of
  `docker inspect`. The just-in-time secret fetch landed as planned, and grew to carry the
  playbook as well — because executable content must be *sealed*, not merely signed.
- ~~**Password Safe JIT checkout by the agent**~~ — shipped, and then superseded for the
  case it was aimed at. See [Where the credential comes from](remote-agents/credentials.md#where-the-credential-comes-from)
  for the agent-side checkout, and
  [the credential the dashboard holds](remote-agents/credentials.md#the-credential-the-dashboard-holds) for the variant
  that leaves *no* credential on the on-prem host, not even a Password Safe client.
- **Nutanix power verbs and snapshots.** Both are full spec PUTs carrying a metadata
  version rather than simple actions, so getting one wrong writes to the VM instead of
  failing. Worth doing carefully rather than quickly.
- **Nutanix and XCP-ng guest addresses.** Both sync VMs but report no guest IP, so their
  guests cannot be Config-Management targets yet — the rows say so rather than failing. The
  data is there (Prism's `nic_list`, XAPI's `VM_guest_metrics`); it is a producer change in
  each sync, gated behind the same per-connection `sync_guest_details` flag.
- **On-premises Kubernetes clusters as agent targets.** `cloud="local"` clusters have the
  identical problem an on-prem database had, and the fix is the same shape — an `agent_id` on
  the cluster row and the existing `run_kind` enum grown a third member.
- **Retiring `POWERSHELL_EXECUTION_MODE=ssh`,** now that a co-located agent does the
  same job by polling outward instead of the dashboard holding an inbound SSH key to a
  Windows desktop.
- ~~VMware Workstation over a co-located agent~~ — shipped; see
  [VMware Workstation Pro](remote-agents/hypervisors.md#vmware-workstation-pro).

`config.py`'s singleton hypervisor keys (one `proxmox_host`, one `vsphere_host`) were
the blocker for all of this. They are now a one-time **seed** for the
`hypervisor_connections` table rather than the source of truth; the old Settings panels
are read-only and say so.


## Where four sections went

These four headings are named, by anchor, in messages that agent images print — so a
deployed agent keeps sending readers here until its image is rebuilt. The headings stay;
the bodies moved.

### Running on Windows

Moved to [Running the agent on your host](remote-agents/agent-host.md#running-on-windows).

### The BeyondTrust Gateway

Moved to [Enrolling an agent](remote-agents/enrolment.md#the-beyondtrust-gateway).

### Sealing a credential this host keeps

Moved to [Credentials an agent uses](remote-agents/credentials.md#sealing-a-credential-this-host-keeps).

### The sibling runner

Moved to [Hypervisor connections through an agent](remote-agents/hypervisors.md#the-sibling-runner).
