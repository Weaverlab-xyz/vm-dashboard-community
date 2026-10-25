# Building a SPIRE lab

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are building a SPIRE lab from the dashboard, as VMs, Docker or Kubernetes, or by hand.

Part of [SPIFFE and SPIRE](../spiffe.md).

The **Workload Lab** page's **SPIRE** tab (preview; enable `spire_lab_enabled` under Settings → Preview
features) does the steps below as one job. Configure it under **Settings → SPIRE Lab**,
and upload the playbooks on the **Storage** page first — the `spire-*.yml` set, plus
`install-docker.yml` or `k3s-server-init.yml` for the Docker and Kubernetes modes. A run
fetches assets *by filename from the storage backend*, never from `examples/`, and the
build form names the ones each mode is missing. Storage is where assets are uploaded; Config
Management only *runs* them.

**It attaches to a VM you already deployed rather than creating one.** That is not a
shortcut: the page re-derives the host from this dashboard's own deploy rows rather than
trusting an address it is handed, because four privileged playbooks against a host of the
caller's choosing is not something it should accept. So a VM built by hand in the portal
will not be offered as a host at all. The VM also keeps its own auto-delete timer and its
own Destroy, which is why tearing the lab down closes `tcp/8081` and leaves the host
alone.

**How the runs log in is yours to choose.** By default the Ansible runner resolves the
host's SSH key from that VM's own *deploy job*, which needs nothing from you. The build
form also takes either a **Password Safe managed account** (checked out just-in-time) or
a **Secrets-Management SSH-key secret**, plus an optional login user — the same two
pickers Config Management has, reading the same two endpoints. Two things worth knowing
before you pick an account:

- Every playbook runs with `become`, and the form has no separate sudo credential, so
  tick **"also use this account for sudo"** unless the account is root or has passwordless
  sudo. It reuses the same Password Safe request rather than opening a second one.
- A `[SSH key]` (DSS-managed) account has a private key and not necessarily a password, so
  it cannot supply a sudo password — the checkbox is disabled for those.
- A managed account works on the local and Azure (ACI) runners directly. On ECS or Cloud
  Run it needs *Ephemeral cloud secrets* enabled in Settings; an SSH-key secret works on
  every runner.

Choosing nothing keeps the auto-derived key. If that resolves to nothing, the stage now
fails saying so, rather than shipping an empty key file to the runner and surfacing as
`Permission denied (publickey)` — which reads like the host rejected a key rather than
like there was none.

What the build does, in order:

0. Creates the credential's Secrets Safe folder tree (`<root>/<lab>` under the safe) if it
   is absent — a pre-flight, before anything is touched. The identity playbook runs
   after the install and seed stages and writes into a folder it does not create, so without this a
   missing folder surfaces only after the server is installed and seeded, as an error that
   reads like a credential fault. The **safe** is never created: it carries its own ACL.
1. Opens `tcp/8081` and `tcp/8443` on the cloud ACL — an NSG rule on Azure, a VPC
   firewall rule plus an instance tag on GCP, a security-group permission on AWS — to the
   sources named in `spire_lab_source_cidrs`. **Blank opens nothing**, which is correct
   for a broker already inside the VNet and is the first thing to check otherwise.
2. The mode's install stages (see [Deployment modes](#deployment-modes-vm-docker-or-kubernetes)):
   `spire-server-install.yml` in `vm` mode — SPIRE under systemd, one trust domain, and
   `admin_ids`.
3. `spire-open-ports.yml`, with the same source set, so the two gates cannot disagree.
4. `spire-seed-entries.yml` — 11 entries, of which discovery should return **8**.
5. `spire-admin-identity.yml` — mints the admin credential into Secrets Safe.
6. `spire-oidc-provider.yml` (`vm` and `docker`; the chart runs it in `k8s`) — the OIDC
   Discovery Provider on `tcp/8443`, after which the lab registers its trust domain with
   the dashboard by JWKS URL.

Each playbook gets its own job row, so a failed stage's Ansible output is somewhere you
can read it; the page links every one. The first failure stops the sequence, because every
later stage asserts the server is up.

**Govern** writes the managed system. A trust domain nothing governs demonstrates SPIRE
rather than governance of machine identities, which is what this page is for — so the
deterministic half of the onboarding is a button, and the two parts that are not
deterministic are named rather than guessed at:

