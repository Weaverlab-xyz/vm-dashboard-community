# Setting up Rancher

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are standing up the Rancher management plane, or connecting one you already run.

Part of [Rancher Kubernetes Management](../rancher.md).

## Step 1 — Enable Kubernetes management

**Settings → Integrations → Kubernetes** → toggle on. A **Kubernetes (Rancher)**
tab appears on the **Containers** page.

## Step 2 — Configure the node

Open **Settings → Kubernetes** and fill in the **Rancher management node**
section. The first group applies whichever cloud the node runs on:

| Field | Notes |
|---|---|
| **Bootstrap password** | First-run admin password. The API token is minted from it and stored encrypted; you never re-enter it. |
| **Allowed source CIDRs** | *Optional / additive.* Comma-separated CIDRs for the node's ingress rule (tcp 80/443). Dashboard-provisioned clusters and the dashboard-managed Web-Jump Gateway are added automatically ([details](network-and-tls.md#automatic-firewall-whitelisting)); use this only for extra operator IPs and pre-existing operator Gateways. If nothing is set here **and** nothing is auto-discovered, the node stays closed unless *Allow open* is ticked. The panel shows the effective allow-list read-only. |
| **Readiness timeout (s)** | Default 360. How long the deploy waits for Rancher to serve after boot; raise it for a cold image pull or a slow disk. |
| **Verify TLS certificate** | Leave off for the node's self-signed cert; turn on only if you've put a real cert on it. |
| **PRA Web Jump to the Rancher UI** | Opt-in zero-trust access — see [PRA Web Jump](../rancher.md#pra-web-jump-optional). |

Below that, a **Settings for cloud** picker switches between one field group per
cloud — container image, machine size, VM name, disk size, and that cloud's
*Allow open* opt-in. Each cloud keeps its own, so trying the node somewhere else
doesn't mean re-entering everything.

> **That picker is a view control, not a setting.** Where the node actually runs
> is chosen on the deploy form and recorded from where it landed — so there is one
> source of truth for it, and it is the node itself.

Sizes must clear Rancher's ~4 GB floor: `e2-medium` on GCP, `t3.medium` on AWS,
`Standard_B2s` on Azure. Smaller types OOM, and the GCP path refuses the obvious
ones outright.

Settings apply immediately — no restart.

## Step 3 — Deploy the node

On **Containers → Kubernetes (Rancher)**, choose the **Cloud** and optionally a
**Region** (and a **Zone**, on GCP only) — see [placement](#placement-cloud-and-region)
— fill the **PRA access** fieldset (see below), then click **Deploy Rancher node**.
This enqueues a background job (follow it at `/jobs/{job_id}`) that:

1. Creates/updates the source-restricted ingress rule (`<node>-allow-mgmt`, tcp 80/443)
   — a firewall rule on GCP, a security group on AWS, an NSG rule on Azure.
2. Launches the VM with the privileged Rancher container and a public IP.
3. Pins the node's public IP as Rancher's `server-url`.
4. Waits for Rancher to come up, then bootstraps it and mints the API token.
5. **Completes Rancher's first-run wizard** (see below) so the UI is ready to log into.
6. If a PRA Web Jump was chosen, provisions it + vaults the admin credential.
7. If Entitle registration is enabled, registers the node in Entitle (best-effort).

When the job completes, the tab shows the node with a **RUNNING** status, a
clickable **URL**, and an **Access** hint. Open it (from an allowlisted IP, or via
the [PRA Web Jump](../rancher.md#pra-web-jump-optional)) and log in as `admin` with your
password (see below).

### PRA access (chosen at deploy)

Like the database and cloud-VM deploys, the deploy form offers **PRA pickers**
(populated from your PRA appliance) so you choose access at deploy/redeploy time:

- **Broker the Rancher UI via a PRA Web Jump** — tick to create a `rancher-ui`
  Web Jump. Then pick:
  - **Asset Group** and **Gateway** — where/through-what the Web Jump routes
    (blank → the `bt_*` / `rancher_ui_*` config defaults).
  - **Vault Account Group** — the PRA Vault group the generated **admin
    credential is stored in**, so PRA **injects** it into the Rancher login (you
    click the Web Jump and you're already logged in — the password never appears
    in the dashboard). Leave blank to skip vaulting (the password is surfaced in
    the *Access* hint instead).

The picks are persisted to config, so they're reused by later console opens.

### Placement (cloud and region)

The deploy form offers a **Cloud**, a **Region** and — on GCP only — a **Zone**:

- **Cloud** — `aws`, `azure` or `gcp`. Defaults to wherever the node currently is.
- **Region** — the dropdown lists only regions of the chosen cloud that have a
  configured subnet (the default region plus any region added via **Settings →
  Multi-region** or a per-region sandbox run). The node needs a public subnet and a
  subnet is regional everywhere, so a region without one can't host it. Blank keeps
  the node's **current** region.
- **Zone** — GCP only, and optional. Blank uses the region's **first available**
  zone (which correctly skips regions with no `-a` zone, e.g. `us-east1` starts at
  `-b`). If that zone is out of capacity (`ZONE_RESOURCE_POOL_EXHAUSTED`), the
  deploy **automatically retries a sibling zone in the same region**. The field is
  hidden on the other clouds because an EC2 subnet already pins its availability
  zone and Azure has no zone in this shape.

Rancher stays a **single** management plane: redeploying to a different region **or
a different cloud relocates** the node — the old VM is deleted first so no duplicate
`rancher-server` is stranded. The node is ephemeral, so state re-bootstraps and
imported clusters must re-import. Where it landed is recorded
(`rancher_node_cloud` plus that cloud's zone key), so teardown and later bare
redeploys stay put instead of drifting back to a default.

## Automatic first-run

A stock Rancher greets its first visitor with a **"Welcome to Rancher — enter your
bootstrap password"** wizard (set a new admin password, accept the EULA, answer
telemetry). On a **fresh** deploy the dashboard completes that for you
(`rancher_auto_first_run`, on by default) using the API token it just minted:

- **Sets the admin password.** Rancher **forbids reusing the bootstrap password**
  as the new admin password (*"must not be the same as the current password"*), so
  a distinct one is required. Set `rancher_admin_password` (≥12 chars) to choose it,
  **or leave it blank and the dashboard auto-generates a strong one**, stores it
  encrypted. When you chose a **Vault Account Group** at deploy, that credential is
  **stored in the PRA Vault** and injected by the Web Jump (never shown); otherwise
  it is **surfaced** in the Containers → Rancher panel's *Access* line + the job result.
- **Accepts the EULA and opts out of telemetry**, and clears the `first-login`
  flag — best-effort, version-tolerant.

The result: the operator opens the URL and lands straight on the cluster list.
**Log in as `admin`** with the password shown in the *Access* hint (change the
auto-generated one after first login). This runs **only on a fresh deploy** — a
reused/already-set-up node is left untouched; a generated password is cleared on
teardown so the next fresh deploy makes a new one. If it can't complete, the deploy
still succeeds and you finish the wizard by hand once (the job note says what
happened). Turn it off with `rancher_auto_first_run=false` for the manual wizard.
