# Skytap

The first **lab platform** for POV environments. A POV is a Skytap *template* instantiated
whole; the dashboard reads and (in a later release) creates those environments, then wires
their VMs into that POV's PRA and Password Safe tenant.

Available on a **POV instance only** — see [pov-instance.md](../pov-instance.md). On a demo
instance the integration is masked off and Settings refuses to enable it.

> **Partial PAM wiring.** The dashboard can create an environment from a template, power
> it on and off, destroy it, enrol an **agent inside it** — see
> [The broker VM](#the-broker-vm) and the [template contract](#the-template-contract) —
> install a **BeyondTrust Gateway** on that broker, registered into the POV's own PRA
> tenant ([the POV Gateway](../pov-instance.md#the-pov-gateway)), and install a **Password
> Safe Resource Broker** on a Windows VM beside it
> ([the Resource Broker](../pov-instance.md#the-resource-broker)). It does not yet onboard
> the environment's own VMs; that arrives in a later release, and the columns already exist
> on the row so they need no migration.

---

## Prerequisites

| | |
|---|---|
| Account | A Skytap account whose user can see the templates you want to build POVs from |
| Credential | An **API security token** from the Skytap account page — **not** your account password |
| Network | Outbound HTTPS from the dashboard to `cloud.skytap.com` |

### Getting the token

In Skytap, open your **Account** page. If an API security token is listed there, that is
what you use; Skytap authenticates with HTTP Basic as `username:api_token`.

This is the single most common setup mistake, so it is worth stating plainly: **the
password field is not your password.** A wrong value produces a `401`, and the dashboard's
error message says so rather than reporting a generic failure.

## Configuring it

**Settings → Integrations → Skytap.**

| Field | Notes |
|---|---|
| API URL | `https://cloud.skytap.com` unless your account is on another region's endpoint |
| Username | The account's login, usually an email address |
| API security token | From the account page. Stored encrypted; the panel shows "stored — leave blank to keep" once set |
| Project ID | Optional. Scopes environments to a Skytap project, for access control and usage reporting |

The token is encrypted at rest with the same Fernet key as every other secret, and — like
any secret in this dashboard — it can instead be a reference into an external vault.

## What the dashboard does with the API

Three of Skytap's behaviours are easy to get wrong once and then wrong everywhere, so all
three are handled in one place (`services/skytap_client.py`) rather than at each call site.

**`423 Locked` is normal.** It is not an error — it is Skytap saying "this resource is
busy, or the account is being rate-limited", and it carries a `Retry-After`. Environments
also expose a `rate_limited` boolean, which the POV page surfaces as a badge. The client
retries, honouring `Retry-After`, bounded; only after that does it report a failure, and
the message says it is a rate limit rather than a fault.

**Every read carries `keep_idle=true`.** Without it, *reading* an environment resets its
idle timer. A dashboard that polls environments would hold every one of them awake and
quietly defeat `suspend_on_idle` — the single biggest lever on Skytap spend. The only
symptom would be the invoice, which is exactly why it is not left to the caller.

**Collections paginate by count/offset.** A single GET returns a first page that looks
exactly like a complete answer, so listings are walked to the end.

## The lifecycle

| Action | What happens |
|---|---|
| **Create** | Instantiate a template, set the idle timer, power on, wait for it to settle, read the VMs back, then enrol [the broker agent](#the-broker-vm) |
| **Start / Suspend** | A runstate change, then a poll until it settles |
| **Broker** | Re-issue the enrolment code and re-write the bootstrap. The remedy for every way the first attempt can fail |
| **Gateway** | Start a BeyondTrust Gateway container on the broker VM, registered into this POV's PRA tenant |
| **Resource Broker** | Run the staged Password Safe installer on a Windows VM, over WinRM from the broker |
| **Destroy** | Revoke the broker agent, then delete the configuration and everything Skytap keeps inside it |

Three orderings are load-bearing, and each is wrong in a way that leaves a resource nobody
can reclaim:

**The platform id is persisted before anything else can fail.** An environment that exists
in Skytap and not in this database is the one failure mode nothing can clean up
automatically. So the create call is followed immediately by a commit, and every later step
is written to be re-runnable around it.

**A failure after creation keeps the id.** If the power-on fails, the row stays `failed`
*with* its Skytap id and a message telling you to press Destroy. Failing without the id is
how an orphan is made.

**A failed destroy does not mark the row destroyed.** Marking it would hide an environment
that is still running and still billing. The row stays visible, keeps its id, and Destroy
can be re-run once the cause is cleared.

Three smaller rules worth knowing:

- **Destroy is allowed from `failed`,** not only from `active`. A POV that broke halfway
  through provisioning is exactly the one that most needs reaping.
- **An empty VM read never prunes.** A transient error returning zero VMs would otherwise
  delete every VM row — later taking the PAM artifact columns with it — and record success.
- **The broker step cannot fail the provision.** An environment that is up with no agent in
  it is running, billing and reapable; failing it would trade a fixable gap for a destroyed
  environment. The reason lands on the row next to a **Broker** button that re-runs it, and
  never in the row's `error_message`, which means "this environment is broken".

---

## The broker VM

Everything the dashboard does *inside* a POV after it is running goes through one VM in
the environment: the **broker**. It runs the [remote agent](../remote-agents.md), and the
agent dials out — because a POV lives on a lab platform's private network and this
dashboard has no route into it. The Gateway install, the Resource Broker install and the
per-VM wire-up all wait on this one thing, so it is worth setting up properly once.

Which VM? The one whose name matches the POV's **Broker VM name** — `broker` unless you
set something else when you create the POV. The match is exact and case-insensitive, and
deliberately not fuzzy: "contains broker" also matches a customer VM called
`password-broker`, and the cost of that mistake is an agent installed on a machine nobody
expected. If no VM matches, the POV still comes up and the Broker column reads `none` with
the names it *did* find.

A POV that also runs a Resource Broker needs a **second** special VM — a Windows Server
2019 or 2022 x64 guest with WinRM enabled, on the same automatic network. See
[the Resource Broker](../pov-instance.md#the-resource-broker); the rest of this section is
about the Linux broker.

That VM needs three things:

| | |
|---|---|
| **Docker** | The agent is a container, and so is the Gateway it later runs beside itself. Podman works if `docker` resolves to it |
| **An automatic network** | Skytap's metadata service answers only on VMs attached to one. On a manual network the VM gets no metadata at all, which looks exactly like a missing runner |
| **The metadata runner** | Below. Skytap hands `user_data` to the guest and **nothing executes it** |

### What the dashboard generates

A `policy.yaml` granting **one `/32` per VM in this POV**, on ports 22, 443, 3389, 5985 and
5986 — not the environment's subnet. The subnet is what the platform hands out and it is
bigger than the POV; on a shared lab network it can contain somebody else's environment
entirely. `169.254.0.0/16` is denied, because the agent has no business at the metadata
service the bootstrap itself came from.

Then a `/bin/sh` script that writes that policy, writes a single-use enrolment code, and
starts the agent container. Two lines in it are load-bearing:

- **It removes the agent's state volume** before starting. The agent writes its identity
  there at first enrolment and never redeems a code again, so a re-run with a surviving
  volume gives you a container that starts cleanly, signs with a key the dashboard has just
  cleared, and 401s forever. That reads as a revoked agent, not as a stale volume.
- **The code file is written `umask 022`.** The container runs as uid 10001 and cannot read
  a root-owned `0600` file; the agent says so and exits rather than enrolling.
- **The Docker socket is mounted**, which is root on the broker VM. The Agents page
  deliberately does *not* emit that mount — an operator's own agent host is theirs, and
  adding it there is a separate considered act. A POV broker is different: the dashboard
  created that VM from a template for this POV, and running the Gateway beside itself is
  the machine's only job.

### The ordering that matters

The payload is injected **after** the power-on, never before. An enrolment code lives
fifteen minutes and a first boot is not bounded — a Windows template pulling updates can
eat all of it. Injecting first hands the guest a code that expired while it was starting,
and the symptom is an agent stuck at `enrolling` with no request ever reaching the
dashboard.

Once the agent enrols, the dashboard **clears `user_data`**. It is readable by anyone who
can read the environment in Skytap, and clearing it also stops a reboot re-running a
bootstrap whose code is spent.

## The template contract

Skytap's `bootstrap_injection` mechanism is **`metadata`**: the platform stores the payload
and the guest fetches it. There is no cloud-init datasource and nothing runs it for you, so
the broker VM in your template must carry a small runner. This is the one piece of the
feature that lives in your image rather than in this dashboard.

The runner has to do four things. Anything that does them is fine; this is the shape:

```sh
#!/bin/sh
# /usr/local/sbin/dashboard-bootstrap-runner - run at boot and keep running.
set -eu
MARKDIR=/var/lib/dashboard-bootstrap
mkdir -p "$MARKDIR"

while :; do
  body=$(curl -fsS http://169.254.169.254/skytap/vms/self/user_data 2>/dev/null || true)
  case "$body" in
    *BEGIN-DASHBOARD-AGENT-BOOTSTRAP*END-DASHBOARD-AGENT-BOOTSTRAP*)
      sum=$(printf '%s' "$body" | sha256sum | cut -d' ' -f1)
      if [ "$sum" != "$(cat "$MARKDIR/last" 2>/dev/null || true)" ]; then
        printf '%s' "$body" > /run/dashboard-bootstrap.sh
        sh /run/dashboard-bootstrap.sh && printf '%s' "$sum" > "$MARKDIR/last"
      fi
      ;;
  esac
  sleep 20
done
```

1. **Poll, do not read once.** The payload arrives *after* the VM is up, because of the
   ordering above. A runner that reads `user_data` once at boot finds it empty and stops.
2. **Require both markers.** `BEGIN-DASHBOARD-AGENT-BOOTSTRAP` and
   `END-DASHBOARD-AGENT-BOOTSTRAP` must both be present before anything runs. A truncated
   metadata read would otherwise execute half the script — and the half at the top is the
   half that deletes the running agent and its state volume.
3. **Key the "already ran" marker on the payload's hash, not on a flag.** A reboot with
   unchanged `user_data` must not re-run; a *re-injection* — a new enrolment code — must.
   A boolean gets exactly one of those right.
4. **Run it as root.** It writes to `/etc/dashboard-agent` and calls `docker`.

Install it as a service that starts at boot — a systemd unit with `Restart=always` is the
usual form — and bake it into the template. It is idempotent and it costs one HTTP call to
a link-local address every twenty seconds.

> If your account's metadata service does not serve
> `http://169.254.169.254/skytap/vms/self/user_data`, read `http://169.254.169.254/skytap`
> instead and pull `user_data` out of the JSON document. The payload is what matters; the
> path is Skytap's.

## What is deliberately not used

**The Terraform provider.** `skytap/skytap` last released v0.15.1 in November 2022, and its
own documentation says it "doesn't enumerate the resources contained within that template,
including VMs and networks" — which is the one thing a POV needs, because a POV *is* a
template instantiated whole. The dashboard talks to the REST API directly, as the Portainer
and Rancher integrations do. That also keeps the feature clear of the provider pre-cache
coupling in the image build.

**Published services for PAM wiring.** Skytap can NAT a guest port to a public `ip:port`,
and the dashboard displays those, but the wire-up does not use them: a published address
changes per environment and per power cycle. POV wiring reaches VMs on their **private**
IPs through a Gateway inside the environment.

## Capabilities

The registry (`services/lab_platforms.py`) records what each platform can do, so a feature
a platform lacks degrades visibly instead of failing late:

| Capability | Skytap |
|---|---|
| Templates | yes — `/v2/templates` |
| Runstate | yes — running / suspended / stopped / halted |
| Idle suspend | yes — `suspend_on_idle`, per environment, in seconds |
| Bootstrap injection | **metadata** — per-VM `user_data`, read by the guest at `http://169.254.169.254/skytap`. Used by [the broker VM](#the-broker-vm) |
| Share link | yes — publish sets, with a password and an expiry |
| Stored credentials | yes — `…/vms/{id}/credentials`. Used by [the Resource Broker install](../pov-instance.md#there-is-no-login-field-on-purpose), which is why the dashboard stores no Windows password for a POV |

`bootstrap_injection` is one intent with more than one mechanism. Skytap hands data to the
guest and the guest fetches it; another platform might run a script on the guest instead.
The distinction matters because Skytap's metadata service works **only on VMs attached to
automatic networks**, and nothing executes `user_data` for you — there is no cloud-init
datasource, so the guest must fetch it. That is what
[the template contract](#the-template-contract) above specifies, and it is why the broker
install refuses outright on a platform whose mechanism is anything other than `metadata`
rather than failing somewhere inside a job.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Skytap rejected the credentials … uses an API token, not your account password" | The account password was pasted into the token field | Use the API security token from the Skytap account page |
| The POV page says Skytap is not configured | No URL, username or token stored | Settings → Integrations → Skytap |
| Settings refuses to enable it with a 409 | This is a demo instance | Skytap is POV-only; see [pov-instance.md](../pov-instance.md) |
| No POV nav link at all | `pov_environments_enabled` is off, or the profile is `demo` | Both must be true — `GET /api/features` shows `install_profile` |
| "Skytap is still busy after N retries" | The account is genuinely rate-limited | Expected under heavy concurrent use; retry shortly. Running or suspending many VMs at once makes it more likely |
| An environment shows a **rate-limited** badge | Skytap set `rate_limited` on it | Operations against it will be slow until it clears |
| Environments list is empty but the account has some | The token's user cannot see them, or they belong to another project | Check the user's access in Skytap; clear the Project ID to widen the scope |
| VM counts show `—` | The collection read did not include the VM array | Expected. Open the environment for the measured count — a dash means "not measured", never zero |
| The Broker column reads **none** and the row names other VMs | No VM matches the POV's Broker VM name | Rename the template's broker VM, or create the POV with the name your template actually uses. The match is exact |
| The Broker column reads **enrolling** and never changes | Nothing executed the payload | The broker VM has no metadata runner ([template contract](#the-template-contract)), is on a manual network, or cannot reach the agent endpoint. Fix it and press **Broker** to re-issue |
| "no agent enrolled within 14 minutes" | Same causes as above | The bootstrap is still on the VM — `docker logs dashboard-agent` there, if it ever started, says which |
| "this dashboard does not know its own public URL" | No pinned audience and no `public_base_url` | An agent inside a customer network needs an address. Set it in Settings → Integrations → Remote Agents |
| "the agent endpoint is `http://…`" | The audience is plaintext | The agent refuses to sign over plaintext, so the broker would never enrol. Terminate TLS and correct **Public base URL** |
| The broker was online and 401s after a re-run | Its state volume survived a re-broker | The generated bootstrap removes it. A hand-run `docker run` that skipped that line leaves the old identity in place — remove the `dashboard_agent_state` volume and press **Broker** |