| | Who | Why |
|---|---|---|
| Asset + **Managed System** on `SPIFFE SVID` at port 8081 | **Govern** | deterministic: the address, the port and the platform are all known |
| **Functional Account** whose *name is a SPIFFE ID* (`spiffe://<trust-domain>/password-safe/admin`), PKCS#12 as its DSS key | you | its DSS-key field holds the administrative credential, and creating it here would mean the dashboard reading that credential out of Secrets Safe to push it back in. `spire-admin-identity.yml` writes it there under `no_log`; nothing in the app ever reads it |
| **`SpiffeTrustDomain`** attribute on the managed system | **Prepare probe** | the plugin takes its whole configuration from BeyondInsight *attributes*, and whether the gateway populates them for a plugin action **has never been observed**, which is the question this lab exists to answer. Setting the attribute is the probe's setup, not a bet on the answer. It is created if the tenant lacks it, assigned, and read back |
| **The answer**: Verify Functional Account's `Attributes received:` line | you | there is no API for running Verify Functional Account or reading its activity record. Record what the line said on the probe panel; the panel shows that answer's next step |

**No managed account is created, deliberately.** The plugin's accounts are SPIRE
registration entries and they arrive by *discovery*; one created here would sit beside
them, indistinguishable, and move the number the next section measures — the number that
caught a real plugin bug.

The **Onboarding** panel still resolves every value, and after Govern it shows what ran
and what is left. `SpiffeTrustBundlePem` comes from the **Bundle** button.

Then press **Prepare probe**, run *Verify Functional Account*, read the
`Attributes received:` line, and record it on the same panel.
[The standup runbook](../../../runbooks/spire-lab-standup.md) §5 is that procedure and what each
answer means.

## Deployment modes: VM, Docker or Kubernetes

The build form's **Deployment** select picks how SPIRE runs on the lab host. Everything
downstream — seed entries, the admin credential, Govern, the Kubernetes link, the agent
cell, JWT-SVID client assertions — is the same in all three.

| Mode | Install stages | then |
|---|---|---|
| `vm` (default) | `spire-server-install.yml` — SPIRE under systemd | ports, seed, identity, oidc |
| `docker` | `linux/install-docker.yml`, `spire-docker-server.yml` — `ghcr.io/spiffe/spire-server` and the OIDC Discovery Provider under Docker Compose | ports, seed, identity, oidc |
| `k8s` | `k3s/k3s-server-init.yml`, `spire-helm.yml` — k3s on the lab host and the `spiffe/helm-charts-hardened` charts (server, agent, CSI driver, controller manager, OIDC provider) | ports, seed, identity |

A mode whose playbooks are not on the Storage page is disabled in the select and names
them. The shared stages reach the server through a command prefix (`docker exec
spire-server` / `k3s kubectl exec … spire-server-0`), and in the container modes the admin
credential is minted to stdout and split on the host, so no key is written inside a
container. One mode per host: the Docker play refuses a host already running the `vm`-mode
systemd server.

