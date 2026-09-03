# `chrweav/dashboard-agent`

A long-lived agent that runs **inside a private network**, dials out to the dashboard
over HTTPS, and asks for work. Nothing listens; no inbound firewall rule is needed.

Unlike the other images under `runners/`, this one is not launched by the dashboard —
an operator runs it, on their own infrastructure, and the published tag is how it gets
there. See [`docs/remote-agents.md`](../../docs/remote-agents.md) for the full setup.

## The contract

| | |
|---|---|
| Talks to | one dashboard, over HTTPS, outbound only |
| Authenticates with | an Ed25519 keypair it generates at enrolment |
| Executes | nothing the dashboard sends — see below |
| Needs | a `policy.yaml` you write, mounted read-only (`:ro,Z` — see below) |
| Exits non-zero when | misconfigured, revoked, or the policy is missing/corrupt |

## Why it cannot be turned into a backdoor

An agent is, structurally, a dashboard-controlled endpoint inside someone else's
network. The design assumes the dashboard may be compromised and makes that
insufficient:

1. **No executable content crosses the protocol.** A discovery job names networks and
   ports. There is no field that can hold a command, a script, a URL or a filename, and
   the handler table is a closed dict rather than a dispatch on a string from the wire.
2. **Job envelopes are signed** by a dashboard key the agent pins at enrolment. Someone
   who can write a row into the dashboard's database still cannot produce a signature,
   so the job is refused before its payload is parsed.
3. **`policy.yaml` is yours.** No dashboard API can read, write or override it. It
   names the networks and ports that may be touched, and the agent reports only its
   sha256 so you can see on the Agents page if it changed. Missing or unparseable means
   *refuse everything*.
4. **The image carries no execution machinery** — no ansible, no kubectl, no helm, no
   container socket. `tests/test_agent_runner_contract.py` asserts that, so it stays
   true rather than being a claim in a README.

