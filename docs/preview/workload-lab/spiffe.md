# SPIFFE and SPIRE

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need a SPIRE trust domain to govern non-human identities against, and want it gone again when the demo ends.

This page is about the SPIRE trust domains the **Workload Lab** builds for demos, and the
Password Safe plugin that governs them. The dashboard's **own** SPIRE server, and every
feature that uses it, is on [SPIFFE and SPIRE](../../platform/spiffe.md).

> **Preview.** The plugin's own suite covers everything above the SPIRE API, and its
> 16-step harness runs green against a live SPIRE 1.15.3 server — but **nothing here has
> been proven through a live BeyondInsight**. The single open question is whether the
> gateway populates managed system and managed account *attributes* at all, and the
> whole configuration model turns on the answer. That is what this lab exists to settle
> cheaply. Off by default; enable it under **Settings → Preview features**.

This page is about the lab for the Password Safe **SPIFFE SVID** custom platform plugin,
which brings a SPIRE trust domain into BeyondInsight as a Managed System. **There is no
human in this workflow by design** — every identity here belongs to a machine, and the
consumer is a pipeline, not a person.

It is the **SPIRE** tab of the **Workload Lab** page (`/workload-lab`), alongside
[Certificates](certificates.md) — both labs govern identities that belong to machines, so
they share a page. Each still has its own preview toggle and its own Settings panel.

