# SPIRE lab samples (`spire/`)

The lab for the Password Safe **SPIFFE SVID** custom plugin: a SPIRE Server on one
Linux VM, seeded with registration entries worth governing, plus the administrative
credential the plugin authenticates with.

Feature reference: [docs/spiffe.md](../../../docs/spiffe.md).

**These playbooks are cloud-agnostic.** They configure a Linux host over SSH, so the
only thing that changes between Azure, GCP and AWS is how the VM is created and how its
network ACL is opened — see [Opening the port](#opening-the-port).

| File | Target | Runner image | What it does |
|---|---|---|---|
| `spire-server-install.yml` | Linux VM (SSH) | `ansible-winrm` | SPIRE 1.15.3 under systemd: one trust domain, sqlite datastore, `join_token` attestor, `admin_ids` |
| `spire-open-ports.yml` | Linux VM (SSH) | `ansible-winrm` | Opens tcp/8081 on the **host** firewall (firewalld or ufw) |
| `spire-seed-entries.yml` | Linux VM (SSH) | `ansible-winrm` | The 11 registration entries the demo is built on |
| `spire-admin-identity.yml` | Linux VM (SSH) | `ansible-winrm` | Mints the admin X509-SVID, packs a PKCS#12, writes it into Password Safe |

All four are `--syntax-check` clean against `chrweav/ansible-winrm`, which is the image
to use: `spire-open-ports.yml` needs `ansible.posix`, and `spire-admin-identity.yml`
needs `beyondtrust.secrets_safe`. Neither is in `chrweav/ansible-cloud`, which is the
Kubernetes/database image.

## The order to run them in

> The dashboard's **SPIRE** page (preview: `spire_lab_enabled`) does all five steps below
> as one job, on Azure, GCP or AWS, and opens the cloud ACL first — see
> [docs/spiffe.md](../../../docs/spiffe.md#building-it-from-the-dashboard). It runs these
> same files, fetched **by filename from the storage backend**, so upload them on the
> **Storage** page first (Config Management only *runs* them); the build form names the
> ones it cannot find. The steps
> below are the by-hand path and the explanation of what each one is for.

1. **Deploy a VM** from the normal cloud page, on a subnet the Password Safe worker or
   Resource Broker can reach. Ubuntu 22.04 on a small shape is plenty.
2. **`spire-server-install.yml`**, passing `trust_domain`. This is the only required
   variable, and it is the one the Managed System models.
3. **`spire-open-ports.yml`**. Then open the cloud ACL too — that is a separate gate.
4. **`spire-seed-entries.yml`**, same `trust_domain`.
5. **`spire-admin-identity.yml`**, passing `admin_secret_folder`. Copy the two Secrets
   Safe values it names into the Password Safe functional account, and the printed
   trust bundle into the managed system's `SpiffeTrustBundlePem`.

## What each one is actually proving

**Discovery returning 8 of 11 is the assertion, not "discovery succeeded".** The three
exclusions — one node/agent entry, one `-admin` and one `-downstream` — are the point.
Vaulting an agent identity would be a category error, and control-plane privileges
sitting in a workload entry are almost always a leftover, so the counts are reported
separately in the activity record. A number that climbs is worth a look.

This is not a hypothetical. The plugin shipped with discovery defaulting its path
filter to `SpiffeMintablePathPrefix`, so configuring minting silently narrowed the
inventory to the vaulted namespace — 2 accounts instead of 7 — and the run still
reported success, because the only assertion was that the action finished. **Assert the
count.**

**`admin_ids` is the line people get wrong**, so `spire-server-install.yml` writes it.
Setting `-admin` on a registration entry grants admin rights to SVIDs the *agent*
issues for that entry; it does nothing for an SVID produced by `spire-server x509 mint`,
which is exactly how the functional account credential is made. A missing `admin_ids`
surfaces much later as `PERMISSION_DENIED` on *Verify Functional Account*.

**The credential moves in one direction only.** `spire-admin-identity.yml` writes the
PKCS#12 and its passphrase straight into Password Safe with
`beyondtrust.secrets_safe.secrets_create` under `no_log`, the same way
[`k3s/k3s-kubeconfig.yml`](../k3s/k3s-kubeconfig.yml) moves a cluster-admin kubeconfig.
There is no output-as-value channel in this runner — a job's "output" is a captured log
— so a private key must never come back that way. The job log gets the Secrets Safe
titles and nothing else. The passphrase is generated *on the VM*, so it never passes
through the run form or your shell history.

**The trust bundle is printed on purpose.** It is what every consumer of the trust
domain has to trust, in the same sense as a CA chain — public by construction. It is
also embedded in the PKCS#12 by `-certfile`, so the credential carries its own chain.

## Opening the port

The plugin dials `<host>:8081` over gRPC with mutual TLS. Two gates, and the cloud one
is usually the one that bites:

| Cloud | The cloud gate |
|---|---|
| Azure | The VM's Network Security Group — an inbound allow on tcp/8081 |
| GCP | A VPC firewall rule on the network, targeting the VM's network tag |
| AWS | An inbound rule on the VM's security group |

**Terminating TLS in front of it does not work.** Anything that terminates TLS strips
the client certificate, and the SPIRE server rejects the call as unauthenticated. The
symptom looks like a credential problem and is not. If you need a proxy, it must pass
through.

A closed ACL and a closed host firewall present identically: a gRPC timeout on *Verify
Functional Account*. Check both before suspecting the credential.

## Boundaries these samples do not cross

- **No agent, and no workload attestation.** The plugin only ever talks to the server
  API, so the lab does not run one. The node entry exists to parent the workload
  entries and to give discovery something to exclude. That also means these samples
  demonstrate the *governance* half honestly and the *attested issuance* half not at
  all — which is correct, because minting through Password Safe deliberately bypasses
  attestation.
- **No revocation.** Deleting a registration entry stops renewal; an SVID already in a
  consumer's memory stays valid until it expires. Containment is only as fast as the
  TTL. That is SPIRE's design, not the plugin's — and re-running
  `spire-admin-identity.yml` with `remint=true` does not revoke the old credential
  either.
- **Not a production topology.** One server, sqlite, a disk key manager, no upstream
  CA, no HA. It is built to be thrown away.
- **`ca_ttl` caps the admin credential.** `-ttl 720h` against the default 168h gives a
  ~7-day credential, and SPIRE says so rather than failing. The real expiry is printed;
  schedule off that number.
