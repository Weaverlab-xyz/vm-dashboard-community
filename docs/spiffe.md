# SPIFFE and SPIRE

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you need a SPIRE trust domain to govern non-human identities against, and want it gone again when the demo ends.

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
they share a page. Each still has its own preview toggle and its own Settings panel. The
page's third tab, **Kubernetes access**, argues a pattern nothing here builds yet: see
[the design note](design/workload-k8s-short-lived-token.md).

The companion docs:

- [`examples/playbooks/spire/`](../examples/playbooks/spire/README.md) — the playbooks
  that build the lab, and what each one is actually proving
- [Config Management](config-management.md) — how those playbooks get run
- [Cloud VMs](cloud-vms.md) — where the SPIRE server lives
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

## Building it from the dashboard

The **Workload Lab** page's **SPIRE** tab (preview; enable `spire_lab_enabled` under Settings → Preview
features) does steps 1–5 below as one job. Configure it under **Settings → SPIRE Lab**,
and upload the four `spire-*.yml` playbooks on the **Storage** page first — a run
fetches assets *by filename from the storage backend*, never from `examples/`, and the
build form names the ones that are missing. Storage is where assets are uploaded; Config
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

- All four playbooks run with `become`, and the form has no separate sudo credential, so
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
   is absent — a pre-flight, before anything is touched. The identity playbook is the
   *last* of the four and writes into a folder it does not create, so without this a
   missing folder surfaces only after the server is installed and seeded, as an error that
   reads like a credential fault. The **safe** is never created: it carries its own ACL.
1. Opens `tcp/8081` on the cloud ACL — an NSG rule on Azure, a VPC firewall rule plus an
   instance tag on GCP, a security-group permission on AWS — to the sources named in
   `spire_lab_source_cidrs`. **Blank opens nothing**, which is correct for a broker
   already inside the VNet and is the first thing to check otherwise.
2. `spire-server-install.yml` — SPIRE under systemd, one trust domain, and `admin_ids`.
3. `spire-open-ports.yml`, with the same source set, so the two gates cannot disagree.
4. `spire-seed-entries.yml` — 11 entries, of which discovery should return **8**.
5. `spire-admin-identity.yml` — mints the admin credential into Secrets Safe.

Each playbook gets its own job row, so a failed stage's Ansible output is somewhere you
can read it; the page links all four. The first failure stops the sequence, because every
later stage asserts the server is up.

**The dashboard does not write the Password Safe objects.** The plugin takes its whole
configuration from BeyondInsight *attributes*, and whether the gateway populates those
for a plugin action has never been observed — so a writer built now would be betting on
the answer. The page's **Onboarding** panel resolves every value instead:

- an Asset for the host, and a Managed System on the `SPIFFE SVID` platform at port 8081;
- a Functional Account whose **name is a SPIFFE ID**
  (`spiffe://<trust-domain>/password-safe/admin`), carrying the PKCS#12 as its DSS key —
  the panel names the two Secrets Safe titles holding it and never reads them;
- `SpiffeTrustDomain` on the managed system, and `SpiffeTrustBundlePem` from the
  **Bundle** button.

Then run *Verify Functional Account* and read the `Attributes received:` line.
[The standup runbook](runbooks/spire-lab-standup.md) §5 is that procedure and what each
answer means.

### By hand

Full detail, including what each playbook proves, is in
[`examples/playbooks/spire/`](../examples/playbooks/spire/README.md). The playbooks are
cloud-agnostic — they configure a Linux host over SSH — so only step 1 differs between
Azure, GCP and AWS. Run them from Config Management on the **`ansible-winrm`** runner
image: `spire-open-ports.yml` needs `ansible.posix` and `spire-admin-identity.yml` needs
`beyondtrust.secrets_safe`, and neither is in `ansible-cloud`.

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
