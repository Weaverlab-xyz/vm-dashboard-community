# Portainer: importing from another Portainer

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are moving users, teams and registries from an existing Portainer into the one the dashboard drives.

Part of [Portainer](../portainer.md).

Merges **users, teams, team memberships and registries** from another Portainer into the
configured one. Existing names are matched rather than duplicated, so re-importing the
same bundle is a no-op.

## What a Portainer backup can and cannot do

Portainer's own **Backup** produces a `tar.gz` of its `/data` volume. Two limits matter:

- It **only restores into a pristine instance** with an empty data volume, during first
  run. A managed node initializes its admin at container start (to dodge the
  [init-timeout lockout](managed-server.md#minting-a-token-later)), so it is *never* pristine — a `.tar.gz` cannot be
  restored into one, and the dashboard will not pretend otherwise.
- It never covered what is *deployed* on your environments — containers, volumes,
  images. Portainer's docs say so explicitly.

So the archive is opened where it already lives, and the dashboard imports a small
reviewable JSON bundle instead.

## Step 1 — turn a backup into a bundle (on your own machine)

Skip to step 2 if the source Portainer is still running — point the exporter straight at
it.

```bash
docker run -d --name portainer-scratch -p 9443:9443 portainer/portainer-ce:latest
curl -k -X POST https://localhost:9443/api/restore \
     -F "file=@portainer_backup.tar.gz" -F "password=<only if encrypted>"
python -m web_dashboard.scripts.portainer_migrate export \
     --url https://localhost:9443 --username admin --insecure --out bundle.json
docker rm -f portainer-scratch
```

The restore must be the **first** thing that scratch instance is asked to do — Portainer
closes its first-run window a short time after the container starts.

The exporter is stdlib-only, so it needs no virtualenv. `inspect` reviews a bundle
offline:

```bash
python -m web_dashboard.scripts.portainer_migrate inspect --bundle bundle.json
```

## Step 2 — import it

**Containers → Portainer → Import from another Portainer** → choose the `.json`. This
enqueues a `portainer_import` job; follow it at `/jobs/{job_id}`.

Optionally pick an environment under **Deploy stacks onto**. Be deliberate: Portainer has
no "save a stack without running it" API, so this **deploys** the bundle's stacks and
starts containers.

## What does not come across

| Not migrated | Why |
|---|---|
| **Environment connections** | They address a local Docker socket or a LAN host this node cannot route to. The bundle records them under `reference` so you can see what existed; re-establish them as [Edge agents](../portainer.md#connect-a-docker-host-edge-agent). |
| **User passwords** | Portainer's API never returns them, and the bundle scrubs credential-shaped fields regardless. Imported users get fresh generated passwords, reported **once** in the job result. |
| **Registry credentials** | Same reason. A registry is recreated with its name and URL but **unauthenticated**, and the job names each one that needs its password re-entered. |
| **Administrator role** | An imported user is always created as *standard*, even if the source had it as an administrator — a bundle is a hand-editable file from another server. The job says which users were downgraded; promote them in Portainer deliberately. |
| **The node's own `admin`** | Skipped outright. The dashboard holds that credential; colliding with it is how you lose access to the node. |
| **Anything deployed on the environments** | Portainer's backup never covered this either. |
