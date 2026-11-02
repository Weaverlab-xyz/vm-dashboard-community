# The UI robot on the POV control node

> **Audience:** contributor · **Profile:** `pov` · **Read this when:** you are building or changing the browser robot that fetches the Resource Broker installer and creates the PRA to Password Safe integration, or the agent job that runs it.

Part of [POV design notes](README.md).

**Status: a design. Nothing here is built, and none of it has been run against a live
Password Safe, PRA or control node.** Every statement about the code was checked by reading
it; every statement about a live system is listed under [Not verified](#not-verified). The
code references are to the tree this page was written against, so re-check a line number
before relying on it.

## The problem

Two setup steps cannot be done through an API, so an operator does them by hand today:

1. **Fetch the Resource Broker installer and its installer key.** The package is generated
   per Password Safe tenant, and nothing in the dashboard fetches it —
   [the Resource Broker design](resource-broker.md) and
   [The Gateway and the Resource Broker](../gateway-and-broker.md) both say so. The
   operator downloads it, uploads the `.exe` on `/storage`, and types the key into the POV.
2. **Create the PRA to Password Safe integration.** The PRA API cannot, so it is a manual
   `/login` setting that the POV only *checks* (`pov_pra_ps_link.py`, the `pra_ps` ladder
   step).

Both are browser work. A Playwright robot can do them, and the only question is where it
runs and how it reaches the dashboard.

## The decision

**The robot is a sibling container on the POV control node, started by the dashboard agent,
and it calls the dashboard as a service account that authenticates with a SPIFFE JWT-SVID.**
The control node already runs the agent, the Gateway and the Resource Broker, and the agent
already starts sibling containers over the Docker socket (`_run_ansible_sibling`,
`agent.py:4601`). The agent starts and supervises the robot and hands it its logins; the
robot talks to the dashboard's *existing* storage and POV routes for the rest.

```
dashboard ── agent_ui_robot job ──▶ agent ── Docker Engine API ──▶ ui-robot container
    ▲                                                                  │        │ browser
    │   existing /api/storage/upload/* and                             │        ▼
    └── /api/pov/managed/{env}/resource-broker  ◀── service-account ───┘  Password Safe / PRA UI
                                                    token (JWT-SVID)
```

Rejected: a new agent-authenticated upload route, so the robot would hold no dashboard
credential at all. It was the first design here and was dropped: it is the largest piece of
new code (a route that does not exist, a tar stream read in the agent, a key-handoff route),
and the SPIFFE service-account path reaches the same end with routes that already exist.
The cost is stated below, under what the robot holds.

**No PAT anywhere.** The dashboard refuses a PAT for a service account at use
(`api/auth.py:73-77`). A service account authenticates at `POST /api/oauth/token`, and with a
SPIFFE JWT-SVID it stores no secret at all: see
[Authenticating with a SPIFFE assertion](#5-authenticating-with-a-spiffe-assertion).

## The pieces

### 1. A new job type, `agent_ui_robot`

Payload: `task` (`installer` or `integration`) and `environment_id`, nothing else. Every
agent job type has its own closed allowlist module (`agent_gateway_meta.py` is the pattern),
because the envelope is signed and carries no free-form string.

A job type has to be added in each of these, and `tests/test_agent_lease_invariants.py` pins
several of them:

| Where | What |
|---|---|
| `agent_service.py:68` `AGENT_JOB_TYPES` | the dashboard-side grant; must stay disjoint from `jobs_worker.HANDLED_TYPES` |
| `api/agent.py:420` `_envelope_payload` | one branch per type, **no default** — an unlisted type raises |
| `agent.py:5946` `HANDLERS`, `Policy.load` (`agent.py:1437`) | the handler, and a `ui_robot:` policy block naming the image |
| `agent.py` `AGENT_VERSION` and a `MIN_*_VERSION` gate | enqueue-time refusal for an agent too old to have the handler |
| `pov_broker.py:149` `BROKER_JOB_TYPES`, `render_policy` (`:505`) | the control node's own grant and `policy.yaml` |
| `pov_broker.py` bootstrap `pulls` (`:921`) and `images` (`:1282`) | the image has to be on the host, see below |

**The agent never pulls an image.** A missing image is a policy refusal, "not present on
this host" (`agent.py:4243`). The bootstrap pulls images itself, so the robot image goes in
that list or the job fails the first time it runs.

**A control node enrolled before the grant has no policy for it** (`pov_broker.py:145-148`).
Existing POVs need their control node re-brokered; adding the type does nothing for them.

### 2. A preflight, in the order the gateway does it

A new `pov_ui_robot.py` with a `preflight` shaped like `pov_gateway.preflight`: cheapest and
most likely wrong first, every refusal written for the operator, every one ending "Nothing
was queued." (a failed row renders only `error_message`). Order: the control node agent
exists and is online; it supports the type (version gate); it *reports* the type (the
`policy.yaml` grant); the dashboard allows it for that agent; then the domain checks —
tenant URLs, credentials, and for `integration` a wired-up environment.

### 3. A browser needs its own container settings

The sibling HostConfig is a constant sized for Ansible (`_ANSIBLE_HOSTCONFIG`,
`agent.py:4343`): 1 GB of memory, a 128 MB `/tmp`, a read-only root filesystem, no shared
memory setting. Chromium will not run in that. The robot gets **its own constant** — a
`ShmSize`, more memory and `/tmp`, a writable work volume — and, like the others, no field of
it may come from the job. The existing test that asserts this
(`test_no_hostconfig_field_comes_from_the_job`) must cover the new constant.

The container keeps what is worth keeping: dropped capabilities, `no-new-privileges`, a pids
limit, the `com.weaverlab.dashboard-agent` label so `sweep_orphans` reaps leftovers, `exec`
so the process is PID 1 and gets SIGTERM, and a deadline from policy that the cancel watcher
enforces.

**Concurrency.** Everything but `agent_ansible` shares one serial slot
(`CONCURRENT_JOB_TYPE`). A robot run is minutes long, and on the control node it would hold
up discovery and gateway jobs. It needs its own slot, capped at one.

### 4. Credentials arrive sealed, as a file

The Password Safe and PRA logins (and TOTP secrets, if the account has MFA) never ride in the
job envelope or in `Env`. The pattern already exists for Ansible: the agent POSTs a reply
key and the dashboard seals a bundle bound to `agent_id + job_id`
(`/jobs/{id}/ansible-bundle`, `api/agent.py:1052`). A sibling route,
`/jobs/{id}/ui-robot-bundle`, releases what the job row says and nothing the request says.

The agent writes the bundle into the container with the archive PUT it already uses
(`_archive_put`), as a file in the work volume. **Not in `Env`**: an environment variable is
visible to `docker inspect` for the container's life, and a file is not. Each value goes
through `hold_secret` (`agent.py:1752`), which scrubs exact-string matches from every
outbound body.

**Redaction is exact-match only, and ignores values under four characters.** A secret that
the robot prints base64-encoded, URL-encoded or split across lines reaches Live Output. The
robot must therefore never echo a credential in any form, and the page's selectors file must
never be recorded against a real password (the recorder writes what you type into its
output).

### 5. Authenticating with a SPIFFE assertion

`services/spiffe_assertion.py` lets a workload that holds a JWT-SVID exchange it for a
short-lived dashboard token as a service account, with no stored secret
(`api/oauth.py`, `POST /api/oauth/token`):

```
grant_type=client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-spiffe
client_assertion=<JWT-SVID>
```

What the dashboard checks, and so what the robot's side must satisfy:

| Check | Consequence for the robot |
|---|---|
| signature against the registered trust domain's JWT-SVID keys | the dashboard's own trust domain is registered when SPIRE is on; a Workload Lab's must be added |
| **audience** is the dashboard's issuer URL or its `/api/oauth/token` | the robot needs its own SVID. The agent's is minted for `/api/agent/attest` and is refused here, by design |
| lifetime at most 3600 s | SPIRE's default is 5 minutes, fine |
| **single use**: the assertion's hash is recorded until it expires | one exchange per SVID; re-read the file for each exchange, and after a 401 |
| `sub` names an OAuth client created with `auth_method="spiffe_jwt"` | one SPIFFE ID binds one active client, bound to one service account |
| throttled by source address only | many failed exchanges from one NAT lock out its agents |

On a control node that attests through SPIRE, `pov_broker` already starts `spire-agent`
and `spiffe-helper`, and the helper's config (`jwt_svids = [{jwt_audience=…,
jwt_svid_file_name="attest.jwt"}]`, `pov_broker.py`'s `_render_spire_block`) is where a
second entry, audience `<dashboard>/api/oauth/token` and file `oauth.jwt`, would go. The
robot container then mounts that volume read-only and `DASHBOARD_SPIFFE_JWT_FILE` points at
the file. The standalone scaffold already does this (`DashboardStorage.from_spiffe_svid`),
and falls back to a client id and secret when no file is given.

**What the robot holds.** A token that lives for the service account's `token_ttl_seconds`,
in a container that renders third-party pages. So the service account must hold **only** what
the robot needs (storage write, and write on the one POV), and the token request should narrow
it further with `scope`. This is the price of dropping the agent upload route.

Caveats that decide whether it is usable at all:

- **SPIRE has to be up on that node.** A control node that could not reach the SPIRE server
  falls back to an Ed25519 agent (`SPIRE_WAIT_SECONDS`, `pov_broker.py`) and has no SVID, so
  the robot could not authenticate there.
- **The SPIFFE ID.** An SVID's SPIFFE ID comes from the SPIRE registration entry that matches
  the workload. Whether the robot container can have its own entry, or shares the helper's,
  is not established here, and a shared ID would make the robot's service account the
  agent's.
- A service account's `pov:write` must resolve against the POV (`pov_gates.require_write_on_env`
  accepts a service account's permission; `pov_own` ownership for one was not checked).
- A 300 MB upload outlasts a short token TTL; the scaffold re-exchanges after a 401, which
  needs a fresh SVID from the helper in time.
- A token is not scoped to one POV. The `pov:write` it holds applies to every POV the service
  account may write, so a compromised robot could stage a key on a different POV.

### 6. What the robot sends

Both calls are existing routes, made with the service-account token.

- **The installer** goes through the chunked upload lane, `/api/storage/upload/begin`,
  `part/{n}`, `commit` and `abort` (see
  [Uploading assets](../../../operations/storage-management/uploading.md)). It lands on
  whichever backend is active. The wire-up hands it to the Windows VM by signed URL above
  128 KB and the agent-brokered backend cannot do that, so the POV needs S3, Azure Blob or GCS
  active, as today. A failed part aborts, so no staged parts keep billing.
- **The key and the asset name** go in one call to
  `POST /api/pov/managed/{env}/resource-broker` with `install: false`
  (`pov_wiring.py:277`). That route stores the key at `pov/<env>/rb_installer_key` under
  Fernet (`set_installer_key`, `pov_resource_broker.py:105`) and records the asset. **`install`
  must be false**: its default queues a Resource Broker install as a side effect, and the
  robot only stages. The key is sent once, never printed, never put in the job result (a
  result is merged into the job's metadata and shown).

The agent's job result carries only status: which task ran and whether it succeeded.

The **zone** is still an operator input (`rb_zone`), and `rb_ready` needs it. If the Password
Safe page shows it, the robot could capture it, but that is a selector nobody has recorded;
until then the preflight refuses with a message that says so.

### 7. Where it sits in the setup ladder

The ladder is `pov_setup_steps.STEPS` (`:384`). The `installer` task belongs **before**
`resource_broker`, and fills the two things that step is currently `BLOCKED` on ("needs a
staged installer", "needs an installer key"). The `integration` task belongs after
`wireup`, because it needs the Password Safe API key and onboarded accounts that wire-up
produces, and before the manual `pra_ps` check.

**The robot's success is not the integration's success.** A toast on a PRA page says the
form saved; it does not say PRA can read Password Safe. `pov_pra_ps_link.check` already
counts matched accounts, and a green `integration` step should wait for that, as the
Resource Broker step stays `CONFIGURED` and never `DONE` because the dashboard cannot read
back whether the install ran (`pov_setup_steps.py`, the ladder's own comment).

Use the ladder's vocabulary: `SKIPPED` means this recipe never wanted it, `BLOCKED` is about
the run's state, and a robot that was simply never run is neither. If the job writes
per-step lines the wire-up counters read, they are matched on the words `FAILED` and
`skipped` (`pov_wireup.py:1047`), so a new line has to use them or it is miscounted.

### 8. The image

`runners/ui-robot/` (Playwright for Python on the official Playwright image, pinned to the
matching `playwright` version) becomes a `publish-images.yml` matrix entry with
`sign: true`, so the agent can pin the signing identity like it does for its other images.
`images.yml` reads the matrix, so CI builds it on every PR with no extra work.

The Trivy step fails on a fixable HIGH or CRITICAL in OS or Python packages
(`scripts/ci/trivy_image.sh`), and a browser base image is the likeliest thing to trip it.
The first build will show.

`storage_client.py` in the scaffold is the robot's dashboard client on the control node too,
not just a fallback. It also serves running the robot by hand from a laptop, which is how
selectors get recorded.

## Network reach

`render_policy` grants the control node's agent the POV's own `/32` targets and nothing
else (`pov_broker.py:505`). That is an *agent policy*, not a firewall, so it does not by
itself stop a sibling container from reaching a tenant URL, but the **security group or NSG
on each platform** may. The robot needs outbound HTTPS to the Password Safe and PRA tenants
from every platform a POV runs on (Skytap and each cloud). Nobody has checked that.

## Tests this needs

Not written. When it is built: the lease-invariants suite (an envelope branch exists for the
new type, and the type is disjoint from `HANDLED_TYPES`), the HostConfig-from-job test, a
bootstrap fit test (`test_pov_bootstrap_fit.py` — the 16,384-character limit and the images
list are asserted there), a preflight test in the `_ready()` fixture style of
`test_pov_resource_broker.py`, a ladder test with the new rows added to the `_row()` fixture,
and a test that the installer key never appears in a job's metadata. The agent upload
route is gone, so there is no new dashboard route to test.

## When this ships, fix the text that says it cannot

These statements are true now and become false: `design/resource-broker.md` ("the dashboard
cannot obtain it"), `gateway-and-broker.md` ("The dashboard cannot download the
installer"), and the refusal strings in `pov_resource_broker.py` that tell an operator to
download and upload by hand (`:238-240`, `:436-447`). `test_pov_resource_broker.py` asserts
on some of that wording.

## Not verified

- That any selector, login flow or download behaves as assumed on a real Password Safe or
  PRA. None are recorded; `selectors.py` is all `TODO`.
- That Chromium runs under the proposed HostConfig, and what its memory and `/dev/shm`
  needs are.
- That a 300 MB upload from the control node's network to the dashboard completes within a
  token's lifetime, and what it does to a small control node VM.
- That each platform's network lets the container reach both tenants.
- That the PRA account and PRA's MFA can be driven unattended. A service account with TOTP
  is assumed; SSO is not supported.
- That the Password Safe page exposes the zone.
- That the SPIFFE assertion path works for the robot at all: no SVID client has been created
  for it, and the helper has no second `jwt_svids` entry.
- Anything about a control node that already exists (it needs a re-broker first).
