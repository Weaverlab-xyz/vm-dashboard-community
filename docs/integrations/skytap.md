# Skytap

The first **lab platform** for POV environments. A POV is a Skytap *template* instantiated
whole; the dashboard reads and (in a later release) creates those environments, then wires
their VMs into that POV's PRA and Password Safe tenant.

Available on a **POV instance only** — see [pov-instance.md](../pov-instance.md). On a demo
instance the integration is masked off and Settings refuses to enable it.

> **No PAM wiring yet.** The dashboard can create an environment from a template, power
> it on and off, and destroy it. It does not yet install a Gateway or a Password Safe
> Resource Broker inside the environment, or onboard its VMs — those arrive in later
> releases, and their columns already exist on the row so they need no migration.

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
| **Create** | Instantiate a template, set the idle timer, power on, wait for it to settle, read the VMs back |
| **Start / Suspend** | A runstate change, then a poll until it settles |
| **Destroy** | Delete the configuration, and everything Skytap keeps inside it |

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

Two smaller rules worth knowing:

- **Destroy is allowed from `failed`,** not only from `active`. A POV that broke halfway
  through provisioning is exactly the one that most needs reaping.
- **An empty VM read never prunes.** A transient error returning zero VMs would otherwise
  delete every VM row — later taking the PAM artifact columns with it — and record success.

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
| Bootstrap injection | **metadata** — per-VM `user_data`, read by the guest at `http://169.254.169.254/skytap` |
| Share link | yes — publish sets, with a password and an expiry |
| Stored credentials | yes — `…/vms/{id}/credentials` |

`bootstrap_injection` is one intent with more than one mechanism. Skytap hands data to the
guest and the guest fetches it; another platform might run a script on the guest instead.
The distinction matters because Skytap's metadata service works **only on VMs attached to
automatic networks**, and nothing executes `user_data` for you — there is no cloud-init
datasource, so the guest must fetch it. A later release covers the resulting template
contract.

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
