# Deploying a Gateway host

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are deploying a Gateway host, choosing its region and naming it.

Part of [BeyondTrust Gateway Hosts](../gateways.md).

**Containers → Gateways** → the deploy row at the top of the panel:

| Field | Notes |
|---|---|
| **Cloud** | AWS / Azure / GCP. Changing it reloads the region list and re-suggests a name, because both are per-cloud. |
| **Region** | A picker, not free text. `(configured default)` leaves it blank — resolved at deploy time — which is what a single-region install wants. |
| **Zone** | **GCP only**, optional. Blank resolves from the region; the placeholder shows what that will be. |
| **Name** | Prefilled with a free, cloud-legal default (`gw-<region>-01`). Editable. |

**Deploy gateway** enqueues a `gateway_deploy` job and returns — the row appears
immediately as `provisioning` with a **Job** link. The work runs on the durable worker, so
a gunicorn recycle mid-deploy can't strand a half-built host:

| Step | What happens |
|---|---|
| Launching | Ensures the host under the name you gave. Passing a name is precisely what marks this gateway as *requested* — the ensure skips its managed-adoption write, because the row already exists and is owned by the job. |
| Recording placement | Writes back where the host **actually landed** (zone, egress IP), not what was asked for. |
| Updating node firewalls | Re-applies the Rancher + Portainer node allow-lists. Best-effort — a firewall hiccup logs and never fails the gateway job. |
| Done | Row flips to `running` with its host id. |

> **Why the row records where it landed rather than what you picked.** A blank GCP zone is
> resolved at launch, and a capacity-exhausted zone falls through to a sibling — so the
> requested zone is not reliably the real one. Teardown deletes by `(zone, name)`, so a row
> holding the requested zone would later fail to find its own host.

## Placement and the region picker

**The picker offers only the configured default region plus every region that has a
per-region config set of its own** (the same list the Rancher and Portainer node pickers
use — [`region_config.deployable_regions`](../../../web_dashboard/services/region_config.py)).
That restriction is the feature, not a limitation.

The tab used to take the region as free text, and typing an unconfigured region was **worse
than a validation error**. Every regional id a gateway needs — subnet, security group, ECS
cluster — resolves through `region_config.resolve_region`, which falls each field back to
the flat config keys. So the host came up **on the default region's network** while the row,
the job, and you all said otherwise. Nothing failed; the gateway was simply somewhere else,
with no line of sight to the private targets in the region you meant.

Restricting the picker makes *"the region I picked"* and *"the region it lands in"* the same
thing. To deploy somewhere new, add that region under **Settings → Multi-region** first;
it then shows up in the picker.

GCP has a second route to the same silent relocation: a gateway's **subnet is derived from
its zone**, so a zone in another region would quietly override the region you chose. The
deploy endpoint refuses that pair (HTTP 400) rather than queueing a job that lands
elsewhere, and picking a new region clears any leftover zone.

## Names, and why they matter more than they look

The name is not cosmetic — it is what keeps the managed and requested hosts apart **in the
cloud itself**, because the managed idle teardown finds its host *by name tag*. A requested
gateway wearing the managed name is one the idle teardown would terminate out from under
you. So:

- **Names must be unique within a cloud**, case-insensitively, across both kinds — including
  the managed name, which is reserved even if the auto-ensure has never run and so has no
  row yet.
- **Cloud rules apply.** GCP names must be RFC1035 (lowercase, digits, hyphens, leading
  letter) and ≤ 63 characters; AWS allows ≤ 255. Rejections are explicit rather than being
  silently rewritten — you typed the name and then have to find it in a cloud console.
- **Azure is capped at 15 characters**, not the 64-character Azure resource limit, because
  the in-guest hostname is derived as `name[:15]`. Two gateways differing only past the 15th
  character would share a hostname.
