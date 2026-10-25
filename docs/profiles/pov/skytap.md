# Skytap

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are pointing a POV instance at Skytap and want to know what it will and will not do with the account.

The first **lab platform** for POV environments. A POV is a Skytap *template* instantiated
whole; the dashboard creates those environments and wires their VMs into that POV's PRA,
Password Safe and Entitle tenants.

Available on a **POV instance only** — see [POV profile](README.md). On an estate
instance the integration is masked off and Settings refuses to enable it.

> **What the dashboard does with an environment.** Create it from a template, power it on
> and off, and destroy it. Enrol an **agent inside it** — see
> [The control node VM](skytap/control-node.md) and the [template contract](#the-template-contract).
> Install a **BeyondTrust Gateway** on that control node, registered into the POV's own PRA
> tenant ([the POV Gateway](gateway-and-broker.md#the-pov-gateway)), and a **Password Safe
> Resource Broker** on a Windows VM beside it
> ([the Resource Broker](gateway-and-broker.md#the-resource-broker)).
> Then, per VM: **one PRA jump item** through that Gateway, **Password Safe onboarding**
> through that Resource Broker, and **Entitle registration** for the Linux guests
> ([wiring the VMs](wiring.md#wiring-the-vms-into-pra-password-safe-and-entitle)).
> Publish a **customer-facing share link** for the whole environment
> ([the share link](customer-access/share-link.md)) — Skytap calls it a
> publish set, and the dashboard always gives it a generated password and an expiry. And
> reap the environment on an **auto-delete timer**
> ([the auto-delete timer](lifecycle.md#the-auto-delete-timer)), which warns on a
> ladder rather than once because an evaluation outlives whoever set it up.

---

## Prerequisites

| | |
|---|---|
| Account | A Skytap account whose user can see the templates you want to build POVs from |
| Credential | An **API security token** from the Skytap account page — **not** your account password |
| Network | Outbound HTTPS from the dashboard to `cloud.skytap.com` |
| Network, for template builds only | Outbound **TCP to an arbitrary high port** at a Skytap NAT address. Only the automatic runner install needs this — see [building a template](skytap/building-a-template.md). An egress rule that allows 443 to the API host and nothing else will block it, and the builder says so rather than reporting a generic connection failure |
| Network **from the guest** | A route to a container runtime, unless the base template already carries one: either outbound HTTPS from the **control node VM** to `download.docker.com`, **or** the guest's own distro repository carrying `podman` + `podman-docker`, which is the fallback and is usually already reachable. This is the guest's route out, not the dashboard's — the only prerequisite on this list that nothing on this side can test for you. A build that cannot reach either still bakes, with the reason in the **Runner** column, and the POV's first **Install control node** press tries again |

### Getting the token

In Skytap, open your **Account** page. If an API security token is listed there, that is
what you use; Skytap authenticates with HTTP Basic as `username:api_token`.

This is the single most common setup mistake, so it is worth stating plainly: **the
password field is not your password.** A wrong value produces a `401`, and the dashboard's
error message says so rather than reporting a generic failure.

## Configuring it

**Settings → Integrations → Skytap.**

| Field | Key | Notes |
|---|---|---|
| API URL | `skytap_base_url` | `https://cloud.skytap.com` unless your account is on another region's endpoint |
| Username | `skytap_username` | The account's login, usually an email address |
| API security token | `skytap_api_token` | From the account page. Stored encrypted; the panel shows "stored — leave blank to keep" once set |
| Project ID | `skytap_project_id` | Optional. Templates and environments are **listed from** this project, and new environments are **created inside** it. Blank lists everything the token can see |

The token is encrypted at rest with the same Fernet key as every other secret, and — like
any secret in this dashboard — it can instead be a reference into an external vault.

### Verifying the connection

The Skytap panel has a **Test connection** button. It reads the **saved** values, not what
is currently in the form, so save first.

It issues one page of `GET /v2/templates` — the same read the POV page makes — scoped to
the Project ID when one is set. That single call answers three questions at once: whether
the token authenticates, what this account can actually *see*, and whether the Project ID
is real.

It distinguishes five outcomes:

| It says | Meaning |
|---|---|
| **Connected. Templates are visible** | Everything the POV page needs |
| *not configured* | One of the three connection fields is empty |
| *Skytap rejected the credentials* | Wrong username or token. Note it is an **API security token**, not your account password |
| *exposes no templates* | The token works, but the account — or the project — shows nothing. There is nothing to build a POV from, so this is reported as a **failure**, not a warning |
| *no project N (404)* | The credentials work; the Project ID is stale or wrong |
| *the host could not be reached* | DNS, firewall or proxy — not a credential problem |

An account that authenticates but exposes no templates is deliberately red. A POV cannot be
created from an empty catalogue, so reporting green for a connection that cannot do the one
thing it is for would be the exact false positive a check like this exists to prevent.

## What the dashboard does with the API

Three of Skytap's behaviours are easy to get wrong once and then wrong everywhere, so all
three are handled in one place (`services/skytap_client.py`) rather than at each call site.

**`423 Locked` is normal.** It is not an error — it is Skytap saying "this resource is
busy, or the account is being rate-limited", and it carries a `Retry-After`. A plain `429`
is retried identically. Environments also expose a `rate_limited` boolean, which the POV
page surfaces as a badge. The client retries, honouring `Retry-After`, bounded; only after
that does it report a failure, and the message says it is a rate limit rather than a fault.

The retry budget is per client. Provisioning gets the generous default, because a job that
has already waited minutes should keep waiting; **Test connection** asks for one retry, so
a rate-limited account answers the button instead of hanging behind a spinner.

**Every read carries `keep_idle=true`.** Without it, *reading* an environment resets its
idle timer. A dashboard that polls environments would hold every one of them awake and
quietly defeat `suspend_on_idle` — the single biggest lever on Skytap spend. The only
symptom would be the invoice, which is exactly why it is not left to the caller.

**The dashboard's view of an environment is refreshed on a timer.** A POV's `runstate` used
to be written only when this dashboard changed it — at provision, or at an explicit Start or
Suspend. Nothing asked Skytap again. So `suspend_on_idle`, the biggest lever on spend, would
suspend an environment and the POV page would go on saying `running` indefinitely; worse, it
gates the Start button on that value, so the environment you most needed to wake was the one
whose Start button was hidden. A **reconcile sweep** (`services/pov_reconcile`) now reads
every managed environment back every ten minutes — one paginated collection read — and the
POV page shows when each row was last confirmed rather than implying it is live. Each pass
is a `pov_env_reconcile` job; completed ones are hidden on Job History unless you tick
**Show routine sweeps**, and are pruned on the same retention as the other sweeps.

**Collections paginate by count/offset.** A single GET returns a first page that looks
exactly like a complete answer, so listings are walked to the end.

**Listings are project-scoped when a Project ID is set** — they read
`/v2/projects/{id}/templates` and `/v2/projects/{id}/configurations` instead of the
account-wide collections. A project id the token cannot see answers `404`, and the
dashboard turns that into a message naming the remedy rather than a bare error. That is
why the sub-resource paths were chosen over a filter parameter: a filter an API does not
implement is silently ignored, and an unscoped list looks exactly like a correct one.

## The lifecycle

| Action | What happens |
|---|---|
| **Reconcile** | Every ten minutes, and on **Re-check platform**: read every managed environment back, update its runstate, rate-limit flag and idle timer, and flag one that has vanished. Never destroys anything |
| **Build a template** | Instantiate a base template into a scratch environment, check it against [the template contract](#the-template-contract), install the metadata runner on its control node VM, save the environment back as a **new template**, then reap the scratch environment. See [building a template](skytap/building-a-template.md) |
| **Create** | Instantiate a template, set the idle timer, power on, wait for it to settle, read the VMs back, then enrol [the control node's agent](skytap/control-node.md) |
| **Start / Suspend** | A runstate change, then a poll until it settles |
| **Install control node** (**Rebuild control node** once its agent is online) | Re-issue the enrolment code and re-write the bootstrap. The remedy for every way the first attempt can fail |
| **Gateway** | Start a BeyondTrust Gateway container on the control node VM, registered into this POV's PRA tenant |
| **Resource Broker** | Run the staged Password Safe installer on a Windows VM, over WinRM from the control node |
| **Wire** | One PRA jump item per VM through this POV's Gateway, one Password Safe managed system through its Resource Broker, and one Entitle integration per Linux guest |
| **Share** | Publish every VM as one password-protected, time-limited `publish_set`; re-sharing revokes the previous link first |
| **Auto-delete** | Reap the whole environment when its timer expires, warning on a ladder as the deadline approaches |
| **Destroy** | Revoke the share link, reap the PAM artifacts, revoke the control node's agent, then delete the configuration and everything Skytap keeps inside it |

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
- **The control node install cannot fail the provision.** An environment that is up with no agent in
  it is running, billing and reapable; failing it would trade a fixable gap for a destroyed
  environment. The reason lands on the row next to an **Install control node** button that re-runs it, and
  never in the row's `error_message`, which means "this environment is broken".

---

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [The Skytap control node VM](skytap/control-node.md) | you need to know what the control node in a Skytap POV does, how it is built, and how to reach it |
| [Building a Skytap template](skytap/building-a-template.md) | you are building the Skytap template POVs are copied from |
| [The Skytap API calls the dashboard makes](skytap/api-calls.md) | you want to know exactly which Skytap v1 API calls the dashboard makes, and why each one |

## The template contract

Skytap's `bootstrap_injection` mechanism is **`metadata`**: the platform stores the payload
and the guest fetches it. There is no cloud-init datasource and nothing runs it for you, so
the control node VM in your template must carry a small runner.

> **The builder writes this for you.** [Building a template](skytap/building-a-template.md) generates
> the runner from the same marker constants the dashboard writes into the payload, installs
> it, and installs Docker beside it when the guest has no runtime. The shape below is what
> it generates, and what you need if you are baking a template by hand — **POV → Templates**
> will also just hand you the script to paste. That script is plain ASCII on purpose: the
> console you may have to type it into has no clipboard.

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
| Bootstrap injection | **metadata** — per-VM `user_data`, read by the guest at `http://169.254.169.254/skytap`. Used by [the control node VM](skytap/control-node.md) |
| Share link | yes — publish sets, with a password and an expiry. The expiry is **two** fields, `expiration_date` + `expiration_date_tz`; sending the date alone is a 400 that names the tz |
| Stored credentials | yes — `…/vms/{id}/credentials`. Used by [the Resource Broker install](gateway-and-broker.md#there-is-no-login-field-on-purpose), which is why the dashboard stores no Windows password for a POV |
| Verify | yes — one page of `/v2/templates`, surfaced as **Test connection** in Settings |
| Project scoping | yes — `/v2/projects/{id}/templates` and `/v2/projects/{id}/configurations` |
| Template authoring | yes — `POST /templates.json` with a `configuration_id`. There is no *edit a template* call on any lab platform, so authoring is always instantiate → change → bake. Used by [building a template](skytap/building-a-template.md) |
| Published services | yes — `…/interfaces/{id}/services`, a guest port NAT-ed to a public `ip:port`. Used **only** by a template build, for the length of one build |
| Add VMs to a live environment | yes — `PUT /configurations/{id}.json` with `template_id` + `vm_ids[]`. The only platform that can: a cloud POV's VM set is whatever its template service created. See [the quietest v1 trap](skytap/api-calls.md#the-merge-which-is-quieter-still) |

> **The two project paths are not yet confirmed against a live account.** Three things are
> assumed: that they return the same object shape as the account-wide collections, that
> they honour `count`/`offset` pagination, and that a project id the token cannot see
> answers `404` rather than an empty list. They are flagged the same way in the code, above
> the one place the paths are built. Nothing changes for an install that leaves the Project
> ID blank, and **Test connection** is the quickest way to check one that does not.

`bootstrap_injection` is one intent with more than one mechanism. Skytap hands data to the
guest and the guest fetches it; another platform might run a script on the guest instead.
The distinction matters because Skytap's metadata service works **only on VMs attached to
automatic networks**, and nothing executes `user_data` for you — there is no cloud-init
datasource, so the guest must fetch it. That is what
[the template contract](#the-template-contract) above specifies, and it is why the control node's agent
install refuses outright on a platform whose mechanism is anything other than `metadata`
rather than failing somewhere inside a job.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Skytap rejected the credentials … uses an API token, not your account password" | The account password was pasted into the token field | Use the API security token from the Skytap account page |
| The POV page says Skytap is not configured | No URL, username or token stored | Settings → Integrations → Skytap |
| Settings refuses to enable it with a 409 | This is an estate instance | Skytap is POV-only; see [POV profile](README.md) |
| No POV nav link at all | `pov_environments_enabled` is off, or the profile is `demo` | The flag must be ON **and** the profile must be `pov` — `GET /api/features` shows `install_profile` |
| "Skytap is still busy after N retries" | The account is genuinely rate-limited | Expected under heavy concurrent use; retry shortly. Running or suspending many VMs at once makes it more likely |
| An environment shows a **rate-limited** badge | Skytap set `rate_limited` on it | Operations against it will be slow until it clears |
| A POV reads **running** on the page but is suspended in Skytap | The reconcile sweep has not run since it changed | The row says how old the reading is — `confirmed 8m ago`. Press **Re-check platform** for the answer now. Both power buttons show while a reading is stale, so the one you need is never hidden |
| A POV is badged **gone from the platform** | Skytap answered 404 for it | Somebody deleted the environment outside this dashboard. The row is kept deliberately — it holds this POV's tenant references and reaping manifest — so use **Destroy** to close it out, which is idempotent on a 404 |
| A POV is badged gone but definitely exists | The Project ID changed under it, and the direct read also failed | The sweep confirms with a direct read before flagging, so this needs both to fail. Check the Project ID, then press **Re-check platform** — the flag clears by itself once the environment is visible again |
| Environments list is empty but the account has some | The token's user cannot see them, or they are outside the configured project | Check the user's access in Skytap, or clear the Project ID to widen the scope. **Test connection** says which of the two it is |
| **Test connection** says the account exposes no templates | The token authenticates but sees nothing | Check the user's access in Skytap. If a Project ID is set, clear it first to find out whether the project is the constraint |
| "Skytap has no project N (404)" on the POV page or in Test connection | The Project ID is stale, wrong, or belongs to an account this token cannot see | Correct or clear it in Settings → Integrations → Skytap. Blank lists everything the token can see |
| **Test connection** says the host could not be reached | DNS, a firewall or an outbound proxy | Not a credential problem. Check outbound HTTPS to the API URL from wherever the dashboard runs |
| VM counts show `—` | The collection read did not include the VM array | Expected. Open the environment for the measured count — a dash means "not measured", never zero |
| "publishing the share link failed … `Expiration date tz is invalid`" | Skytap did not accept the expiry's timezone. A publish set's expiry is **two** fields — `expiration_date` and `expiration_date_tz` — and the date alone is a 400 naming the tz | Fixed: the dashboard now sends both, the date in UTC and the zone as `UTC`. If it recurs, the message names the zone it sent — change `SHARE_EXPIRY_TZ` in `services/skytap_service.py` to one the account accepts |
| The Control node column reads **none** and the row names other VMs | The control node could not be resolved. Three different causes, and the message says which | If it says **no VM reports a Linux OS**: set the OS in the VMs tab's OS column for the guest that runs Docker. If it says **several VMs could be the control node**: press **name VM** beside the Control node column and name one. If it says **no VM is named X**: you typed a name this template does not use — clear it to auto-detect, or correct it |
| The control node resolved to the **wrong** guest | The template has one Linux VM and it is not the Docker host | Press **name VM** and name the right one. A typed name always wins over auto-detect |
| The Control node column reads **none** on a POV that used to work | A second Linux VM was added, so auto-detect became ambiguous | This refuses rather than silently moving the control node. Name the original with **name VM**. An *already enrolled* control node is never moved by this |
| The Control node column reads **enrolling** and never changes | Nothing executed the payload, or it executed and died | The control node VM has no metadata runner ([template contract](#the-template-contract)), **has no `docker`**, is on a manual network, or cannot reach the agent endpoint. Fix it and press **Install control node** to re-issue |
| "no agent enrolled within 14 minutes" | Same causes as above | **Read the whole message first.** When this dashboard built the template, the failure names it: *"built that template (…) and recorded 'docker: MISSING' on its control node"* means the runtime is the cause and the bootstrap's own install could not fix it either. Otherwise: the bootstrap is still on the VM — `user_data` is only cleared on a successful enrolment, so being able to read it proves the agent never enrolled. On the guest, `journalctl -u dashboard-bootstrap-runner -n1 -o cat` names the line that failed and `docker logs dashboard-agent`, if it ever started, names the rest |
| "nothing is listening at /var/run/docker.sock" | The runtime is installed and its API is not running | Almost always Podman with `podman.socket` not started — the `docker` command answers while nothing serves the Engine API, and the agent uses only the API. `systemctl enable --now podman.socket` on the control node VM, then press **Install control node**. The install does this itself; seeing this means it did not take |
| "/var/run/docker.sock … is a DIRECTORY … NOT EMPTY" | A `docker run -v` created one on a host whose daemon was down, and something has since been written into it | The install clears an **empty** one itself — `rmdir`, never `rm -rf` — so seeing this means there is content in it. Look at what is inside, remove it, make sure the daemon is up, and press **Install control node** again |
| "this dashboard does not know its own public URL" | No pinned audience and no `public_base_url` | An agent inside a customer network needs an address. Set it in Settings → Integrations → Remote Agents |
| "the agent endpoint is `http://…`" | The audience is plaintext | The agent refuses to sign over plaintext, so the control node would never enrol. Terminate TLS and correct **Public base URL** |
| A build fails with "does not satisfy the template contract" | The base template has no control node VM, or its control node is on a manual network | Read the contract report on the build row. Press **Discard** to reap the scratch environment, fix the base template, and build again |
| The **Runner** column reads `failed` and names a firewall | The SSH install could not reach the published port | It dials a NAT-ed high port, not `cloud.skytap.com`. Either open that egress or clear **Install the metadata runner and Docker** and paste the install script onto the control node VM by hand — the template itself is fine |
| The **Runner** column reads `failed` with "refused all N stored credentials" | Every login stored against the control node VM was rejected by its sshd | The detail names the usernames it tried. Correct the credential on that VM in Skytap and build again — SSH answered, so this is the login and not the route |
| The **Runner** column reads `failed` with "the install exited …" | A login worked but the script did not finish | It installs as root, so check that the credential you left is an administrator with `sudo`. The detail carries the state read back off the VM afterwards, so it says which half landed. The template still bakes; paste the script in by hand |
| The **Runner** detail says `docker at boot: disabled` | The daemon is up but its unit is not enabled | The build refuses this rather than baking it: a template is booted for every POV, so a daemon that is only running *now* gives each one a control node with no runtime. `systemctl enable docker` on the control node VM and build again — `dnf install docker-ce` leaves it disabled on the RHEL family |
| The **Runner** detail says `docker: MISSING` | The runner landed and neither runtime did | No route from the **guest** to `download.docker.com` *and* no `podman` in its own repository (see [prerequisites](#prerequisites)) — the detail names what it tried. The template is still usable: the POV's first **Install control node** press runs the same install again, and a POV whose control node still will not enrol now says this in the job's own error. Install a runtime by hand and re-bake to fix it for every POV; the runner on the VM is already correct |
| The **Runner** column reads `skipped` | You cleared the checkbox, or no control node VM was resolved | Not a failure. Paste the install script from **POV → Templates** onto the control node VM |
| A build row shows a **build env** that is still running | The build failed, or you asked to keep it | It is billing. Press **Discard** to reap it. A failed build keeps the id on purpose, so this always works |
| Discard says the environment could not be deleted | Skytap refused the delete | The row stays visible with its id rather than being marked discarded — marking it would hide a running environment. Clear the cause and press Discard again |
| A POV built from a new template still reads **enrolling** | The runner is present but the control node cannot reach the agent endpoint | The runner is not the only requirement — see [the control node VM](skytap/control-node.md). Check Docker and the automatic network, then press **Install control node** |
| The control node was online and 401s after a re-run | Its state volume survived a control node rebuild | The generated bootstrap removes it. A hand-run `docker run` that skipped that line leaves the old identity in place — remove the `dashboard_agent_state` volume and press **Rebuild control node** |
