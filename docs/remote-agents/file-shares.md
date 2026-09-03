# Agent-brokered file shares

> **Audience:** operator · **Profile:** `both` · **Read this when:** an agent has to read a file share the dashboard cannot reach.

Part of [Remote Agents](../remote-agents.md). A name rather than a path, the grants, and why it needs no Docker socket.

## Agent-brokered file shares

The dashboard's storage backend — where its playbooks, scripts and inventories live —
can be a directory or a corporate UNC share on **this** network, with the agent doing the
reading and writing.

It exists because the dashboard's own "Local Filesystem / UNC" backend opens the SMB
socket from inside its own container, which means it only works when the dashboard is
already on the same network as the share. Most are not: a dashboard on Azure Container
Apps has no route to a file server, and a [POV instance](../profiles/pov/README.md) has no cloud
provider to fall back on, so it has nowhere to put a playbook at all. The agent is
already inside; this makes it the path.

Full setup, fields and limits: [Storage Management → Remote Filesystem / UNC](../storage-management.md#remote-filesystem--unc-via-agent).

### The dashboard holds a name, not a path

`shares.yaml` — [example](../../examples/remote-agent/shares.example.yaml) — maps a name to
a path and, for UNC, a credential. Mount it `:ro,Z` beside `policy.yaml`:

```yaml
shares:
  - name: playbook-share
    path: /srv/playbooks
  - name: corp-automation
    path: \\fs01.corp.example.com\automation\playbooks
    username: svc-dashboard
    password: ...
    domain: CORP
```

The same split as `connections.yaml`, and the same reason. The dashboard stores an agent
and a share **name**; the path and the credential never leave this host. There is no path
field anywhere in the job protocol, so a UNC path to some other server is not refused —
it is unsayable. A job names a share you already wrote down plus a bare filename, and
both ends independently refuse a name containing a separator or a leading dot.

### Four grants, all required

```yaml
job_types:
  - agent_storage          # 2

storage:                   # 3
  enabled: true
  shares:
    - name: corp-automation
      write: true          # 4 — read is implied, write is not
```

1. the dashboard grants this agent the `agent_storage` job type (Agents page);
2. `agent_storage` is in `job_types:`;
3. the `storage:` block is enabled and lists the share, whose name also matches an entry
   in `shares.yaml`;
4. `write: true`, if the dashboard should be able to upload or delete.

Withhold any one and nothing happens. **Write defaults to off** — naming a share grants
reading it, and the common case is a share somebody else populates. Without the flag, an
upload or a delete is refused by name into Live Output.

Note what is *not* consulted: `targets:` at the top of the file. That grants a port probe
on a network, which has nothing to say about which directory may be read — the same
separation `ansible.targets` has, and for the same reason.

### It needs no Docker socket

The mildest of the four job types. It starts no container and executes nothing; it opens
files. UNC shares are read with `smbprotocol`, a pure-Python SMB client in the agent
image, so there is no host-side mount and no `cifs-utils`. An operator who would rather
bind-mount the share can: a non-UNC `path:` takes the plain filesystem branch and never
touches SMB.

### Requires an agent image of 2.5.0 or newer

The dashboard refuses to **queue** for an older one, for the same reason as Config
Management below — an older agent has no `agent_storage` handler and would refuse the job
into Live Output, where it reads as a `policy.yaml` problem. It matters more here than
elsewhere because storage is called from the request path: a job that will be refused
still costs a full round trip of waiting before the page shows an error.

Files move inside the signed job envelope, capped at 256 KB — about 190 KB of file.
Playbooks and scripts fit; packages do not, and the dashboard says so before queueing.
