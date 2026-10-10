# Container User

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are upgrading from a release that ran the dashboard as root, a job fails with "permission denied", or you are setting a security context (Kubernetes `runAsNonRoot`, `docker run -u`) on the dashboard container.

The dashboard image runs its app and its job worker as an unprivileged user,
`dashboard` (uid and gid **10001**), not as root. Earlier releases ran everything as root.

## How it starts

The image starts as root for a moment, then drops privileges before the dashboard runs.
`docker/entrypoint.sh` does three things as root, then switches to `dashboard`:

1. **Docker socket access.** The local Ansible runner starts sibling containers through
   `/var/run/docker.sock`. The host owns the socket as `root:<docker group>`, and that
   group's id differs from host to host (Docker Desktop's socket is `root:root`). The
   entrypoint reads the socket's group and gives it to `dashboard`, so you do not need to
   find the id and set `group_add` yourself.
2. **Files from older releases.** Earlier releases ran as root and left root-owned files
   in the places the app writes. The entrypoint hands these to `dashboard` on the first
   start after an upgrade:
   - `/app/terraform/deployments`
   - `/app/packer/builds`
   - `/run/spiffe-tokens`
   - `/runner-work`
   - the SQLite database in `/app`

   A directory that `dashboard` already owns is left alone, so later starts skip this.
3. **The dev SSH key**, if the Windows override mounts one. It is copied to
   `/home/dashboard/.ssh/dev_key` with the permissions ssh requires.

Then `setpriv` switches to `dashboard`, with no capabilities and `no_new_privs` set, and
runs gunicorn or the worker. Nothing the dashboard runs, and no job it starts, holds root.

## Upgrading

Nothing to change. Start the new image with your existing compose file and volumes, and
the entrypoint takes care of ownership.

One thing to expect: a Terraform state lock that an old root container left behind
belongs to `root@<container>`, and a new container is `dashboard@<container>`. The
cancel path never releases a lock from an earlier run anyway, so nothing changes. If you
need to clear such a lock, use the operator force-unlock as before.

## Starting it as non-root yourself

You can start the container as uid 10001 directly: `docker run -u 10001:10001`, or
Kubernetes `runAsUser: 10001` with `runAsNonRoot: true`. The entrypoint sees it is not
root and runs the command unchanged. In that case you handle what it would have done:

- **Docker socket:** if you mount one, add its group with `group_add` / `supplementalGroups`.
- **Volumes:** volumes written by an older release must already belong to 10001.

The managed cloud runtimes in [Cloud Hosting](cloud-hosting.md) mount neither, so this
works there as is.

## If something needs root again

Set `DASHBOARD_RUN_AS_ROOT=1` on the app and the worker to run them as root, as before.
This is a way back while you report the problem, not a setting to keep: please open an
issue with the error.

## Checking it

```bash
docker compose exec -u 10001 app sh -c 'grep ^Uid /proc/1/status'
```

The line should read `Uid: 10001 10001 10001 10001`. CI checks the same thing on every
pull request: the image job boots the container and fails if any process in it is not
uid 10001.
