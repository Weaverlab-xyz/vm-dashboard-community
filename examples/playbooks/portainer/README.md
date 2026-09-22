# Portainer

Configuring Portainer itself from Config Management: the teams just-in-time access is
granted through, the environment access policies that make those teams mean something,
the stacks on your Docker hosts, and registering a new host as an environment.

The operator-facing write-up is
[docs/integrations/portainer.md](../../../docs/integrations/portainer.md). This file is
the quick reference.

## Two kinds of play in one directory

| | Target to pick | What it is |
|---|---|---|
| Everything except the Edge play | **Portainer** | `hosts: localhost` — the runner calls the REST API. Nothing is installed anywhere. |
| `portainer-edge-env-ensure.yml` | **a VM** | Two plays: the first creates the environment over the API, the second installs the agent **on that host**. |

**Portainer** is a target family of its own in the run form — no id, no SSH user, no
cloud. It appears whenever the integration is enabled and a URL and API token are
stored. The connection (`PORTAINER_URL`, `PORTAINER_PAT`, `PORTAINER_VERIFY_SSL`) is
injected into every runner, so no play takes a credential as a parameter, and the token
is in the job's scrub set.

Which runner a Portainer run lands on is `ansible_runner_portainer`, falling back to
`ansible_runner`. It matters for a **managed** node: that node's firewall is fail-closed
and admits the dashboard's own egress, not a transient in-cloud runner's — see
[Portainer targets](../../../docs/integrations/ansible/kubernetes-runner.md#portainer-targets).

## The files

| File | Purpose |
|---|---|
| `list-endpoints.yml` | Read-only smoke test — every environment and its online status |
| `portainer-team-ensure.yml` | Create the teams JIT access is granted through (`team_name` / `team_names`) |
| `portainer-env-access.yml` | Give a team standing access to an environment (`team_name` + `endpoint_id`/`endpoint_name`, `state: present\|absent`) |
| `portainer-jit-prereqs.yml` | Team **and** access policy in one run — everything the JIT adapter needs before it can be paired |
| `portainer-edge-env-ensure.yml` | Register a Docker host as an Edge environment and install the agent on it |
| `deploy-stack.yml` | Create **or update** a compose stack |
| `stack-remove.yml` | Remove a stack; a missing stack is a no-op, not a failure |
| `prune-containers.yml` | Reclaim disk — prune stopped containers, optionally images/volumes |

> `prune-containers.yml` is destructive, and `prune_volumes: true` deletes any volume
> not attached to a container. It is off by default; opt in deliberately.

## Setting up just-in-time access

The `portainer_access` adapter publishes **one Entitle asset per team** and grants by
adding a freshly minted account to one. So two things have to exist before the adapter
can be paired from the Portainer page, and a fresh managed node has neither:

1. a **team** — with none, the adapter reports itself unconfigured and registration is
   refused;
2. that team's **access to an environment** — with none, the asset exists and grants
   access to nothing.

`portainer-jit-prereqs.yml` does both:

```
team_name: Platform
endpoint_names: ["local"]      # or all_environments: true
```

The team name is the role code a requester picks in Entitle, so name it the way it
should read there.

## Traps these plays encode

- **`PUT /api/endpoints/{id}` replaces the access-policy map.** Portainer assigns the
  payload's `TeamAccessPolicies` straight over the stored one, so a PUT naming only the
  team being granted silently revokes every other team's access. The plays read the
  environment first and merge.
- **`POST /api/endpoints` is multipart form, not JSON.** A JSON body comes back as a
  missing-name validation error that says nothing about the cause. The tag field is
  also spelled `TagIds`, and must be valid JSON rather than an empty string.
- **`RoleId: 0`** is what CE writes: per-role RBAC is a Business Edition feature. On BE,
  pass `team_role_id`.
- **The Edge key encodes the server URL it was minted against.** A managed node takes an
  ephemeral external IP, so recreating it invalidates every key issued before the
  change — which is why the Edge play mints one per run instead of taking a stored key,
  and derives the agent id from the URL and the environment name so a re-run rejoins as
  the same agent rather than adding a second one.
- **`EDGE_INSECURE_POLL=1`.** The managed node serves a self-signed certificate on 9443;
  without this the agent's first poll fails verification and the environment simply
  never comes up.
- **Every API task is `no_log`** — the token rides the request headers. That censors a
  failed write's own detail too, which is why each mutating play re-reads and asserts
  on what it finds.

`tests/test_playbook_portainer.py` pins all of the above, and renders the merge and
matching expressions against sample data.