**Every mode publishes the OIDC Discovery Provider on `tcp/8443`**, and the build opens it
on the cloud ACL and the host firewall to the same sources as 8081. After a successful
build the lab **registers its trust domain with the dashboard by JWKS URL** —
`https://<host>:8443/keys`, verified as `oidc.<trust-domain>` (the row's *TLS name*) against
the lab's own trust bundle — so the token endpoint verifies JWT-SVIDs with current keys. A
trust domain another lab or an operator already registered is never re-pointed.

**Refresh keys** on a lab's row re-reads the JWT and X.509 bundles, renews the provider's
serving certificate (`vm`/`docker`; the chart rotates its own) and updates the registration.
The dashboard also runs it hourly-checked, for any available lab whose registration is
older than a third of `ca_ttl`. If the URL cannot be fetched, a stored bundle is used as a
fallback.

**Destroy** closes 8081 and 8443 and deletes the lab's own trust-domain registration. The
host is still left alone unless you confirm **also remove SPIRE from the host**, which runs
`spire-remove.yml`: systemd units / `docker compose down -v` / `helm uninstall`, and deletes
`/opt/spire` including the CA key. k3s and Docker Engine stay installed.

**Nothing runs as root.** In every mode SPIRE runs the way upstream ships it:

| | Server | OIDC provider |
|---|---|---|
| `vm` | `spire` system user; systemd sandbox (no capabilities, `ProtectSystem=strict`, writable only `/opt/spire/data` and its socket directory, syscall filter) | `spire-oidc`, in the `spire` group only to reach the socket; same sandbox, writes nothing |
| `docker` | uid 1000 (the image's own user); read-only root, all capabilities dropped, `no-new-privileges`, pid limit | uid 1001 with group 1000; same restrictions |
| `k8s` | the chart's recommended settings (`global.spire.recommendations.enabled`): 1000:1000, read-only root, all capabilities dropped, no privilege escalation, RuntimeDefault seccomp; the `spire-server` namespace enforces the *restricted* Pod Security Standard | same |

The server makes its API socket `0770`, so the provider reaches it through the group and
nothing else can. The datastore and CA keys are `0700` to the server user; the provider's
serving key is root-owned and readable by the provider's group only. Binaries are
root-owned. `systemd-analyze security spire-server` scores the VM units.

**Every download is verified.** The plays pin, per SPIRE version, the SHA-256 of the
release tarballs (server and agent, `spire-extras` for the VM provider; amd64 and arm64,
checked against upstream's own `*_sha256sum.txt`), the Kubernetes exec credential plugin
(`k8s-spiffe-workload-jwt-exec-auth` 0.3.0, against its release `checksums.txt`) and the
digests of the
`ghcr.io/spiffe/*` multi-arch images, which the compose file runs as
`<image>:<tag>@<digest>` — Docker then refuses a re-pointed tag, or any local image with
that tag. A version with no pin is **refused**, not warned about: add its values to the
tables in the plays (a test holds all four plays and the lab's default version equal), or
set `spire_lab_allow_unpinned` to take it on TLS alone, deliberately. The charts are pinned
too (below). Helm itself publishes checksums only on `get.helm.sh`, so none is shipped:
set `spire_lab_helm_sha256` to verify it.

All of these are on **Settings → SPIRE Lab**: the chart and CRDs versions (blank = the pins),
the k3s version, extra chart values, the Helm checksum and **Allow unpinned downloads** (off).

**Upgrading SPIRE.** Change the SPIRE version on **Settings → SPIRE Lab** (and, for `k8s`,
the chart pins with it), then press **Upgrade** on the lab's row. It appears whenever the
lab's recorded release differs from the configured one, and the row shows both. It re-runs
the stages that install software, server before agents, as SPIRE requires:

1. the mode's install play — `spire-server-install.yml` restarts onto the new binary,
   `spire-docker-server.yml` recreates on the new pinned image, `spire-helm.yml` is a
   `helm upgrade` to the pinned chart and images;
2. the OIDC provider (vm/docker): each version installs into `/opt/spire/oidc/bin/<version>/`
   behind the symlink the unit runs, the link is re-pointed with a restart, and the old
   version is pruned only after the new one has served the discovery document;
3. a linked k3s node's agent — the entry stage first, since the agent play needs a fresh
   one-use join token.

Seeded entries, the admin credential, the trust domain and the ACL are kept. The first
failed stage stops the upgrade (an agent is never moved past a server that did not make
it); the lab stays available so it can be retried, and its row names the stages that
completed and the job holding the failure. A version the playbooks hold no checksum pin
for is refused. While an upgrade runs, **Refresh keys**, a second upgrade and **Destroy**
are refused; an auto-delete timer simply retries on its next pass.

The VM-mode provider also follows the configured version on its own at the next **Refresh
keys**, which re-runs its stage.

**What has been run:** the Docker mode, end to end, against images built from the 1.15.3
release binaries exactly as upstream's Dockerfile builds them — server, seed entries, the
OIDC provider as uid 1001 reaching the socket, the discovery document and JWKS over TLS,
a JWT-SVID minted by SPIRE and verified by the dashboard through the JWKS URL by address
and TLS name, and `spire-remove.yml`. That also confirmed the `x509 mint` stdout layout the
container modes split.

**The Kubernetes mode is pinned** to `spire` chart **0.30.2** and `spire-crds` **0.6.1**.
0.30.2 ships SPIRE 1.15.3, and no chart release ships a later SPIRE yet (0.30.3 still
carries appVersion 1.15.3), so the play sets the chart's server, agent and OIDC provider
images to `spire_lab_version` by digest: all three modes run the same server. That
override has been rendered, not run. `spire_lab_helm_chart_version` / `spire_lab_helm_crds_chart_version` override
the pin (blank falls back to it); move them together with `spire_version`. The play's
values were checked by rendering 0.30.2 with `helm template`: `adminIDs`, `caTTL` and the
SVID TTLs reach `server.conf`, the provider's domains include `oidc.<trust-domain>`, and its
serving certificate is issued for that name (the chart's default is
`oidc-discovery.<trust-domain>`, which the dashboard would refuse). Extra values go in
`spire_lab_helm_values_extra`.

**Not yet run:** the VM units' sandbox under a real systemd, and a Kubernetes build on a
real k3s node.

## By hand

Full detail, including what each playbook proves, is in
[`examples/playbooks/spire/`](../../../../examples/playbooks/spire/README.md). The playbooks are
cloud-agnostic — they configure a Linux host over SSH — so only step 1 differs between
Azure, GCP and AWS. Run them from Config Management on the **`ansible-winrm`** runner
image: `spire-open-ports.yml` needs `ansible.posix` and `spire-admin-identity.yml` needs
`beyondtrust.secrets_safe`, and neither is in `ansible-cloud`.
