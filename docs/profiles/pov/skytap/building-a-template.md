# Building a Skytap template

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are building the Skytap template POVs are copied from.

Part of [Skytap](../skytap.md).

**POV → Templates** (`/pov/templates`), admin only — the **Build a template** and
**Build history** tabs, which show while the page's platform toggle is on Skytap. A cloud has
no template object to bake, so with the toggle on a cloud those tabs give way to
**Templates**, where the cloud template is defined instead.

A POV *is* a template instantiated whole, so until this page existed the whole feature was
downstream of a catalogue that could only be authored by hand in Skytap's own console. That
matters most where the existing catalogue was built for an **on-premises** approach: a
template carrying a full product stack inside the environment is the wrong shape for the
SaaS-first POV this dashboard runs, where PRA, Password Safe and Entitle are *tenants*
reached from outside and the environment needs only the customer-like VMs plus a control node.

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
[lifecycle](../skytap.md#the-lifecycle) section gives: an environment that exists on the platform and
not in this database is the one failure nothing can clean up — and a scratch one bills
until somebody notices.

## What the build does about the runner and Docker

This is the point of the page. [The template contract](../skytap.md#the-template-contract) below
requires the control node VM to carry a metadata runner, because Skytap hands `user_data` to the
guest and **nothing executes it**. Before this, that runner existed only as an example in
this document for somebody to copy into an image by hand — and it is the single most common
way a POV fails.

**And a runner with no container runtime is the same failure one line later.** The payload
ends in `docker run`; everything above it is a `mkdir`, a heredoc and two `|| true`s. A
control node VM with a perfect runner and no Docker therefore reads the payload, dies on that
line, and re-reads it every twenty seconds forever — while the POV page says `enrolling`
and names nothing, exactly as if no runner existed. Base templates that carry neither are
common: a stock AlmaLinux 8 guest has no `docker`, no `podman`, and no socket.

The build installs both. It publishes SSH on the control node VM, reads the login from Skytap's
own [stored credentials](../skytap.md#capabilities), installs the runner and its systemd unit, then a
container runtime if — and only if — `docker` does not already resolve, and revokes the
published service in a `finally`. If the VM carries more than one credential it tries
each in turn — the first the guest accepts wins, and the **Runner** detail names it — so a
box holding a stale login beside a good one builds rather than refusing.

**And since 2026-09-16 the injected bootstrap installs a runtime too**, using the same
block, so a POV made from a template that baked without one repairs itself on the first
**Install control node** press instead of sitting at `enrolling`. That does not make the bake redundant:
an install at bake time lands on every POV made from the template, before anyone is waiting
fourteen minutes for it.

**Docker CE first, the distro's Podman second.** Docker CE comes from
`download.docker.com` and is what the agent's use of the Engine API is tested against, so
it leads. But a lab guest routinely reaches its own distro mirror and no external CDN at
all — a Skytap AlmaLinux 8 control node resolves `appstream` and `baseos` perfectly and cannot
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

Published services are otherwise [deliberately unused](../skytap.md#what-is-deliberately-not-used),
because a published address changes per environment and per power cycle. A build is the one
case where that does not matter: the address is used once and revoked in the same job.

## The contract report

**Verify** checks a template — including one somebody else authored — against the contract
without building anything. Each check is reported on its own, never collapsed into one
badge, because "no control node" and "the control node is on a manual network" have different fixes:

| Check | `fail` means |
|---|---|
| Control node | No control node could be resolved, so a POV from this template has nowhere to run its agent: either the name you typed matches nothing, or nothing distinguishes the template's Linux guests. The report names the VMs it *did* find. Note a template read has no POV, so the per-VM OS override cannot apply here — a template whose only Linux guest reports no OS fails this check while the POV built from it resolves fine once you set the OS on its VMs tab |
| Control node network | The control node is on a manual network. The metadata service answers **only** on automatic networks, so the guest would receive no bootstrap at all — which looks exactly like a missing runner |
| Resource Broker host | Never fails. No Windows guest is a **warning**: a PRA-and-Entitle POV does not need one |
| Workload VMs | Never fails. A control-node-only template is a **warning** — a POV built from it has nothing to demonstrate |

Only a `fail` stops a build. A warning is a statement about what a template is *for*, not a
defect in it.

Whether the runner is actually installed cannot be read from the platform at all — it is a
file inside the guest. Neither can whether Docker is: `command -v docker` is not a thing an
API answers. So a template carrying **neither** still reports `pass` on all four checks
above, which is correct — those checks are about what the platform can see — and is also
the reason a build installs both rather than trusting a green report. A Verify says what it
cannot know rather than guessing.

## The scratch environment bills

Three guards, because a running environment costs money whether or not anyone is watching:

1. **`suspend_on_idle` is set on it before the power-on.** This is the only one that
   survives the worker being killed mid-build, which is the failure nobody notices.
2. The job reaps it after a successful bake, unless you asked to keep it. It deliberately
   does **not** reap on failure — a build that broke is the one whose environment you may
   want to look at.
3. **Discard** on any build row reaps it by hand. A failed build keeps its environment id
   precisely so this can work — and a failed *reap* does not mark the row discarded, because
   that would hide an environment that is still running.
