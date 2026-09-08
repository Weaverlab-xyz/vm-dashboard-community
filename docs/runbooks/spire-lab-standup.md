# SPIRE lab standup + the attribute probe (Azure)

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are standing up the SPIFFE SVID plugin's lab on a cloud VM and need to settle whether BeyondInsight populates plugin attributes.

Operator runbook for putting a SPIRE trust domain on an Azure VM and onboarding it into
Password Safe against the **SPIFFE SVID** custom plugin. **§5 is the point of the whole
exercise** — it answers the one question the plugin's configuration model rests on, and
it takes about two minutes once §1–§4 are done.

Feature reference: [SPIFFE and SPIRE](../spiffe.md).
Playbooks: [`examples/playbooks/spire/`](../../examples/playbooks/spire/README.md).

**Scope: Azure.** The playbooks are cloud-agnostic — they configure a Linux host over
SSH — so GCP and AWS differ only in §1 and §2. Those sections name what changes.

> **Nothing below has been run.** The plugin is proven against a live SPIRE server (83
> unit assertions, a 16-step harness) and against nothing else. Every Password Safe
> interaction here is expected behaviour, not observed. That is what §5 exists to fix,
> and it is why it comes before any dashboard code that assumes an answer.

---

## 0. Before you start

- **The Resource Broker must be able to reach the VM on tcp/8081.** The plugin dials the
  SPIRE server API with mutual TLS, so it needs a route in. Note which subnet the broker
  sits on — you need its egress address in §2.
- **Check which subscription you are on.** `Microsoft.Compute` is *unregistered* in
  **SE-Prod** and cannot be registered at resource-group scope, so no VM can be created
  there at all — and SE-Prod is the default in a fresh `az login`. The VM belongs in the
  subscription the dashboard's Azure integration targets, which needs `Microsoft.Compute`
  and `Microsoft.Network` both `Registered`. Confirm before deploying:

  ```bash
  az provider show -n Microsoft.Compute --subscription "<sub-id>" --query registrationState -o tsv
  ```

  A `MissingSubscriptionRegistration` failure on deploy is this, not a quota or a
  permission problem.
- **A Secrets Safe safe to write into**, with create rights. §4 stores two secrets in it.
  `examples/playbooks/password-safe/onboard-safe-and-account.yml` creates one.
- Decide the trust domain name now. It is baked into the server config, every SPIFFE ID,
  and the Managed System. `weaverlab.test` is what the plugin's own lab uses.

## 1. Deploy the VM

Cloud VMs → **Deploy**, on the Azure tab. Ubuntu 22.04, smallest shape that runs a Go
binary and a sqlite file — `Standard_B1s` is enough. Put it on a subnet the broker can
reach, and set an auto-delete timer.

Nothing SPIRE-specific happens here: it is an ordinary VM, so it already gets the
auto-delete timer, ref-counted NAT and Password Safe VM onboarding.

**Pass:** the VM reaches `running` and Config Management lists it as a target.

> **GCP / AWS:** the same step on the GCP or AWS tab. Everything from §3 onwards is
> identical.

## 2. Open tcp/8081 — both gates

There are two, and they fail identically.

**The cloud gate.** On the VM's Network Security Group, add an inbound allow for
tcp/8081, sourced from the broker's address rather than `*`. 8081 is an API that mints
identities.

**The host gate.** Config Management → run `spire-open-ports.yml` against the VM's IP,
runner image **`ansible-winrm`**, with:

```
spire_source_cidr: <the broker's CIDR>
```

**Pass:** from the broker's network, `nc -vz <vm-ip> 8081` connects. A refusal is the
host firewall; a hang is the NSG.

> **Do not terminate TLS in front of it.** Anything that terminates TLS strips the
> client certificate and the SPIRE server rejects the call as unauthenticated. The
> symptom reads as a credential problem and is not.
>
> **GCP:** a VPC firewall rule targeting the VM's network tag. **AWS:** an inbound rule
> on the VM's security group.

## 3. Install and seed SPIRE

Both runs use Config Management, target kind **VM**, the VM's IP, runner image
**`ansible-winrm`**.

`spire-server-install.yml`:

```
trust_domain: weaverlab.test
```

**Pass:** the play ends with `SPIRE 1.15.3 serving trust domain 'weaverlab.test' on
:8081` and a clean `spire-server healthcheck`.

`spire-seed-entries.yml`, same `trust_domain`.

**Pass:** `11 registration entries`. Not 10 — if you see 10, one create was refused;
the play prints the failure rather than swallowing it.

## 4. Mint the administrative credential

`spire-admin-identity.yml`:

