# The Skytap control node VM

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you need to know what the control node in a Skytap POV does, how it is built, and how to reach it.

Part of [Skytap](../skytap.md).

Everything the dashboard does *inside* a POV after it is running goes through one VM in
the environment: the **control node**. It runs the [remote agent](../../../platform/spiffe/remote-agents.md), and the
agent dials out — because a POV lives on a lab platform's private network and this
dashboard has no route into it. The Gateway install, the Resource Broker install and the
per-VM wire-up all wait on this one thing, so it is worth setting up properly once.

Which VM? **Leave Control node VM name blank and the POV works it out: the one Linux VM in the
template.** A control node is a Docker host by construction, so on the usual shape — one Linux
guest beside some Windows ones — its OS identifies it and its name does not have to. That
matters because templates come from wherever the SE got them, and a guest called
`BtPocLin01` was never going to match a name this dashboard guessed.

The full order it resolves in, and why each step exists:

| | |
|---|---|
| 1. **The name you typed**, if you typed one | Matched exactly and case-insensitively, and **refused** if nothing matches rather than falling through to a guess. A name you wrote down is an instruction: "contains broker" also matches a customer VM called `password-broker`, and the cost of that mistake is an agent installed on a machine nobody expected |
| 2. **The VM that is already the control node** | An enrolled control node is not *guessed* away from. The agent's identity lives on that guest, so re-resolving onto a different VM leaves a container polling from a machine nothing points at. Below the typed name, deliberately — otherwise **name VM** would be silently ignored on the POVs that most need it |
| 3. **The only Linux VM** | Uniqueness, never position. "The first Linux VM" is not a decision a position in a list can make, but "the only one" is a fact about the template. VMs already named as the [Entitle agent host](../wiring.md) or the [Resource Broker host](../gateway-and-broker.md#the-resource-broker) are excluded — those are deliberately different machines |
| 4. **A VM called `broker`**, if several Linux guests remain | The old convention, kept as a tie-break |
| 5. Otherwise it **refuses, naming the candidates** | So you can pick one. The Control node column reads `none` with the reason beside it |

**A blank OS is not Linux.** Skytap does not report a guest OS as a field — the dashboard
infers it from whatever text the platform hands back, and it answers "unknown" rather than
guess wrong, because a confident wrong answer sends a Windows VM down an SSH path. A guest
whose OS reads `—` on the POV's **VMs tab** is therefore skipped by step 3, by the wire-up,
and by every other POV feature. **Set it yourself in the OS column on that tab**; it is
stored separately from the platform's own answer, so it survives every later refresh. That
one control is the remedy for most "this VM is being ignored" questions, not just for the
control node.

You can also correct the name after the fact: **name VM** beside the Control node column sets it
on a POV that already exists, which is what answers a step-5 refusal.

A POV that also runs a Resource Broker needs a **second** special VM — a Windows Server
2019 or 2022 x64 guest with WinRM enabled, on the same automatic network. See
[the Resource Broker](../gateway-and-broker.md#the-resource-broker); the rest of this section is
about the Linux control node.

That VM needs three things:

| | |
|---|---|
| **Docker**, enabled at boot | The agent is a container, and so is the Gateway it later runs beside itself. **Enabled** matters as much as installed: a POV boots the template, so a daemon that is merely running when the template was baked is a control node with no runtime on every POV. The build installs Docker CE if the base template carries no runtime, enables it, and leaves an existing one alone — Podman works if `docker` resolves to it, though what the builder bakes is Docker, because the agent drives the Engine API over the socket rather than a CLI |
| **An automatic network** | Skytap's metadata service answers only on VMs attached to one. On a manual network the VM gets no metadata at all, which looks exactly like a missing runner |
| **The metadata runner** | Below. Skytap hands `user_data` to the guest and **nothing executes it** |

## What the dashboard generates

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
- **The Docker socket is mounted**, which is root on the control node VM. The Agents page
  deliberately does *not* emit that mount — an operator's own agent host is theirs, and
  adding it there is a separate considered act. A POV control node is different: the dashboard
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

## SPIRE on the control node

When **Let agents attest through SPIRE** is on (Settings → Integrations → Remote Agents)
and this dashboard's [SPIRE server](../../../platform/spiffe.md) answers, the control node **attests through
SPIRE**, so its signing key is held only in memory. Nothing to configure per POV: each
control node install (**Install control node** or **Rebuild control node**) does what **Migrate to SPIRE** does for a hand-installed agent:

- it mints the enrolment code first, then a one-use **join token** for
  `spiffe://<trust domain>/node/<agent id>`;
- it binds the agent to `spiffe://<trust domain>/agent/<agent id>`;
- it writes the token and the trust bundle into the bootstrap beside the code.

On the guest, the script starts `spire-agent` and `spiffe-helper`, the same two sidecars
as the [agent overlay](../../../platform/spiffe/remote-agents/spire-attestation.md), with the same images and
the same locked-down settings. It then **waits up to two minutes for an SVID**:

| The guest | The agent starts with |
|---|---|
| gets an SVID | `AGENT_SPIFFE_JWT_FILE`; the enrolment code is not mounted, and is deleted |
| gets none: the SPIRE port is blocked, a proxy inspects TLS, or an image cannot be pulled | its **enrolment code**, exactly as before. The script prints a warning and `spire-agent`'s last log lines, and removes the sidecars |

**The fallback is the design.** A customer's network that stops SPIRE is the usual case
on site, and a control node on an Ed25519 key is worth more than one that never comes up.

The control node install job's last line, and an **SPIRE** or **Ed25519** label beside the
control node status on the POV list, say which way it went. If you expected SPIRE and got Ed25519, the
control node VM could not reach **SPIRE server address for agents** over plain TCP. Teardown
removes the control node's SPIRE entries along with the agent.

Port 8081 is the default. On an instance that shares its environment with another
dashboard, as the reference POV instance does on port 8091, set **SPIRE server address for
agents** to `host:port` first: a blank one sends control nodes to the other dashboard's server.

Before it mints anything, the control node install checks that address: it opens TLS to it and
requires the certificate to chain to this dashboard's own trust bundle and to name
`spiffe://<trust domain>/spire/server`. If something else answers, such as another
dashboard's server, SPIRE is not offered and the job says which address to fix, instead of
the guest waiting two minutes and falling back. If nothing answers from the dashboard, it is
offered anyway, because that proves nothing about the guest's own network.

## Fitting Skytap's 16,384-character limit

Skytap refuses a longer `user_data` with a 422 ("Contents is too long"). About half of the
script is comments written for whoever reads it on the guest, and the policy grows with
every VM in the POV. So the dashboard fits it before injecting, changing as little as it
can:

1. **sent as written** when it fits;
2. else **with its comment lines removed**. Heredocs (the policy, SPIRE's configs, the
   trust bundle) are kept byte for byte;
3. else **gzip-compressed and base64-encoded**, between the same two markers, with a
   short decoder that writes the script to a temporary file and runs it. `base64` and
   `gzip` are in coreutils and BusyBox alike.

The control node install job says when it had to do 2 or 3. A POV large enough that even 3 does not fit
is refused with both sizes named. Cloud POVs use the same 16 KB limit, which is EC2's.

## The ordering that matters

The payload is injected **after** the power-on, never before. An enrolment code lives
fifteen minutes and a first boot is not bounded — a Windows template pulling updates can
eat all of it. Injecting first hands the guest a code that expired while it was starting,
and the symptom is an agent stuck at `enrolling` with no request ever reaching the
dashboard.

Once the agent enrols, the dashboard **clears `user_data`**. It is readable by anyone who
can read the environment in Skytap, and clearing it also stops a reboot re-running a
bootstrap whose code is spent.