Probes never authenticate. Discovery is a TCP connect plus a pre-auth protocol banner
(Postgres SSLRequest, MySQL greeting, TDS PRELOGIN, Oracle TNS, Kubernetes
`GET /version`). Authenticated probing of unknown hosts locks out service accounts and
reads like credential spraying in a customer's SIEM.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `DASHBOARD_URL` | — | Required. Must be `https://` unless `AGENT_INSECURE_TLS=1`. |
| `AGENT_ENROLLMENT_CODE` | — | Required on first start only; the identity persists. Stays readable via `docker inspect` for the container's life — prefer the file below. |
| `AGENT_ENROLLMENT_CODE_FILE` | — | Path to a mounted file holding the code. Wins over the variable above. Must be readable by uid 10001, and mounted `:ro,Z`. Read as text, so it must be ASCII or UTF-8 with no BOM — see [Running on a Windows host](#running-on-a-windows-host). |
| `AGENT_STATE_DIR` | `/var/lib/dashboard-agent` | Holds the 0600 private key, and `sealing.key` if you use [`seal`](#sealing-a-credential-kept-on-this-host). Mount a volume. |
| `AGENT_POLICY_FILE` | `/etc/dashboard-agent/policy.yaml` | Mount read-only, as `:ro,Z`. |
| `AGENT_MODE` | `normal` | `audit` logs what it would do and executes nothing. |
| `AGENT_POLL_INTERVAL` | `5` | Seconds, jittered. |
| `AGENT_CA_BUNDLE` | — | For a TLS-inspecting corporate proxy. |
| `AGENT_INSECURE_TLS` | — | Lab only. Warns once at startup. Case-sensitive: `1`, `true` or `yes`. |
| `KUBECONFIG` | `/etc/dashboard-agent/kubeconfig` | Optional. Only the server URL and version are reported. |

`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are honoured. Set `NO_PROXY` for your private
ranges, or probes get routed to the corporate proxy and fail confusingly.

## Sealing a credential kept on this host

`seal` is the one subcommand this image takes; everything else is an environment variable.
It encrypts a single value against a key in the state volume and prints the result, which
you paste into `password_sealed:` in `connections.yaml` or `client_secret_sealed:` in
`passwordsafe.yaml`.

```bash
docker run --rm -it -v dashboard_agent_state:/var/lib/dashboard-agent \
    chrweav/dashboard-agent:latest seal --host vcenter.lab.internal

docker run --rm -it -v dashboard_agent_state:/var/lib/dashboard-agent \
    chrweav/dashboard-agent:latest seal --api-url https://passwordsafe.corp.internal
```

The value is read from a prompt without echo, or from stdin when there is no TTY. Only the
token goes to stdout, so `seal … > value.txt` works.

**Mount the same volume the agent runs with.** Without it the key would be created inside
the container and destroyed on exit, taking every value sealed against it — so `seal`
refuses instead. The sealed value is also bound to the address you pass, so `--host` must
match the entry's `host:` and changing that address means sealing again.

This is not protection from `root` on this host: the key is on the same machine, which is
unavoidable for a container that restarts unattended. It protects the *config file*, which
gets copied into repos, tickets and backups in a way the state volume does not. See
[docs/remote-agents.md](../../docs/remote-agents/credentials.md#sealing-a-credential-this-host-keeps)
for the full trade against `ps_managed_account` and `dashboard_secret`.

## Bind mounts need `:ro,Z` on SELinux hosts

On Fedora, RHEL, CentOS, Rocky or Alma — a large share of on-prem Linux — a bind-mounted
file keeps its host SELinux label, and the container may not read the `user_home_t` of a
file in your home directory however permissive its mode is. Without the `Z` suffix the
agent exits 2 on `Cannot read the policy file … Permission denied` on a mode 644 file, and
because that happens *before* the first network call, nothing reaches the dashboard: the
agent row stays at `enrolling` with no source IP, which reads like a DNS or TLS fault.
Docker and Podman ignore `z`/`Z` where SELinux is absent, so the flag is safe everywhere
and no host detection is needed. The install command the Agents page emits already includes
it; check a hand-written one with `ls -lZ policy.yaml`.

`Z` gives the file a label private to this container, which is what you want for one agent
per host. Use lowercase `z` instead if the same `policy.yaml` is mounted into more than one
container, since a private label would leave only the most recent one able to read it.

## Running on a Windows host

This is a Linux image (`linux/amd64`, `linux/arm64`) and there is no Windows-container
build. It runs on a Windows desktop or server under Docker Desktop's Linux VM, which is an
ordinary supported arrangement — keep Docker Desktop in **Linux containers** mode, or the
pull fails with `no matching manifest for windows/amd64`.

What changes is the shell, not the container: PowerShell continues lines with a backtick
rather than a `\`, spells the working directory `${PWD}`, and writes files with
`Set-Content` rather than `printf`. The Agents page emits both flavours behind a toggle, so
there is nothing to translate by hand.

Two things behave differently here rather than merely looking different:

- **`:ro,Z` is inert**, since there is no SELinux. Harmless, and kept so there is one mount
  string rather than two. An unreadable mount on Windows is Docker Desktop file sharing, not
  a label — so the `chcon` and `ls -lZ` advice above cannot apply. Bind mounts also arrive
  with permissive ownership, so the uid-10001 readability problem does not arise at all.
- **`AGENT_ENROLLMENT_CODE_FILE` is encoding-sensitive.** The file is read as text, so a
  UTF-16 one — what PowerShell 5.1's `>` and `Out-File` produce — fails to decode, and a
  UTF-8 BOM survives the whitespace trim and makes the code invalid. Write it with
  `Set-Content -Encoding ascii -NoNewline`.

See [`docs/remote-agents.md`](../../docs/remote-agents/agent-host.md#running-on-windows) for the host
setup, including why `--restart unless-stopped` does not survive a logoff.

## Back-pressure

The dashboard throttles each agent (see `services/agent_guard.py`) and answers **429**
with `Retry-After` when one is over its cap. The agent honours that interval instead of
its own doubling, with jitter so a fleet throttled together does not return in lockstep.

A **401 is not a 429**: 401 means the signature was refused — revoked, re-enrolled
elsewhere, or the audience changed — and the process exits rather than hammering. Only a
429 means "come back later". Anything that conflates the two turns a busy minute into a
fleet that never reconnects.

## Build

```bash
docker build -t chrweav/dashboard-agent:dev runners/agent
```

CI publishes multi-arch (amd64 + arm64) on a version tag, alongside the other runner
images — see `.github/workflows/publish-images.yml`.

## Local checks

```bash
python tests/test_agent_probes.py
```

`tests/test_agent_runner_contract.py` is the one that matters most: `agent.py` vendors
its own copy of the request canonicalization (this image versions independently of the
dashboard, like `runners/promote`), and that test pins the copy against
`web_dashboard/services/agent_signing.py` byte for byte. Drift there would produce
signatures that are always generated and never verify — a failure that looks like a
revoked agent rather than a bug.