The lab has a second half, on this same tab:
[Reaching a Kubernetes cluster with a JWT-SVID](spiffe/kubernetes.md).
Everything else here is about identities that are *minted and vaulted*, which
[Minting is a deliberate downgrade](#minting-is-a-deliberate-downgrade-and-the-honest-version-matters)
is explicit about being a downgrade. That section is the un-downgraded path — a workload
attests itself and reaches a real relying party with a token stored nowhere.

The companion docs:

- [`examples/playbooks/spire/`](../../../examples/playbooks/spire/README.md) — the playbooks
  that build the lab, and what each one is actually proving
- [Config Management](../../operations/config-management.md) — how those playbooks get run
- [Cloud VMs](../../clouds/vms.md) — where the SPIRE server lives
- [Certificates](certificates.md) — the sibling feature, for a plugin that has a human
  approval in its path

---

## What the plugin does, in one paragraph

SPIRE is an issuance engine and a very good one. It is **not** an identity governance
system and does not claim to be: it has no inventory, no ownership model, no review
workflow, and `spire-server entry show` is a CLI dump on a box most of the people who
need the answer cannot log into. The plugin does two separate jobs with very different
risk. **Governance** discovers every registration entry as a Managed Account and raises
attestation-policy findings on every verification — it mints nothing and holds no
workload credential, so there is no trade-off to weigh. **Distribution** mints an
audience-scoped JWT-SVID for consumers that cannot run a SPIRE agent, and stores it as
the Managed Account credential.

Deploy the governance half first. It stands on its own.

**The dashboard now runs a SPIRE server of its own** (for remote agents). This lab keeps
its own server on purpose: its seeded weak entries must never go into a real trust domain.
Which lab demos could move to the dashboard's trust domain instead, and what else that
server should be used for, is in
[The dashboard as a SPIFFE workload](../../design/dashboard-workload-identity.md).

## Minting is a deliberate downgrade, and the honest version matters

Minting **bypasses SPIRE node attestation and workload attestation**. That is SPIFFE's
core security property, set aside on purpose. A vault-issued SVID is an exportable
bearer token at rest in a database rather than a non-exportable, attested,
continuously-rotating one — so **if a workload can reach the Workload API, it should use
the Workload API**, and using this plugin instead is a strict downgrade.

It is legitimate for three cases: consumers that genuinely cannot be attested
(mainframes, appliances, SaaS-hosted CI, partner systems), break-glass and debugging,
and a phased SPIFFE rollout. It is not legitimate for any workload that can be attested,
for any SPIFFE ID also issued to attested workloads, or for control-plane identities.
The plugin enforces that structurally: `SpiffeMintablePathPrefix` is unset by default and
**unset means refuse**, so minting is inert until an operator names the namespace that
may be minted.

## What the lab is for

Standing a SPIRE server up by hand takes an afternoon and produces something that lives
on one laptop and disappears on reboot. The plugin was developed against exactly that,
which is why every Password Safe interaction it documents is expected behaviour rather
than observed.

The lab replaces it with one Linux VM built from playbooks, seeded with the population a
real trust domain accumulates — weak selectors, inflated TTLs, a leftover `-admin` entry
— so the findings have something to catch. Unlike a private CA, the lab has **no standing
cost beyond the VM itself**, which the auto-delete timer already reaps. The reason it
still gets its own record and its own timer is different: a forgotten trust domain keeps
minting identities that relying services keep accepting, and it appears on no other page.

## Pages

The larger parts have a page each:

| Page | Read it when |
|---|---|
| [Building a SPIRE lab](spiffe/building.md) | you are building a SPIRE lab from the dashboard, as VMs, Docker or Kubernetes, or by hand |
| [Reaching Kubernetes with a JWT-SVID](spiffe/kubernetes.md) | you want a workload to authenticate to a Kubernetes API server with its SPIFFE identity, and to rehearse the two ways it fails |
| [Federating a SPIRE lab with the dashboard](spiffe/federation.md) | you want the lab's trust domain federated with the dashboard's own, and to prove its workloads across it |

## Two numbers to check, because both have already been wrong

**Discovery must return 8 of 11.** Not "discovery succeeded" — the count. The plugin
shipped with discovery defaulting its path filter to the mintable prefix, so configuring
minting silently narrowed the governance inventory to the vaulted namespace and the two
showcase findings disappeared, while every run still reported success. Read the exclusion
counts in the activity record: node/agent, admin, downstream, and outside-prefix are
reported separately, and that is what tells you which one bit.

**The admin credential is shorter than you asked for.** `ca_ttl` caps every SVID the
server issues, so `-ttl 720h` against the default 168h gives a ~7-day credential and
SPIRE says so rather than failing. Once it lapses, every action fails
`PERMISSION_DENIED`, which reads exactly like an `admin_ids` misconfiguration. Schedule
off the real expiry the playbook prints — or off the date the SPIRE tab shows, which
is the same number: the playbook publishes it, and the trust bundle, as text secrets
alongside the credential so the dashboard reads them as *values* rather than scraping a
job log. Both are public by construction; the credential itself never leaves Secrets
Safe.

## Settings

**Settings → SPIRE Lab**, plus the preview toggle `spire_lab_enabled` under **Settings →
Preview features**. Nothing here is a secret.

| Setting | Key | Default | What it does |
|---|---|---|---|
| Allowed source CIDRs | `spire_lab_source_cidrs` | blank | Who may reach tcp/8081, and 8443 and 8082 with it. Blank changes no cloud ACL: it does not open anything |
| SPIRE version | `spire_lab_version` | `1.15.5` | What every mode installs (for `k8s`, the chart's images), and what **Upgrade** moves to |
| CA TTL | `spire_lab_ca_ttl` | `168h` | Caps every SVID the server issues, the admin credential included |
| Admin SVID TTL | `spire_lab_admin_ttl` | `720h` | What the admin mint asks for; the CA TTL decides what it gets |
| Secrets Safe safe | `spire_lab_ps_safe` | `Automation` | Where the admin credential lands. It must exist already, with create rights |
| Folder root | `spire_lab_secret_root` | `spire` | Four titles land under `<root>/<lab>`: the PKCS#12, its passphrase, the trust bundle and the granted expiry |
| Playbook storage backend | `spire_lab_asset_backend` | blank: the active storage backend | Where a run fetches the `spire-*.yml` playbooks by filename |
| SPIRE chart version | `spire_lab_helm_chart_version` | blank: the pin, `0.30.2` | `k8s` mode. Never "latest" |
| SPIRE CRDs chart version | `spire_lab_helm_crds_chart_version` | blank: the pin, `0.6.1` | `k8s` mode |
| k3s version | `spire_lab_k3s_version` | blank: what get.k3s.io serves | `k8s` mode and the k3s link. At least 1.34; the playbook refuses older |
| Helm tarball SHA-256 | `spire_lab_helm_sha256` | blank, which warns | `k8s` mode. From `get.helm.sh/helm-v<ver>-linux-<arch>.tar.gz.sha256sum` |
| Extra chart values (YAML) | `spire_lab_helm_values_extra` | blank | `k8s` mode. Merged over the play's values, so it can undo them, hardening included |
| Allow unpinned downloads | `spire_lab_allow_unpinned` | off | Takes a SPIRE version the plays hold no checksum for, on TLS alone. Leave off |

Two more are read but not shown on the page. Set them with the headless import
(`POST /api/setup/import`) if a Password Safe admin has renamed something:

| Key | Default | What it does |
|---|---|---|
| `spire_ps_platform` | `SPIFFE SVID` | The platform name **Govern** looks for, if the imported `.psplugin`'s platform was renamed |
| `spire_ps_workgroup` | blank: `passwordsafe_workgroup` | The workgroup **Govern** creates the managed system in |

## Boundaries

- **No revocation.** Deleting a registration entry stops renewal; an SVID already in a
  consumer's memory stays valid until it expires. Containment is only as fast as the
  TTL. That is SPIRE's design, not the plugin's.
- **No kill switch.** The plugin implements neither Enable nor Disable Managed Account,
  so removing the Managed System — or the entry in SPIRE — is how an identity is stopped.
- **No self-rotation, permanently.** A workload cannot mint its own replacement: the
  server API requires an admin caller, and the Workload API is reachable only from the
  attested workload's own machine. The action is declared unsupported so BeyondInsight
  refuses it early.
- **No just-in-time issuance.** Password Safe returns what is stored rather than calling
  the plugin at retrieval time, so TTL planning is scheduling arithmetic:
  `jwt_svid_ttl >= rotation_interval + max_checkout_duration + clock skew`.
- **JWT-SVIDs only.** An X509-SVID is three artefacts and the credential field has
  nowhere to put the bundle.
- **No discovery of SVIDs already in circulation.** The plugin inventories registration
  entries, which is what SPIRE knows about. A JWT-SVID pasted into a config file two
  years ago is invisible to it and to SPIRE alike.