```
trust_domain: weaverlab.test
admin_secret_folder: spire/weaverlab
ps_safe: Automation
```

**Pass:** the play names three things and prints no key material —
`spire/weaverlab/admin-pfx-b64`, `spire/weaverlab/admin-pfx-pass`, and the trust bundle
PEM. **Grep the job output for `MII`**; if it appears, the credential leaked into the
log and something is wrong with the `no_log` guards.

Note the printed **SVID expires** line. `ca_ttl` caps the request, so a `-ttl 720h` ask
becomes ~7 days. Once it lapses every action fails `PERMISSION_DENIED`, which reads
exactly like an `admin_ids` problem — so put that date somewhere.

## 5. Onboard, and read the one line that matters

In BeyondInsight:

1. **Asset** for the VM.
2. **Functional Account** on the `SPIFFE SVID` platform. Its **name is a SPIFFE ID**,
   not a username: `spiffe://weaverlab.test/password-safe/admin`. DSS key = the text
   secret `spire/weaverlab/admin-pfx-b64`; DSS passphrase = `spire/weaverlab/admin-pfx-pass`.
   Paste both from Secrets Safe.
3. **Managed System** on the same platform, port **8081**, using that functional
   account. The managed system inherits its platform from the account, so an account on
   the wrong platform onboards green and then fails every action.
4. Define an attribute type and set **`SpiffeTrustDomain` = `weaverlab.test`** on the
   managed system.
5. Run **Verify Functional Account** and open the activity record.

**The line to read:**

```
Attributes received: system=[...] account=[...]
```

| What you see | What it means | What happens next |
|---|---|---|
| `system=[SpiffeTrustDomain]` | The gateway populates attributes. The configuration model works as designed. | Build the attribute writer in `ps_api_service`. |
| `system=[SpiffeTrustDomain]` but the value is truncated | Short attributes work; a 1.8 KB PEM will not fit one. | The trust bundle needs another home — see below. |
| `system=[]` | The gateway does not populate attributes at all. | The whole configuration surface moves onto the managed system address, like the Certificate and k8s plugins. That is a plugin change. |

**Also record, while you are here:**

- Did BeyondInsight accept **`~`** in a managed account name (`vaulted~partner~acme-etl`)
  and **`@`** in an audience label? `!` is no longer in question — SPIRE's own path
  grammar makes it unreachable.
- Does `SpiffeTrustBundlePem` hold a full PEM? It is ~900 bytes for one authority and
  ~1.8 KB across a CA rotation. If it does not fit, the cheapest fix needs no new field
  at all: `spire-admin-identity.yml` already embeds the bundle in the PKCS#12 via
  `-certfile`, so the plugin could read it from the credential it already holds.

**Pass:** *Verify Functional Account* returns success, names the trust domain, and the
`Attributes received:` line is legible. A `PERMISSION_DENIED` here is almost always
`admin_ids` — but check the SVID expiry from §4 first.

## 6. Prove discovery, and check the number

Run **Discovery Accounts**.

**Pass: 8 accounts.** Not "discovery succeeded" — the count. Read the exclusion counts
in the activity record: `1 node/agent, 2 admin/downstream, 0 outside path prefix`.

If you get 2, discovery is filtering on the mintable prefix rather than the discovery
prefix — the defect fixed in plugin **1.1.0.2**. Check which package is installed
before looking anywhere else.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| gRPC timeout on Verify Functional Account | Reachability, not credentials. Check the NSG and the host firewall separately — §2. |
| `PERMISSION_DENIED` / `Unauthenticated` | The functional account's SPIFFE ID is not in `admin_ids`, or its SVID expired. An entry-level `-admin` does **not** apply to a minted SVID. |
| `SPIRE Server certificate could not be validated` | `SpiffeTrustBundlePem` is unset. A SPIRE server presents only its leaf, so there is nothing in the handshake to chain against and fingerprint pinning cannot substitute. |
| `did not chain to the configured trust bundle` | The bundle is stale — SPIRE rotated its CA. Re-run `spire-server bundle show` and update the attribute. |
| `Connected to trust domain 'X' but the managed system is configured for 'Y'` | The managed system points at the wrong server. Deliberately fatal. |
| Discovery returns fewer accounts than expected | Read the exclusion counts; each category is reported separately. |
| `Minting is blocked because SpiffeMintablePathPrefix is not set` | Working as designed. Minting is inert until an operator names the mintable namespace. |
| The credential appeared in a job log | Stop and fix the playbook. `spire-admin-identity.yml` must never print it — `tests/test_playbook_spire.py` pins that. |
