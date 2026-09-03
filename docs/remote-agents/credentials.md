# Credentials an agent uses

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deciding where a target's credential lives — the dashboard, or the host.

Part of [Remote Agents](../remote-agents.md). What the dashboard holds, what the host seals, and where a credential comes from at run time.

### The credential the dashboard holds

Set `dashboard_secret: true` on a connection in your `connections.yaml` and the agent stops
reading a credential locally. Instead, for each job, it asks the dashboard.

Two things make that safe to do over the same channel the agent polls on:

* **It is scoped to the job.** The route is `POST /api/agent/jobs/{job_id}/secret`, and the
  connection it answers for is derived from *the job row*, not from anything the request
  says. The agent cannot name a different connection, so a stolen identity cannot enumerate
  what else the dashboard holds. The job must also be `running` — a cancelled job may still
  log and complete, but it gets no fresh credential.
* **It is encrypted, not merely transported.** The agent generates an X25519 keypair per
  fetch and sends the public half in the request body, which its Ed25519 signature already
  covers. The dashboard seals the credential to it (X25519 → HKDF-SHA256 → AES-256-GCM). So
  the guarantee in [Behind a TLS-inspecting proxy](agent-host.md#behind-a-tls-inspecting-proxy) still
  holds for a *response* body, not just a request: the inspecting proxy sees a ciphertext.
  The private half never touches disk and dies with the fetch, so unlike an enrolment-bound
  key there is nothing on the host to steal and use on traffic captured earlier.

The seal is bound to the agent, the job **and the connection ref**. That last one is the
least obvious and the most important: without it a credential released for `dc1-vcenter`
could be relabelled as the credential for a connection pointing somewhere else, which turns
credential confusion into credential exfiltration.

The credential is held in memory for the job and scrubbed out of anything the agent sends
back — Live Output and the job's error string both — because `str(exc)` from an arbitrary
library can carry it, and that string is the only text a failed job renders. Nothing here
claims to wipe it from memory: a Python string cannot be zeroed, and the bound is scope.

**Requires an agent image of 2.1.0 or newer.** The dashboard refuses to *queue* work for an
older one rather than let it fail: a 2.0 agent does not know the key, so it would fall
through to a password you had just deleted, send an empty one, and get back the hypervisor's
own "wrong username or password" — the wrong diagnosis, and on the 30-minute sync schedule
it retries until the service account locks out. Pull the image and restart; re-enrolment is
not needed.


### Sealing a credential this host keeps

Some sites will not move a credential to the dashboard — that being the point of an on-prem
agent for them — but do not want it sitting in a YAML file as text. `password_sealed:` in
`connections.yaml`, and `client_secret_sealed:` in `passwordsafe.yaml`, hold a value
encrypted against a key in the agent's state volume.

**Be precise about what this defends against**, because [The agent's private
key](agent-host.md#the-agents-private-key) argues the opposite for `identity.json` and both statements are
true. Sealing is **not** protection from `root` on the agent host. The key is on the same
machine, which is unavoidable for a process that has to restart unattended — a passphrase an
unattended container must read at boot has to be stored next to the key it protects, and that
has not changed.

What it protects is the **file travelling.** `connections.yaml` is a file you author, edit,
version and copy: into a git repo, an Ansible role, a runbook, a support ticket, a
screenshot, a config backup. `identity.json` does none of that — it is written by the agent
into a 0700 volume and never leaves it. The key lives in that volume, so it goes into none of
those copies, and **a copy of the config is worthless anywhere else.** That is the whole
claim. If your threat model is a compromised agent host, use
[`dashboard_secret`](#the-credential-the-dashboard-holds) or `ps_managed_account` instead;
those are the rows in the table above that change what host compromise yields.

Seal a value with the agent image itself. Mount the **same state volume the agent runs with**:

```bash
docker run --rm -it -v dashboard_agent_state:/var/lib/dashboard-agent \
    chrweav/dashboard-agent:latest seal --host vcenter.lab.internal
```

```powershell
docker run --rm -it -v dashboard_agent_state:/var/lib/dashboard-agent `
    chrweav/dashboard-agent:latest seal --api-url https://passwordsafe.corp.internal
```

It prompts for the value without echoing it, prints one line on stdout, and that line is what
you paste in. Everything else it says goes to stderr, so `seal … > value.txt` gives you the
token alone. A prompt rather than an argument is deliberate: on Windows it is the only way to
keep the value out of the shell's on-disk history, the same reason the enrolment code has a
file form.

Three things about it that are load-bearing rather than incidental:

- **The address is part of the seal.** `--host` must match the entry's `host:`, and changing
  the address means sealing again. `connections.yaml` is re-read *per job*, so an edit takes
  effect with no restart and without Docker access — and without the binding, somebody who
  can edit that file but cannot read the state volume could move a sealed vCenter password
  onto an entry pointing at a host of their own, with `verify_ssl: false`, and read the
  plaintext off the next sync. This is the same reason the dashboard is never allowed to set
  `host`. For `--api-url` only the host part is bound, so either accepted form of `api_url`
  works.
- **Mount the volume, or the key is thrown away.** Run `seal` without `-v` and it refuses
  outright rather than sealing against a key that dies with the container. If a value somehow
  was sealed elsewhere, the agent says which key sealed it and which key is present instead
  of "decryption failed".
- **Delete the plaintext.** A leftover `password` or `password_file` under a `password_sealed`
  is ignored and warned about on every job, because it means a credential is still here in
  the clear. Same rule as a leftover under `dashboard_secret`.

**Requires an agent image of 2.2.0 or newer, and the dashboard cannot warn you** — unlike
`dashboard_secret` it never sees this file, so it has nothing to gate on. You cannot reach
that state by accident either, because `seal` ships in the same image that reads the key. The
backstop is that the agent now **refuses** a connection declaring no credential at all rather
than sending an empty password, which is the shape an older image on a sealed-only entry
would otherwise produce.


### Where the credential comes from

Five sources. A *remote* source beats a local one — `ps_managed_account` or
[`dashboard_secret`](#the-credential-the-dashboard-holds), then
[`password_sealed`](#sealing-a-credential-this-host-keeps), then `password_file`, then an
inline `password` — because an operator who has moved a connection off local storage should
not silently keep authenticating with a stale literal left in the file underneath it. A
leftover is warned about on every job rather than ignored quietly, since it means plaintext
is still sitting on a host you meant to clear.

`password_sealed` is not a fourth *authority*; it is the same local credential, not written
down in the clear. That is why it is ordered against the two plaintext forms rather than being
exclusive with them, and why a sealed value found in `password:` — or in the file
`password_file` points at — is **refused** rather than sent. Sent as a literal it would come
back as a wrong password, which reads as the wrong problem entirely.

**A connection that declares none of the five is refused**, naming all five keys. It used to
fall through to an empty password, which the endpoint answers as a wrong one, so a connection
that simply had no credential looked like a connection with a bad one — and on the inventory
sync schedule it retried until the service account locked out.

The two remote sources are **mutually exclusive rather than ordered**. They are different
authorities — the agent asking Password Safe, versus the dashboard asking on its behalf —
there is no stale-leftover story that makes preferring one kind, and choosing quietly would
leave nobody able to say which credential a job actually used. Declare both and the agent
refuses the job and names the file.

`password_file` — and `client_secret_file` in `passwordsafe.yaml` — are read with the same
encoding rules as the enrolment code file: a UTF-8 BOM is stripped, and a UTF-16 file is
refused with a message naming the encoding rather than a decode traceback out of a job.
Write them with `Set-Content -Encoding ascii -NoNewline` on Windows.

With `ps_managed_account`, **the agent holds no hypervisor credential at all** — only a
Password Safe OAuth client whose single power is to ask for one. Each job checks a
credential out and checks it back in, so every use lands in Password Safe's audit trail
and is subject to its policy and approval workflow. That gets the agent close to the end
state this design was heading for: no standing *hypervisor* credential on the host.

It does not get all the way there, and the remaining gap is worth naming: the Password Safe
OAuth client is itself a credential on this host, and its entitlements are usually broader
than the single account it is being used for.
[`dashboard_secret`](#the-credential-the-dashboard-holds) closes that gap by moving the
asking to the dashboard, which already holds such a client for other features. Pick one —
declaring both is a refusal, not a precedence.

Mount the client alongside the other two files — see
[`passwordsafe.example.yaml`](../../examples/remote-agent/passwordsafe.example.yaml). The
dashboard never issues it and never sees it.

A check-in failure is swallowed deliberately: the credential has already been used and the
request expires on its own duration, so failing a completed job over it would be the wrong
trade. An account that requires human approval will not release, and the job fails rather
than hanging — use auto-approve policies for accounts an unattended agent needs.
