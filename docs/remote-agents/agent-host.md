# Running the agent on your host

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are the one running the agent container, on the host that can reach the targets.

Part of [Remote Agents](../remote-agents.md). The private key, the policy file, Windows and Podman, and a TLS-inspecting proxy.

## The agent's private key

The agent generates an Ed25519 keypair on first start and keeps the private half. Only
the public half is ever sent, so **a dump of the dashboard's database cannot impersonate
an agent** — there is nothing in it to replay.

Where it lives, exactly: `$AGENT_STATE_DIR/identity.json`, created mode 0600 inside a
0700 directory owned by uid 10001, written to a temporary file and renamed so it never
exists world-readable even for an instant. It is **not** encrypted at rest: no passphrase,
no TPM, no keyring. That is a deliberate limit rather than an oversight — a passphrase an
unattended container must read at boot has to be stored next to the key it protects.

That reasoning still holds, and it is worth reading alongside
[Sealing a credential this host keeps](credentials.md#sealing-a-credential-this-host-keeps), which *does*
encrypt a hypervisor credential on this host. The two are not in tension: sealing is not
offered as protection from `root` here either. It exists because `connections.yaml` is a file
you copy into repos, tickets and backups, and this one never leaves its volume.

Who can read it:

| Who | Can read the key |
|---|---|
| `root` on the agent host | **Yes.** `docker cp`, `docker exec`, the volume on disk, `/proc/<pid>/mem`. File modes do not constrain root. |
| Anyone in the `docker` group | **Yes** — socket access is root-equivalent, with or without this agent. |
| Another container on the host | No, unless it mounts the state volume or the Docker socket. |
| Anyone on the network, including a TLS-inspecting proxy | No. The key never crosses it, and a signature is good for one method, one path, one body, sixty seconds. |
| The dashboard, or whoever compromises it | No. It only ever received the public half. |

**What a stolen key actually grants**, which is the part worth being precise about: it
authenticates five `POST /api/agent/*` routes and nothing else. The holder can lease jobs
an operator already queued *for that agent*, heartbeat them, push log lines, and complete
them. They cannot create a job (that needs an admin session), learn anything about what
else the dashboard manages, reach any user-facing route, or touch another agent's jobs —
`owned_job` filters on `agent_id` and answers "not found" identically for "not yours", so
the API is not even an oracle for their existence.

Once the agent brokers hypervisor connections it does hold target credentials, so be precise
about what the holder inherits. Where the credential sits in `connections.yaml`, they have it
outright — but then so does anyone who can read that file, and the stolen key is beside the
point. Where the connection uses `dashboard_secret`, the key lets them **request** a
credential, and that is a narrower and much noisier thing: only for a connection bound to
that agent, only while a job it can lease is actually running, one audited event per release,
and revocable by clearing the agent's public key. A leaked file is none of those.

That leaves three real consequences: denial of service against that one agent's queue; the
ability to **report false findings** — data a human reads and acts on, so treat it as
untrusted input, which is the other reason nothing is auto-registered; and, for
`dashboard_secret` connections, credential requests that show up in the audit log and stop
the moment the key is revoked.

Revocation does not depend on reaching the container. **Revoke** clears the stored public
key, so the next poll fails verification whatever that container is still doing, and any
job it held is failed immediately rather than at the next reconcile.

### Running under Podman

Rootless Podman is a real improvement here and worth preferring — as long as it is
recommended for the right reason. It changes *who* can steal the key. It does not change
whether host root can.

What it actually buys:

- **No daemon socket**, so the `docker` group row above disappears entirely. In most
  shops that group is the larger of the two exposures.
- The state volume lives under `~/.local/share/containers/storage/volumes/…`, owned by one
  unprivileged user rather than by root, with uid 10001 mapped through that user's subuid
  range.
- Every hardening flag in the emitted command works unchanged — `--read-only`,
  `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user`, `--tmpfs`. Substitute
  `podman run` for `docker run` and paste it as-is.

Two things to know before you do:

- **On SELinux hosts the bind mounts need a label suffix** — `:ro,Z` — or the container
  cannot read `policy.yaml`. Podman's natural home is RHEL-family hosts, where SELinux is
  enforcing by default, so this is the common case rather than the exotic one. The emitted
  command already carries it, and Podman ignores `Z` where SELinux is absent; if you are
  pasting an older command, add it. That fails closed (`Cannot read the policy file`,
  exit 2), so it is a confusing five minutes rather than a hole — see
  [Troubleshooting](../remote-agents.md#troubleshooting) for how to recognise it, because the dashboard-side
  symptoms all look like networking.
- `--restart unless-stopped` does not survive a reboot on its own; rootless Podman needs
  `systemctl --user enable podman-restart` (and `loginctl enable-linger`) because there is
  no daemon to do it for you.

What neither runtime fixes is host root. If that is the threat you are defending against,
the answer is not a different container runtime — it is a credential that cannot be
copied, sealed to hardware, which is a future feature rather than a configuration. Until
then the honest position is the blast-radius table above plus fast revocation.


## Running on Windows

A Windows desktop or server is a perfectly good agent host, and for some hypervisors it is
the *only* sensible one — Hyper-V and VMware Workstation both live on Windows. There is no
Windows-container build of the agent and you do not need one: Docker Desktop runs the Linux
image in its Linux VM, the same way it runs everything else.

**Almost nothing about the container changes.** Every hardening flag is identical —
`--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user 10001:10001`,
`--tmpfs`. What changes is the *shell* you paste into. Set the toggle on the Agents page to
**Windows (PowerShell)** and the emitted command is already adapted:

- backtick line continuations instead of `\`. This is the one that bites: a trailing `\` is
  not a syntax error in PowerShell, it is a literal argument, so `docker run` takes it as the
  image name and every following line executes as its own command. You get `invalid
  reference format`, then `The term '-e' is not recognized`, and nothing at all pointing at
  the paste having come apart.
- `${PWD}` instead of `$PWD`, and `Set-Content` / `Remove-Item` instead of `printf` / `rm`.

Six things to know before you start:

- **Docker Desktop must be in Linux-containers mode.** In Windows-containers mode the pull
  fails with `no matching manifest for windows/amd64`, or — if the image is already
  local — `image operating system "linux" cannot be used on this platform`. Neither says
  "you are in the wrong mode". Right-click the tray whale → *Switch to Linux containers*.
- **`:ro,Z` is inert here, and kept on purpose.** It is the SELinux relabel, and Docker
  ignores `z`/`Z` on any host without SELinux — which includes Docker Desktop's VM, exactly
  as it does on Ubuntu or Debian. Keeping it means there is one mount string rather than two
  that can drift. It costs nothing; ignore it. The corollary matters more: **every `chcon`
  and `ls -lZ` instruction in this document is Linux-only.** If a mount is unreadable on
  Windows it is file sharing, not labelling — check *Settings → Resources → File sharing*.
  The agent applies that same qualification to its own error text: it recognises Docker
  Desktop's kernel and names file sharing instead of the SELinux label, so you should not
  see `chcon` advice on this host at all.
- **File permissions work out in your favour.** Docker Desktop presents bind mounts with
  permissive ownership because NTFS ACLs do not map onto POSIX mode bits, so uid 10001 can
  read `policy.yaml` with no `umask` and no `chmod`. The emitted PowerShell command has no
  `umask` equivalent for that reason, not by omission.
- **Keep `policy.yaml` under your user profile.** A UNC path (`\\server\share`) or a mapped
  network drive cannot be bind-mounted, and if Docker Desktop cannot reach a bind source on
  the Hyper-V backend it materialises an *empty directory* in the container instead of
  failing — so the agent reports it cannot read its policy file and the advice you get points
  at mount flags rather than at file sharing. Notepad is a fine editor for the file; CRLF
  parses.
- **`--restart unless-stopped` only holds while the engine is running.** Docker Desktop lives
  inside a logged-in user session, so enable *Start Docker Desktop when you log in* and know
  that logging out stops the agent. This is the Windows counterpart of the rootless-Podman
  caveat above. For an agent that must stay up unattended, use Windows Server with the WSL2
  engine, or run it in a Linux VM on the same host.
- **The source IP the dashboard records is Docker's NAT gateway,** not the Windows host's LAN
  address. The **Source IP** column and the audit rows will show that. It is not a fault, and
  it does mean the column cannot identify *which* Windows host an agent is on.

**What discovery can reach from a NAT'd container.** Probes are ordinary unprivileged
unicast TCP connects — no ICMP, no ARP, no broadcast or multicast — so NAT costs you nothing:
anything routable from the Windows host is routable from the container, and being off the
LAN's layer-2 segment does not matter. Every target is an address the agent computed
arithmetically from a CIDR you wrote down.

The exception is **the host the agent is running on**. Inside the container `127.0.0.1` is
the container, so `allow_loopback` cannot reach a service listening on the Windows machine's
own loopback — see [VMware Workstation Pro](hypervisors.md#vmware-workstation-pro), where that is the
whole question. Docker Desktop's `host.docker.internal` resolves to the host and is a
legitimate `fqdn:` target, but it only helps if the service is listening on an address other
than `127.0.0.1`.


## Behind a TLS-inspecting proxy

The property this section describes is the reason the agent signs requests instead of
carrying a bearer token, and it extends to credential *responses*: a credential fetched with
[`dashboard_secret`](credentials.md#the-credential-the-dashboard-holds) is sealed to a per-fetch key inside
the TLS session, so an inspecting proxy that reads the whole body reads a ciphertext. That
was the objection to holding credentials centrally at all, and it is why this is encrypted at
the application layer rather than trusted to TLS.

Mount the inspection CA and point `AGENT_CA_BUNDLE` at it —
[`docker-compose.corp-ca.yml`](../../docker-compose.corp-ca.yml) is the same pattern for
the dashboard. `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are honoured automatically.

They apply to the **dashboard connection only**. The discovery probes are raw sockets
and ignore proxy variables entirely, so `NO_PROXY` is not needed to keep a scan off the
corporate proxy — a scan was never going through it. What `NO_PROXY` is for here is the
narrower case of the dashboard itself being on a network the proxy should not be used
for.

A bad `AGENT_CA_BUNDLE` path is worth knowing about because it fails badly: `requests`
raises a bare `OSError("Could not find a suitable TLS CA certificate bundle, invalid
path: …")`, which is not a `RequestException` and so is not caught — the agent dies with
a traceback and exit 1 rather than a readable message.

Worth saying plainly: an inspecting proxy sees the full content of every request. That
is exactly why the agent's credential is a per-request signature rather than a bearer
token — the proxy captures nothing it could replay. This turns the corporate proxy from
an objection into a demonstration.


## 2. Write the policy

Copy [`examples/remote-agent/policy.example.yaml`](../../examples/remote-agent/policy.example.yaml)
and edit the ranges:

```yaml
targets:
  - cidr: 10.20.0.0/24
    ports: [443, 8006, 9440, 5985, 5986]
deny:
  - 169.254.0.0/16
job_types:
  - agent_discover
limits:
  max_hosts: 1024
```

Omitting `ports` allows any port in the range. Naming them is the difference between
"may look for databases here" and "may reach anything here".

Mind the space after the dash. `-cidr: 10.20.0.0/24` is valid YAML — for a key *named*
`-cidr`, not for a list entry — so the whole list parses as a mapping and grants nothing.
The agent refuses to start on that, and on any entry it cannot read as a target, and on a
key written twice (YAML keeps only the last, which in this file silently drops a grant).
Being loud is the only safe direction here: a target that is skipped rather than rejected
leaves an agent running with an allow-list you believe you wrote.

Mount it `:ro,Z`, as the emitted command does. The `Z` is the SELinux relabel and it is not
cosmetic: on Fedora, RHEL, CentOS, Rocky or Alma a bind mount keeps its host label, and the
container may not read a file in your home directory however permissive its mode is — the
agent then exits 2 on `Permission denied` against a mode 644 file. Docker and Podman ignore
`z`/`Z` where SELinux is absent, so it is safe on every host.
