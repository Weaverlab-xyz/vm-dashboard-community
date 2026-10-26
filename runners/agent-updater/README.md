# Agent updater

Runs beside a remote agent (agent 2.10+, SPIRE-attested) so the dashboard can **upgrade**
it to a newer signed release, or **restart** it, without anyone logging into the host.

It is the only container on the host that holds the Docker socket, and it does one thing
with it: replace the agent container, with a newer image or the same one restarted, and
put the old one back if the new one does not report ready in time.

## Trust

The dashboard only *chooses* a release. Whether any release may run is decided on the host:

1. **policy.yaml must enable `upgrades:`.** The dashboard cannot turn it on.
2. **The agent checks the request:** the policy's repository only, by digest, and no
   downgrade unless `allow_downgrade` is set.
3. **This updater verifies the image's cosign signature** against `upgrades.verify`
   before it pulls anything. The default `keyless` identity is this project's release
   workflow; `public_key` is for a mirror you re-sign yourself.

It has **no network listener** and **never talks to the dashboard**. Its only input is
`request.json` in the volume it shares with the agent. Its Docker operations are a fixed
allowlist (`DockerAPI.ALLOWED`), limited to the container labelled
`dashboard-agent.role=agent` and to images `<upgrades.repository>@sha256:…`.

## Run it

Apply `examples/remote-agent/docker-compose.updater.yml` on top of the base and SPIRE
files. The full guide is "Upgrading and restarting agents centrally" in the remote-agent docs.

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_CONTROL_DIR` | `/var/lib/dashboard-agent-control` | The volume shared with the agent |
| `AGENT_POLICY_FILE` | `/etc/dashboard-agent/policy.yaml` | The agent's own policy, read-only |
| `UPDATER_DOCKER_SOCKET` | `/var/run/docker.sock` | The Docker socket |
| `UPDATER_STOP_TIMEOUT` | `40` | Seconds `docker stop` waits; keep it above `AGENT_DRAIN_SECONDS` |
| `UPDATER_COSIGN_ARGS` | *(none)* | Extra `cosign verify` flags, for a private or air-gapped registry only (for example `--allow-insecure-registry --insecure-ignore-tlog=true` with `public_key`). These flags can't change which key or identity is required. |

Keyless verification needs to reach the registry and Sigstore (`tuf-repo-cdn.sigstore.dev`,
`rekor.sigstore.dev`). Without that egress, use `public_key` with a mirror.
