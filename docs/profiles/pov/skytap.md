# Skytap

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are pointing a POV instance at Skytap and want to know what it will and will not do with the account.

The first **lab platform** for POV environments. A POV is a Skytap *template* instantiated
whole; the dashboard creates those environments and wires their VMs into that POV's PRA,
Password Safe and Entitle tenants.

Available on a **POV instance only** — see [pov-instance.md](README.md). On an estate
instance the integration is masked off and Settings refuses to enable it.

> **What the dashboard does with an environment.** Create it from a template, power it on
> and off, and destroy it. Enrol an **agent inside it** — see
> [The broker VM](#the-broker-vm) and the [template contract](#the-template-contract).
> Install a **BeyondTrust Gateway** on that broker, registered into the POV's own PRA
> tenant ([the POV Gateway](gateway-and-broker.md#the-pov-gateway)), and a **Password Safe
> Resource Broker** on a Windows VM beside it
> ([the Resource Broker](gateway-and-broker.md#the-resource-broker)).
> Then, per VM: **one PRA jump item** through that Gateway, **Password Safe onboarding**
> through that Resource Broker, and **Entitle registration** for the Linux guests
> ([wiring the VMs](wiring.md#wiring-the-vms-into-pra-password-safe-and-entitle)).
> Publish a **customer-facing share link** for the whole environment
> ([the share link](customer-access.md#the-customer-facing-share-link)) — Skytap calls it a
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
| Network, for template builds only | Outbound **TCP to an arbitrary high port** at a Skytap NAT address. Only the automatic runner install needs this — see [building a template](#building-a-template). An egress rule that allows 443 to the API host and nothing else will block it, and the builder says so rather than reporting a generic connection failure |
| Network **from the guest** | A route to a container runtime, unless the base template already carries one: either outbound HTTPS from the **broker VM** to `download.docker.com`, **or** the guest's own distro repository carrying `podman` + `podman-docker`, which is the fallback and is usually already reachable. This is the guest's route out, not the dashboard's — the only prerequisite on this list that nothing on this side can test for you. A build that cannot reach either still bakes, with the reason in the **Runner** column, and the POV's first **Broker** press tries again |

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
| Project ID | Optional. Templates and environments are **listed from** this project, and new environments are **created inside** it. Blank lists everything the token can see |

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
POV page shows when each row was last confirmed rather than implying it is live.

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
| **Build a template** | Instantiate a base template into a scratch environment, check it against [the template contract](#the-template-contract), install the metadata runner on its broker VM, save the environment back as a **new template**, then reap the scratch environment. See [building a template](#building-a-template) |
| **Create** | Instantiate a template, set the idle timer, power on, wait for it to settle, read the VMs back, then enrol [the broker agent](#the-broker-vm) |
| **Start / Suspend** | A runstate change, then a poll until it settles |
| **Broker** | Re-issue the enrolment code and re-write the bootstrap. The remedy for every way the first attempt can fail |
| **Gateway** | Start a BeyondTrust Gateway container on the broker VM, registered into this POV's PRA tenant |
| **Resource Broker** | Run the staged Password Safe installer on a Windows VM, over WinRM from the broker |
| **Wire** | One PRA jump item per VM through this POV's Gateway, one Password Safe managed system through its Resource Broker, and one Entitle integration per Linux guest |
| **Share** | Publish every VM as one password-protected, time-limited `publish_set`; re-sharing revokes the previous link first |
| **Auto-delete** | Reap the whole environment when its timer expires, warning on a ladder as the deadline approaches |
| **Destroy** | Revoke the share link, reap the PAM artifacts, revoke the broker agent, then delete the configuration and everything Skytap keeps inside it |

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
the environment: the **broker**. It runs the [remote agent](../../remote-agents.md), and the
agent dials out — because a POV lives on a lab platform's private network and this
dashboard has no route into it. The Gateway install, the Resource Broker install and the
per-VM wire-up all wait on this one thing, so it is worth setting up properly once.

Which VM? **Leave Broker VM name blank and the POV works it out: the one Linux VM in the
template.** A broker is a Docker host by construction, so on the usual shape — one Linux
guest beside some Windows ones — its OS identifies it and its name does not have to. That
matters because templates come from wherever the SE got them, and a guest called
`BtPocLin01` was never going to match a name this dashboard guessed.

The full order it resolves in, and why each step exists:

| | |
|---|---|
| 1. **The name you typed**, if you typed one | Matched exactly and case-insensitively, and **refused** if nothing matches rather than falling through to a guess. A name you wrote down is an instruction: "contains broker" also matches a customer VM called `password-broker`, and the cost of that mistake is an agent installed on a machine nobody expected |
| 2. **The VM already brokered** | An enrolled broker is not *guessed* away from. The agent's identity lives on that guest, so re-resolving onto a different VM leaves a container polling from a machine nothing points at. Below the typed name, deliberately — otherwise **name VM** would be silently ignored on the POVs that most need it |
| 3. **The only Linux VM** | Uniqueness, never position. "The first Linux VM" is not a decision a position in a list can make, but "the only one" is a fact about the template. VMs already named as the [Entitle agent host](wiring.md) or the [Resource Broker host](gateway-and-broker.md#the-resource-broker) are excluded — those are deliberately different machines |
| 4. **A VM called `broker`**, if several Linux guests remain | The old convention, kept as a tie-break |
| 5. Otherwise it **refuses, naming the candidates** | So you can pick one. The Broker column reads `none` with the reason beside it |

**A blank OS is not Linux.** Skytap does not report a guest OS as a field — the dashboard
infers it from whatever text the platform hands back, and it answers "unknown" rather than
guess wrong, because a confident wrong answer sends a Windows VM down an SSH path. A guest
whose OS reads `—` on the POV's **VMs tab** is therefore skipped by step 3, by the wire-up,
and by every other POV feature. **Set it yourself in the OS column on that tab**; it is
stored separately from the platform's own answer, so it survives every later refresh. That
one control is the remedy for most "this VM is being ignored" questions, not just for the
broker.

You can also correct the name after the fact: **name VM** beside the Broker column sets it
on a POV that already exists, which is what answers a step-5 refusal.

A POV that also runs a Resource Broker needs a **second** special VM — a Windows Server
2019 or 2022 x64 guest with WinRM enabled, on the same automatic network. See
[the Resource Broker](gateway-and-broker.md#the-resource-broker); the rest of this section is
about the Linux broker.

That VM needs three things:

| | |
|---|---|
| **Docker**, enabled at boot | The agent is a container, and so is the Gateway it later runs beside itself. **Enabled** matters as much as installed: a POV boots the template, so a daemon that is merely running when the template was baked is a broker with no runtime on every POV. The build installs Docker CE if the base template carries no runtime, enables it, and leaves an existing one alone — Podman works if `docker` resolves to it, though what the builder bakes is Docker, because the agent drives the Engine API over the socket rather than a CLI |
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
- **And the container joins that socket's group**, resolved on the guest with `stat` at
  bootstrap time. Mounting the socket is not the same as being able to open it: it is
  typically `srw-rw---- root docker`, the container runs as uid 10001 with no supplementary
  groups, and the result is `EACCES` on a socket that is sitting right there. Every POV
  Gateway install failed on this, and the agent's refusal — "the sibling runner needs it
  mounted" — pointed at the one thing that was already correct. The GID is read on the VM
  because the dashboard never chose it; a guest with no socket gets no flag rather than an
  empty argument.

### The ordering that matters

The payload is injected **after** the power-on, never before. An enrolment code lives
fifteen minutes and a first boot is not bounded — a Windows template pulling updates can
eat all of it. Injecting first hands the guest a code that expired while it was starting,
and the symptom is an agent stuck at `enrolling` with no request ever reaching the
dashboard.

Once the agent enrols, the dashboard **clears `user_data`**. It is readable by anyone who
can read the environment in Skytap, and clearing it also stops a reboot re-running a
bootstrap whose code is spent.

## Building a template

**POV → Templates** (`/pov/templates`), admin only.

A POV *is* a template instantiated whole, so until this page existed the whole feature was
downstream of a catalogue that could only be authored by hand in Skytap's own console. That
matters most where the existing catalogue was built for an **on-premises** approach: a
template carrying a full product stack inside the environment is the wrong shape for the
SaaS-first POV this dashboard runs, where PRA, Password Safe and Entitle are *tenants*
reached from outside and the environment needs only the customer-like VMs plus a broker.

A template is immutable — there is no *edit a template* call — so authoring is a bake:

```
clone a base template → power on → check the contract → install the runner → shut the
environment down → save as a template → reap the scratch environment
```

**The shutdown is a stage, not a courtesy.** Skytap will not bake a multi-VM environment
whose VMs are running: it answers `409 {"error":"The machine was busy. Try again later."}`,
and later never arrives — it fails the same way for as long as they are up. The job stops
the environment gracefully and waits; a guest that ignores the shutdown gets `halted`,
which is Skytap's documented force-off and still settles on `stopped`.

The scratch environment is created, used and reaped inside one job. Its id is committed the
moment Skytap returns it, before anything else can fail, for the reason the whole
[lifecycle](#the-lifecycle) section gives: an environment that exists on the platform and
not in this database is the one failure nothing can clean up — and a scratch one bills
until somebody notices.

### What the build does about the runner and Docker

This is the point of the page. [The template contract](#the-template-contract) below
requires the broker VM to carry a metadata runner, because Skytap hands `user_data` to the
guest and **nothing executes it**. Before this, that runner existed only as an example in
this document for somebody to copy into an image by hand — and it is the single most common
way a POV fails.

**And a runner with no container runtime is the same failure one line later.** The payload
ends in `docker run`; everything above it is a `mkdir`, a heredoc and two `|| true`s. A
broker VM with a perfect runner and no Docker therefore reads the payload, dies on that
line, and re-reads it every twenty seconds forever — while the POV page says `enrolling`
and names nothing, exactly as if no runner existed. Base templates that carry neither are
common: a stock AlmaLinux 8 guest has no `docker`, no `podman`, and no socket.

The build installs both. It publishes SSH on the broker VM, reads the login from Skytap's
own [stored credentials](#capabilities), installs the runner and its systemd unit, then a
container runtime if — and only if — `docker` does not already resolve, and revokes the
published service in a `finally`. If the VM carries more than one credential it tries
each in turn — the first the guest accepts wins, and the **Runner** detail names it — so a
box holding a stale login beside a good one builds rather than refusing.

**And since 2026-09-16 the injected bootstrap installs a runtime too**, using the same
block, so a POV made from a template that baked without one repairs itself on the first
**Broker** press instead of sitting at `enrolling`. That does not make the bake redundant:
an install at bake time lands on every POV made from the template, before anyone is waiting
fourteen minutes for it.

**Docker CE first, the distro's Podman second.** Docker CE comes from
`download.docker.com` and is what the agent's use of the Engine API is tested against, so
it leads. But a lab guest routinely reaches its own distro mirror and no external CDN at
all — a Skytap AlmaLinux 8 broker resolves `appstream` and `baseos` perfectly and cannot
reach Docker's — so every step of that install is non-fatal, and `podman` +
`podman-docker` from the guest's own repository is the fallback. Podman serves the same
Engine API on the same socket path, so the agent cannot tell the difference. **Podman on a
lab VM is a supported outcome here, not a degraded one.**

> `podman-docker` is not optional garnish. Podman alone gives no `docker` command and no
> `/var/run/docker.sock`, and the agent speaks the Engine API over that path directly —
> it never runs the CLI. Nor does installing the package *start* anything: the install
> **enables and starts** `podman.socket` itself, because a guest where the shim answers
> and nothing is listening enrols, goes green, and then fails every Gateway and
> Config-Management job.
>
> Both verbs matter, and they fail differently. `systemctl enable podman.socket` writes a
> symlink and starts nothing, so an enabled-only guest has no API **until it is
> rebooted**; starting without enabling is the mirror image, and is the trap `docker-ce`
> sets on the RHEL family. Where socket activation produces no listening socket the
> install falls back to `podman.service` — the API service running persistently, which is
> what an operator reaches for by hand.

The **boot gate asks Podman the same question**. It used to skip a podman-docker guest
entirely, because `systemctl cat docker.service` is false there and there is no docker unit
to enable — so a template could bake with a socket that answered only because the install
had just started it, and every POV from it came up with a working `docker` command and
nothing listening. Either `podman.socket` or `podman.service` being enabled satisfies it.

Three more things are worth knowing before you rely on it:

- **It reaches a NAT-ed high port, not the API host.** See the prerequisites table above.
  If your egress only allows HTTPS to `cloud.skytap.com`, clear **Install the metadata
  runner and Docker** and use the install script the page offers instead.
- **There is no host key to pin.** The VM was created minutes ago by the same API call that
  said where to reach it, and it is destroyed at the end of the job. That trade is
  acceptable for one connection to a machine with a lifetime in minutes, and it is exactly
  why this path is not reused for anything longer-lived — POV wiring reaches VMs through a
  Gateway inside the environment.
- **A failed install does not fail the build.** A template that bakes without the runner is
  still a usable template; you paste the script in, which is what you do today. The reason
  lands in the **Runner** column, never in the build's error — which means "this build is
  broken".
- **It gates on "enabled at boot", not on "running now".** `dnf install docker-ce` leaves
  the unit **disabled** on the RHEL family. A template is baked and then booted, for every
  POV, every time — so a guest whose daemon is up because somebody typed `systemctl start
  docker` an hour ago bakes into a template that comes up with no runtime and sits at
  `enrolling`. The install refuses that, and the **Runner** detail reports the boot state
  beside the version. A guest with no `docker.service` at all — the Podman shim — is not
  failed for having no unit to enable.
- **The runner goes on first, and the column says which half landed.** The runner is local
  and cannot really fail; the Docker install reaches a package repository from inside the
  guest and can. Doing the cheap half first means a guest with no route to Docker's repo
  ends up one `dnf install` from correct rather than needing the whole script again — and
  the **Runner** detail carries the state read back off the VM (`docker: MISSING`, or the
  version it found), not just an exit status.

Published services are otherwise [deliberately unused](#what-is-deliberately-not-used),
because a published address changes per environment and per power cycle. A build is the one
case where that does not matter: the address is used once and revoked in the same job.

### The contract report

**Verify** checks a template — including one somebody else authored — against the contract
without building anything. Each check is reported on its own, never collapsed into one
badge, because "no broker VM" and "the broker is on a manual network" have different fixes:

| Check | `fail` means |
|---|---|
| Broker VM | No broker VM could be resolved, so a POV from this template has nowhere to run its agent: either the name you typed matches nothing, or nothing distinguishes the template's Linux guests. The report names the VMs it *did* find. Note a template read has no POV, so the per-VM OS override cannot apply here — a template whose only Linux guest reports no OS fails this check while the POV built from it resolves fine once you set the OS on its VMs tab |
| Broker network | The broker is on a manual network. The metadata service answers **only** on automatic networks, so the guest would receive no bootstrap at all — which looks exactly like a missing runner |
| Resource Broker host | Never fails. No Windows guest is a **warning**: a PRA-and-Entitle POV does not need one |
| Workload VMs | Never fails. A broker-only template is a **warning** — a POV built from it has nothing to demonstrate |

Only a `fail` stops a build. A warning is a statement about what a template is *for*, not a
defect in it.

Whether the runner is actually installed cannot be read from the platform at all — it is a
file inside the guest. Neither can whether Docker is: `command -v docker` is not a thing an
API answers. So a template carrying **neither** still reports `pass` on all four checks
above, which is correct — those checks are about what the platform can see — and is also
the reason a build installs both rather than trusting a green report. A Verify says what it
cannot know rather than guessing.

### The scratch environment bills

Three guards, because a running environment costs money whether or not anyone is watching:

1. **`suspend_on_idle` is set on it before the power-on.** This is the only one that
   survives the worker being killed mid-build, which is the failure nobody notices.
2. The job reaps it after a successful bake, unless you asked to keep it. It deliberately
   does **not** reap on failure — a build that broke is the one whose environment you may
   want to look at.
3. **Discard** on any build row reaps it by hand. A failed build keeps its environment id
   precisely so this can work — and a failed *reap* does not mark the row discarded, because
   that would hide an environment that is still running.

## The template contract

Skytap's `bootstrap_injection` mechanism is **`metadata`**: the platform stores the payload
and the guest fetches it. There is no cloud-init datasource and nothing runs it for you, so
the broker VM in your template must carry a small runner.

> **The builder writes this for you.** [Building a template](#building-a-template) generates
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

## The calls that are v1

Most of what the dashboard sends is the v2 API. **Which calls have a v2 form is decided
per verb, not per path**, and the paths that look most settled are the ones that are not:
`/v2/configurations/{id}` serves `GET` and `PUT` and **not** `DELETE`, and
`/v2/templates/{id}` serves only `GET`. Every one of these was learned from a live account
answering `404 {"error":"Not Found"}` — never from the documentation, which reads as though
both objects support the full set.

| Operation | Call | Why not v2 |
|---|---|---|
| Create an environment from a template | `POST /configurations.json` — `template_id`, optionally `project_id` and `name` | no `POST` on the v2 collection |
| Create a template from an environment | `POST /templates.json` — `configuration_id`, `name` | no `POST` on the v2 collection |
| Copy VMs into an existing environment | `PUT /configurations/{id}.json` — `template_id`, `vm_ids[]` | the v2 `PUT` answers **200 and does nothing** — see below |
| **Delete an environment** | `DELETE /configurations/{id}.json` | `DELETE /v2/configurations/{id}` 404s for **every** id, including a running one |
| **Delete a template** | `DELETE /templates/{id}.json` | `/v2/templates/{id}` serves only `GET` |
| Set a template's description | `PUT /templates/{id}.json` — `description` | same; the v2 `PUT` 404s |

Everything **nested** under those paths is v2 and works: `…/vms/{id}/user_data.json`,
`…/publish_sets`, `…/vms/{id}/interfaces/{id}/services`, `…/vms/{id}/credentials`. So is
`PUT /v2/configurations/{id}` for a name, a runstate or an idle timer.

A 404 on one of these means **the endpoint, not the id** — and that is the trap worth
remembering, because the message reads exactly like "the id you sent does not exist". The
first live template build was spent inspecting a base template id that was perfectly good.

### A 404 on a DELETE is not proof the thing is gone

The delete rows above cost six environments. `delete_environment` sent the v2 path, Skytap
404ed it, and the adapter read that 404 as "somebody already deleted it" — so every POV
destroy reported a clean success, marked the row `destroyed`, wrote *"The infrastructure is
gone"* on the archive page and deleted the VM rows that were the only record of what had
been inside. The environments went on running, and on billing, invisible to every sweep:
`pov_reconcile` only asks about rows that are **not** destroyed.

The deletes are still idempotent — a teardown that fails because the thing is already gone
leaves a row nobody can ever clean up — but the evidence for "already gone" is now a
**direct read of the object**, never the status code of the delete
(`skytap_service._confirm_gone`). A 404 on the delete with a readable object raises, which
puts the POV row in `failed` with a re-runnable Destroy rather than in `destroyed` with an
orphan. It is the same refusal to guess that makes `pov_reconcile` confirm a missing
environment with a direct read before flagging it, and it is worth applying to any delete
added here later.

A read that fails with something else — a 423, a 500, a network blip — re-raises rather
than being guessed either way.

### The merge, which is quieter still

The VM copy in the table above is the one with no failure signal at all. It is v1 and a
**PUT**, at a path the v2 API also serves.

Skytap's v2 reference says plainly that "VMs are created indirectly, either by creating an
environment from a template or by merging a template into an existing environment", and
documents no endpoint for the second half. The v1 merge is that endpoint. It also accepts
`merge_configuration` in place of `template_id`, to merge from another environment; the
dashboard does not use that form.

**`PUT /v2/configurations/{id}` exists, is what `update_environment` uses for the name and
the idle timer, and answers a `template_id` with `200` and the environment unchanged.** No
error, no VMs, nothing to investigate — strictly worse than the 404 the creates give,
because a 404 at least says something happened. `skytap_service.add_vms` is the only caller
of the v1 path and `tests/test_pov_add_vms.py` pins it against the code, not the docstring.

Two behaviours worth knowing:

* **`vm_ids` is optional in the API and required by this dashboard.** Omitted, the whole
  template is merged — against a live POV that silently doubles the environment.
* **`409` has one documented cause**: a running IBM Power VM among the ones being copied,
  which cannot be suspended for the copy. Nothing in the word "conflict" says so, so
  `add_vms` names it.
* The copies arrive **stopped** even when the rest of the environment is running, which is
  why the `pov_env_add_vms` job powers the environment on afterwards unless told not to.

#### If you add VMs in Skytap's own UI instead

The dashboard will not notice. `pov_reconcile` is deliberately **one collection read per
pass** — that is the property that lets it run over every POV cheaply — so it refreshes a
runstate, a rate-limit flag and an idle timer, and never a VM list. Adding a per-environment
VM read to it would turn one call into one per POV.

`pov_environment_vms` is refreshed by three things: a provision, a power action, and the
add-VMs job. **Broker** does it too, via `run_env_broker`, which is the shortest way to pick
up a guest added outside the dashboard — and it is what you would press for a new guest
anyway, since a Config-Management grant is written at enrolment.

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
| Share link | yes — publish sets, with a password and an expiry. The expiry is **two** fields, `expiration_date` + `expiration_date_tz`; sending the date alone is a 400 that names the tz |
| Stored credentials | yes — `…/vms/{id}/credentials`. Used by [the Resource Broker install](gateway-and-broker.md#there-is-no-login-field-on-purpose), which is why the dashboard stores no Windows password for a POV |
| Verify | yes — one page of `/v2/templates`, surfaced as **Test connection** in Settings |
| Project scoping | yes — `/v2/projects/{id}/templates` and `/v2/projects/{id}/configurations` |
| Template authoring | yes — `POST /templates.json` with a `configuration_id`. There is no *edit a template* call on any lab platform, so authoring is always instantiate → change → bake. Used by [building a template](#building-a-template) |
| Published services | yes — `…/interfaces/{id}/services`, a guest port NAT-ed to a public `ip:port`. Used **only** by a template build, for the length of one build |
| Add VMs to a live environment | yes — `PUT /configurations/{id}.json` with `template_id` + `vm_ids[]`. The only platform that can: a cloud POV's VM set is whatever its template service created. See [the quietest v1 trap](#the-merge-which-is-quieter-still) |

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
[the template contract](#the-template-contract) above specifies, and it is why the broker
install refuses outright on a platform whose mechanism is anything other than `metadata`
rather than failing somewhere inside a job.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Skytap rejected the credentials … uses an API token, not your account password" | The account password was pasted into the token field | Use the API security token from the Skytap account page |
| The POV page says Skytap is not configured | No URL, username or token stored | Settings → Integrations → Skytap |
| Settings refuses to enable it with a 409 | This is an estate instance | Skytap is POV-only; see [pov-instance.md](README.md) |
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
| The Broker column reads **none** and the row names other VMs | The broker could not be resolved. Three different causes, and the message says which | If it says **no VM reports a Linux OS**: set the OS in the VMs tab's OS column for the guest that runs Docker. If it says **several VMs could be the broker**: press **name VM** beside the Broker column and name one. If it says **no VM is named X**: you typed a name this template does not use — clear it to auto-detect, or correct it |
| The broker resolved to the **wrong** guest | The template has one Linux VM and it is not the Docker host | Press **name VM** and name the right one. A typed name always wins over auto-detect |
| The Broker column reads **none** on a POV that used to work | A second Linux VM was added, so auto-detect became ambiguous | This refuses rather than silently moving the broker. Name the original with **name VM**. An *already enrolled* broker is never moved by this |
| The Broker column reads **enrolling** and never changes | Nothing executed the payload, or it executed and died | The broker VM has no metadata runner ([template contract](#the-template-contract)), **has no `docker`**, is on a manual network, or cannot reach the agent endpoint. Fix it and press **Broker** to re-issue |
| "no agent enrolled within 14 minutes" | Same causes as above | **Read the whole message first.** When this dashboard built the template, the failure names it: *"built that template (…) and recorded 'docker: MISSING' on its broker VM"* means the runtime is the cause and the bootstrap's own install could not fix it either. Otherwise: the bootstrap is still on the VM — `user_data` is only cleared on a successful enrolment, so being able to read it proves the agent never enrolled. On the guest, `journalctl -u dashboard-bootstrap-runner -n1 -o cat` names the line that failed and `docker logs dashboard-agent`, if it ever started, names the rest |
| "nothing is listening at /var/run/docker.sock" | The runtime is installed and its API is not running | Almost always Podman with `podman.socket` not started — the `docker` command answers while nothing serves the Engine API, and the agent uses only the API. `systemctl enable --now podman.socket` on the broker VM, then press **Broker**. The install does this itself; seeing this means it did not take |
| "/var/run/docker.sock … is a DIRECTORY … NOT EMPTY" | A `docker run -v` created one on a host whose daemon was down, and something has since been written into it | The install clears an **empty** one itself — `rmdir`, never `rm -rf` — so seeing this means there is content in it. Look at what is inside, remove it, make sure the daemon is up, and press **Broker** again |
| "this dashboard does not know its own public URL" | No pinned audience and no `public_base_url` | An agent inside a customer network needs an address. Set it in Settings → Integrations → Remote Agents |
| "the agent endpoint is `http://…`" | The audience is plaintext | The agent refuses to sign over plaintext, so the broker would never enrol. Terminate TLS and correct **Public base URL** |
| A build fails with "does not satisfy the template contract" | The base template has no broker VM, or its broker is on a manual network | Read the contract report on the build row. Press **Discard** to reap the scratch environment, fix the base template, and build again |
| The **Runner** column reads `failed` and names a firewall | The SSH install could not reach the published port | It dials a NAT-ed high port, not `cloud.skytap.com`. Either open that egress or clear **Install the metadata runner and Docker** and paste the install script onto the broker VM by hand — the template itself is fine |
| The **Runner** column reads `failed` with "refused all N stored credentials" | Every login stored against the broker VM was rejected by its sshd | The detail names the usernames it tried. Correct the credential on that VM in Skytap and build again — SSH answered, so this is the login and not the route |
| The **Runner** column reads `failed` with "the install exited …" | A login worked but the script did not finish | It installs as root, so check that the credential you left is an administrator with `sudo`. The detail carries the state read back off the VM afterwards, so it says which half landed. The template still bakes; paste the script in by hand |
| The **Runner** detail says `docker at boot: disabled` | The daemon is up but its unit is not enabled | The build refuses this rather than baking it: a template is booted for every POV, so a daemon that is only running *now* gives each one a broker with no runtime. `systemctl enable docker` on the broker VM and build again — `dnf install docker-ce` leaves it disabled on the RHEL family |
| The **Runner** detail says `docker: MISSING` | The runner landed and neither runtime did | No route from the **guest** to `download.docker.com` *and* no `podman` in its own repository (see [prerequisites](#prerequisites)) — the detail names what it tried. The template is still usable: the POV's first **Broker** press runs the same install again, and a POV whose broker still will not enrol now says this in the job's own error. Install a runtime by hand and re-bake to fix it for every POV; the runner on the VM is already correct |
| The **Runner** column reads `skipped` | You cleared the checkbox, or no broker VM was resolved | Not a failure. Paste the install script from **POV → Templates** onto the broker VM |
| A build row shows a **build env** that is still running | The build failed, or you asked to keep it | It is billing. Press **Discard** to reap it. A failed build keeps the id on purpose, so this always works |
| Discard says the environment could not be deleted | Skytap refused the delete | The row stays visible with its id rather than being marked discarded — marking it would hide a running environment. Clear the cause and press Discard again |
| A POV built from a new template still reads **enrolling** | The runner is present but the broker cannot reach the agent endpoint | The runner is not the only requirement — see [the broker VM](#the-broker-vm). Check Docker and the automatic network, then press **Broker** |
| The broker was online and 401s after a re-run | Its state volume survived a re-broker | The generated bootstrap removes it. A hand-run `docker run` that skipped that line leaves the old identity in place — remove the `dashboard_agent_state` volume and press **Broker** |
