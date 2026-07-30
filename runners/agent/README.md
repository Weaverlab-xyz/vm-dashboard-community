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
| Needs | a `policy.yaml` you write, mounted read-only |
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
| `AGENT_ENROLLMENT_CODE` | — | Required on first start only; the identity persists. |
| `AGENT_STATE_DIR` | `/var/lib/dashboard-agent` | Holds the 0600 private key. Mount a volume. |
| `AGENT_POLICY_FILE` | `/etc/dashboard-agent/policy.yaml` | Mount read-only. |
| `AGENT_MODE` | `normal` | `audit` logs what it would do and executes nothing. |
| `AGENT_POLL_INTERVAL` | `5` | Seconds, jittered. |
| `AGENT_CA_BUNDLE` | — | For a TLS-inspecting corporate proxy. |
| `AGENT_INSECURE_TLS` | — | Lab only. Warns on every poll. |
| `KUBECONFIG` | `/etc/dashboard-agent/kubeconfig` | Optional. Only the server URL and version are reported. |

`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are honoured. Set `NO_PROXY` for your private
ranges, or probes get routed to the corporate proxy and fail confusingly.

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
