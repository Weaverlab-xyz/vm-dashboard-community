# `chrweav/hypervisor-runner`

A **one-shot** container the [remote agent](../agent/README.md) launches per job, which
does one hypervisor operation and exits.

It exists because of a restraint elsewhere: the agent image installs `requests`,
`PyYAML` and `cryptography` and nothing else, and two tests enforce that. The restraint
*is* the security argument — the long-lived supervisor imports no execution machinery —
so it is not traded away for a capability. Four of the five hypervisors need nothing
more, because Prism v3, Proxmox's `/api2/json` and the vSphere Automation REST API are
plain JSON and XAPI is stdlib `xmlrpc`.

Two are not reachable that way, and only these two live here:

| Kind | Why it can't be done in the agent | Dependency |
|---|---|---|
| `hyperv` | WinRM is SOAP with NTLM/Negotiate, and hand-rolling NTLM is a large amount of security-critical code | `pywinrm` + `requests-ntlm` |
| `esxi` | A **bare** ESXi host serves the SOAP API only; the Automation REST API is vCenter-only | `pyvmomi` |

Everything else — vCenter, Proxmox, Prism, XCP-ng — is handled by the agent directly and
must not be added here. A second implementation of the same thing is a second thing to
keep correct.

## You pull this, not the dashboard

Like the agent and unlike the other images under `runners/`, this one is
**operator-pulled**:

```bash
docker pull chrweav/hypervisor-runner:latest
```

The agent will not pull it for you, deliberately — a pull is a network fetch of
executable content, and that is the operator's decision, not a job's. If it is absent
the agent says so (`the sibling image … is not present on this host`) rather than
reaching out.

Launching it also needs the **Docker socket**, which is root on the host. That cost is
documented rather than hidden: the agent does not mount the socket by default. Opt in
with [`examples/remote-agent/docker-compose.sibling.yml`](../../examples/remote-agent/docker-compose.sibling.yml)
and name the image in the `sibling:` block of your `policy.yaml`.

Full setup, the four required grants, and troubleshooting live in
[`docs/remote-agents.md#the-sibling-runner`](../../docs/remote-agents.md#the-sibling-runner) —
this file does not restate them.

## The contract

Reads its whole instruction from the **environment**, prints one JSON object on stdout,
and exits. Nothing is read from argv, so there is no place for a shell to expand
anything; nothing is read from a file, so there is nothing to point at.

| | |
|---|---|
| Kinds | `hyperv`, `esxi` (`HV_KIND`) |
| Verbs | `inventory_sync`, `power_on`, `power_off`, `power_reset`, `restart`, `shutdown` (`HV_VERB`) |
| Credential | `HV_PASSWORD`, read once at start and then cleared from the environment — so it never appears in `ps` on the host |
| Output | exactly one JSON object: `{"ok": true, …}` or `{"ok": false, "error": …}` |
| Runs as | uid 10001, unprivileged |

There is no `snapshot` verb (it needs per-kind APIs this runner has no path to) and no
`reboot`: Hyper-V has no graceful-reboot cmdlet — `Restart-VM` is documented as a hard
restart, which is already what `power_reset` does — and ESXi's graceful path is
`restart` (`RebootGuest()`). The agent refuses `reboot` for Hyper-V *before* a job
reaches this container, so the operator reads why instead of "unknown verb", which would
look like a version mismatch.

A failure is always reported on stdout, never inferred from an exit code and an empty
pipe.

The agent additionally sets `--read-only`, drops every capability and disables privilege
escalation on the container it creates. Those are applied at create time, not baked into
this image, so they cannot be removed by rebuilding it with a different `USER` line.

`pywinrm[kerberos]` is deliberately not installed: Kerberos needs a system krb5 stack,
and the dashboard's Hyper-V integration defaults to NTLM.

## Build it yourself

```bash
docker build -t chrweav/hypervisor-runner:latest runners/hypervisor
```

CI publishes it multi-arch (`amd64`/`arm64`) from
[`.github/workflows/publish-images.yml`](../../.github/workflows/publish-images.yml) on
a version tag.
