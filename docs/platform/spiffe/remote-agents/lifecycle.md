# Upgrading and restarting agents centrally

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to upgrade, restart or pause remote agents from the dashboard instead of logging into each host.

Part of [Remote Agents](../remote-agents.md). From agent **2.10**, the Agents page can
upgrade an agent to a newer release, restart it, or pause it. Upgrades can be scheduled
into a change window and rolled out canary-first, and they roll back by themselves.

**Upgrade and restart need an agent attested through SPIRE**
([Attesting an agent through SPIRE](spire-attestation.md)). Under SPIRE the agent's
identity is its SPIFFE ID, not a key file inside the container. A new container attests as
the same agent, so the new agent can report on the job that replaced it. An agent enrolled
with a code keeps its identity in its state volume, and a replacement would be a stranger.
Pause works for every agent, because nothing runs on the host.

## The trust model: the dashboard chooses, the host decides

The remote-agent design assumes the dashboard may be compromised
([Philosophy](../remote-agents.md#philosophy)). Replacing the program that runs on your
network is the most powerful thing the dashboard could ask for. So the dashboard may only
**choose which signed release** an agent runs, and three gates on the host decide whether
it runs:

1. **The host opts in.** The agent's own `policy.yaml` carries an `upgrades:` block. No
   dashboard API can add it. Without it, the agent refuses every upgrade and restart.
2. **The agent checks the request.** It accepts an image only from the repository its
   policy names, only by digest (`sha256:…`, never a tag), and never older than itself
   unless the policy sets `allow_downgrade: true`.
3. **The updater verifies the signature.** Before it pulls anything, it runs
   `cosign verify` against the signing identity pinned in the same policy. The project's
   release workflow signs every agent image with keyless cosign, so a compromised
   dashboard, or a compromised agent, can at most request a release the project itself
   signed.

The dashboard never verifies a signature itself. A wrong release recorded on the
dashboard just offers an upgrade that every host refuses.

## Setting up a host

Two changes on the agent host, both yours:

1. **Add the updater.** Apply
   [`docker-compose.updater.yml`](https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/examples/remote-agent/docker-compose.updater.yml)
   on top of the base and SPIRE files:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.spire.yml \
                  -f docker-compose.updater.yml up -d
   ```

   - **What it adds.** A second container, `dashboard-agent-updater`, holds the Docker
     socket so the agent doesn't need it. It also adds the `agent_control` volume the
     two share, and the label `dashboard-agent.role=agent`, which marks the one container
     the updater may replace.
   - **What the updater cannot do.** It has no network listener and never talks to the
     dashboard. It runs a fixed allowlist of Docker operations on that container only,
     with images from the policy's repository only.

2. **Opt in, in `policy.yaml`:**

   ```yaml
   upgrades:
     enabled: true
     repository: chrweav/dashboard-agent
     verify:
       keyless:
         identity: "^https://github.com/Weaverlab-xyz/vm-dashboard-community/.github/workflows/publish-images.yml@refs/tags/v"
         issuer: "https://token.actions.githubusercontent.com"
     # allow_downgrade: false
     # ready_timeout_s: 480     # how long the new agent has to come up (max 540)
   ```

   - **The identity.** It is the release workflow, on a version tag. A fork that
     publishes its own images pins its own workflow here.
   - **Air-gapped mirrors.** For a mirror, set `repository:` to the mirror and verify with
     a key instead: `verify: {public_key: /etc/dashboard-agent/cosign.pub}`. Mount that
     file into the updater, and sign the mirrored images with the matching key.

**Network.** Keyless verification needs the updater to reach the registry and Sigstore:
`tuf-repo-cdn.sigstore.dev`, `rekor.sigstore.dev` and `fulcio.sigstore.dev`. A site that
can't reach Sigstore uses the `public_key` mode above.

**Upgrade to 2.10 by hand once.** An agent earns central upgrades only once it runs 2.10
with the updater beside it. Until then, the Agents page says what is missing.

**Agents the dashboard deployed** ([Deploying an agent into a cloud](deploying.md)) get
all of this automatically when they attest through SPIRE: the updater, the control
directory and the keyless `upgrades:` block. They also start the newest recorded release,
pinned by digest. With their cloud ticked under **Settings → SPIFFE and SPIRE**, they attest
with the VM's own identity rather than a join token; see
[Identity](deploying.md#identity).

## What an upgrade does

1. **The agent receives a signed job** naming the repository, digest and version, and
   checks them against its policy.
2. **It finishes its other running jobs** (it takes no new ones meanwhile), then writes
   the request into the control volume.
3. **The updater verifies the signature, pulls the image by digest, and checks the
   image's `dashboard-agent.version` label** is the version requested.
4. **It swaps the containers.**
   - It stops the agent, which drains, and renames that container to
     `dashboard-agent-previous`.
   - It creates the new agent with the same configuration: the same mounts, user, labels
     and network.
   - It starts the new agent.
5. **The new agent attests through SPIRE, leases, and writes `ready`.** It then completes
   the upgrade job with its version. The dashboard records the job as failed if that
   version is not the one requested.
6. **If the new agent exits or does not come up within `ready_timeout_s`**, the updater
   restores the previous container, and the restored agent fails the job with the reason.

**Restart** is the same sequence without a new image. Under SPIRE every start attests
with a fresh key held only in memory, so **a restart is also how you rotate an agent's
identity**. No separate action exists or is needed.

## On the Agents page

- **Each agent row** shows:
  - **update** when a newer release is recorded;
  - **canary** for the agent's ring;
  - **paused** when it is paused.
- **The row actions.**
  - **Upgrade** and **Restart** are disabled with the reason when the host can't take
    them: "Migrate to SPIRE first", "Upgrade to 2.10 by hand once", "Enable upgrades in
    this agent's policy.yaml", or "Add the updater container".
  - Either can run now, at a time, or in a [change window](../../../operations/scheduling/change-windows.md).
    A missed window cancels the job like any other.
  - Only one upgrade or restart can be queued or running per agent at a time.
- **Pause** hands the agent no new jobs. It keeps heartbeating, and the jobs it holds
  finish. **Resume** undoes it.
- **The details panel** sets the **rollout ring**: `canary` or `default`.

### Releases

The **Lifecycle** panel lists the releases the dashboard may offer.

- **Check Docker Hub** reads `chrweav/dashboard-agent:latest`: the image's digest, and
  its version from the `dashboard-agent.version` label. The image tag is the dashboard
  release, not the agent version.
- **Add release** records a version, a digest and, for a mirror, the mirror's repository
  (blank means `chrweav/dashboard-agent`). Use it for a mirror, or for a dashboard with no
  route to Docker Hub. The repository must be the one the host's `upgrades.repository`
  names.
- **Recording releases also arms a check.** Once any release is recorded, an agent that
  reports a version of 2.10 or later that is not among them is flagged **Not a recorded
  release**. A signed build always comes from a release, so the version is either a local
  build or a release nobody recorded yet. See
  [Ongoing checks](spire-attestation.md#ongoing-checks).

### Rollouts

**New rollout** upgrades every eligible agent to a release:

1. Agents in the **canary** ring are queued first, now or in the chosen window.
2. Once **every** canary upgrade has completed, the schedule sweep queues the rest, in
   the same window.
3. **Any failure stops the rollout.** That includes a rollback, or a canary job cancelled
   by a missed window. The agents not yet queued stay as they are.

Agents that can't be upgraded are listed with the reason, never silently skipped. With no
canary agents, the rollout goes straight to everyone.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The upgrade fails with "upgrades are allowed only from …" | The release's repository is not the one `upgrades.repository` names. A mirror needs a release recorded with the mirror's repository and digest. |
| It fails with "no matching signatures" or a certificate identity error | The image was not signed by the identity your policy pins: a fork's image, a mirror not re-signed, or an identity regex typo. |
| It fails with "rolled back" | The new agent exited or never reported ready. Its log is in `docker logs dashboard-agent` if it exits fast, and the updater's in `docker logs dashboard-agent-updater`. A SPIRE agent that can't fetch a fresh JWT-SVID within the timeout looks like this too. |
| Upgrade stays disabled with "Add the updater container" | The agent sees no fresh `updater.alive` in the control volume. Check that the updater is running and that both containers mount the same `agent_control` volume. |
| The job fails as stale after ten minutes | The host went away mid-swap, so nothing was left to report. Check the host. `docker ps -a` shows whether `dashboard-agent-previous` is still there. |
