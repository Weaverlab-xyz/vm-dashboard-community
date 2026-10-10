# On-premises directories

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to manage an on-premises AD domain or LDAP directory through a remote agent, extend your own domain to AWS or GCP so cloud servers join it, or have those servers Entra hybrid joined.

Part of [Directories](../directories.md).

An on-premises AD domain or LDAP directory is reached through a
[remote agent](../remote-agents/enrolment.md). From the **Directories** page you can:

- **discover** domain controllers and LDAP servers;
- **register** them, or **import** them from Password Safe;
- **change** them with AD and LDAP playbooks;
- **extend** an on-prem domain to AWS (an AD Connector) or to GCP (a DNS link), so cloud
  servers join the domain you already run, and optionally become
  [Entra hybrid joined](#hybrid-entra-join).

## Discover, register and change them

This needs remote agent **2.8.0** or later, on the network the directory is on.

**Discover.** Run a **Directories** scan from the agent's **Discover** dialog
([scan details](../remote-agents/enrolment.md#5-discover)). The scan reads the anonymous
rootDSE on 389 and 636 and never binds. Scope it by domain name to find every DC. Each
finding has a **Register directory** link that opens the form below, pre-filled.

**Register.** Use **Directories → Register on-prem**:

- AD domain or LDAP name;
- host and port, with LDAPS on by default;
- base DN (derived from the domain for AD);
- the agent that reaches the directory;
- a **Password Safe managed account** to bind as;
- for LDAP, which **LDAP server** it is: OpenLDAP, PingDirectory, the Okta LDAP
  Interface, 389 Directory Server or FreeIPA, or left generic. A discovery finding fills
  this in from the server's `vendorName` (PingDirectory reports Ping Identity).

The dashboard stores only the account's ids and name. The credential is checked out per
run, for that run only, and never written here.

**Okta's LDAP Interface.** Pick **Okta LDAP Interface** and enter the org name (`acme` or
`acme.okta.com`). The rest is fixed by Okta and filled in for you:

- host `acme.ldap.okta.com`;
- LDAPS on 636;
- base DN `dc=acme,dc=okta,dc=com`.

The Password Safe account's name is used as the bind DN as given, so store it in the
form Okta's LDAP Interface documentation gives for a bind (a `uid=<login>,…` DN under
your org's base DN). Users are under `ou=users` and groups under `ou=groups`. Turn the LDAP Interface on in the Okta admin
console first. It is **search-only**, so only `ldap-search.yml` runs against it; any
other playbook is refused before the run starts. To change group membership, register
the org as an [Okta identity provider](identity-providers.md) instead.

**PingDirectory** is an ordinary LDAP server here: every LDAP playbook works against it.
Its password policy lives in `ds-pwp-*` attributes, which `ldap-attrs.yml` can set like any
other.

**Import from Password Safe.** **Directories → Import from Password Safe** lists the AD and
LDAP directories Password Safe already manages, with their domain, port, SSL setting and
the accounts the dashboard can request. Pick each row's account and agent. A row that cannot
be imported says why: no requestable account, no host, or a domain that is not a DNS name.
A PingDirectory platform imports with its vendor set.

Platforms for **Entra ID, Okta and PingOne** are listed too, but cannot be imported as-is:
Password Safe holds their API credential but not the tenant id or app registration.
**Complete registration** opens the
[identity provider dialog](identity-providers.md) with that system's account already
chosen as the credential.

Importing changes nothing in Password Safe. It needs `secrets:use` as well as
`directories:write`.

**Change it.** In Config Management, choose the directory under **On-Prem Directories (via
agent)**, and pick **LDAP** or **WinRM**
([how the run works](../remote-agents/config-runs.md#on-premises-directories)).
[`examples/playbooks/directory/`](../../examples/playbooks/directory/) has playbooks for
these tasks:

- AD users, groups and OUs; password resets; removing stale computer objects;
- LDAP entries, attributes, group membership and passwords;
- read-only audits: stale accounts, privileged group membership and stale computers.

The agent's `policy.yaml` must list the directory's LDAP port, and 5986 for WinRM, under
`ansible.targets`.

**Unregister** only forgets the row. It is refused while an AD Connector or DNS link
extends the directory.

## Connecting on-prem AD to the cloud

Extending your own domain to the cloud is usually cheaper than running domain controllers
there. What you pay for is the network path, and what you give up is resilience to a WAN
outage. Approximate list prices per month; check each cloud's pricing page, and add data
egress:

| Option | What runs in the cloud | Approximate cost | Logons during a WAN outage |
|---|---|---|---|
| VPN + **AWS AD Connector** (built here) | A proxy to your DCs, with no DCs of its own | Small about $36, plus the VPN | No |
| VPN + **GCP DNS link** (built here) | A DNS forwarding zone | about $0.20, plus the VPN | No |
| **VyOS site link** + DNS link (built here, GCP) | One small VyOS VM; WireGuard to your VyOS | about $4–16, no managed VPN | No |
| VPN only; servers join your DCs directly | Nothing | The VPN only | No |
| VPN + one **read-only DC** (RODC) in the cloud | One small Windows VM | about $60, plus the VPN | Yes; changes still need the WAN |
| VPN + your own writable DCs in the cloud | Two Windows VMs | about $150–250 with the VPN | Yes |
| Managed AD (with a trust if you need one) | The cloud's DCs | $150–600 | Yes |

The cloud side of the VPN:

- AWS Site-to-Site VPN: about $36 per connection, with two tunnels.
- GCP HA VPN: about $73 for two tunnels.
- Azure VPN Gateway: VpnGw1 about $140 with BGP; Basic about $27, without BGP.

### Using VyOS

**VyOS on-prem, with the cloud's managed VPN on the other end, is the recommended cheap
and reliable setup.** VyOS is free on hardware or a VM you already have. The cloud side
gives you two tunnels to separate endpoints. BGP over both fails over by itself when a
tunnel or an endpoint goes down.
[`examples/playbooks/network/vyos-ad-site-vpn.yml`](../../examples/playbooks/network/vyos-ad-site-vpn.yml)
configures it:

- IKEv2 with AES-256-GCM, DH group 20 and PFS. The pre-shared keys come from Password Safe.
- The cloud learns only your DC subnets, and VyOS accepts only the cloud's networks.
- A forward firewall lets the cloud networks reach the DCs on the AD ports only, and drops
  everything else.

**VyOS at both ends** (a small cloud VM, about $4–16/month; the
[network demo cell](../profiles/demo/net-demo-cell.md) image can serve) is the cheapest option. It is also a
single point of failure in the cloud, so use it for labs and POVs, not for a domain that
production servers depend on. On GCP the dashboard builds that end for you:

### A VyOS site link, built for you (GCP)

Use **Connect GCP network (VyOS)** on a registered on-prem AD. It builds one small VyOS VM
on GCP and a **WireGuard** tunnel to your on-prem VyOS 1.4 router:

- **The cloud end, built and configured here.** `terraform/directory/gcp_vyos_peer` creates
  the VM (`e2-small` about $12/month, or `e2-micro` for a lab), a static external IP (about
  $3.65/month), a VPC route to the peer for each on-prem subnet, and firewall rules for
  WireGuard (UDP 51820), SSH from the VPC, DNS, and WinRM from on-prem. The dashboard's GCP
  Ansible runner then configures WireGuard, the routes and **DNS forwarding** for the domain
  over SSH to the peer's internal address
  ([`vyos-wireguard-peer.yml`](../../web_dashboard/services/builtin_playbooks/vyos-wireguard-peer.yml)).
- **The on-prem end, done by you.** On your router run
  `generate pki wireguard key-pair install interface wg0` and paste the **public** key into
  the form. After the build, **On-prem commands** gives you the lines to paste back: the
  tunnel address, the peer's public key and address, `persistent-keepalive 25`, and the
  routes to the VPC. Your router dials out, so it needs no static IP and no inbound port.
- **Keys.** Your router's private key never leaves it. The peer's key pair is generated
  here; its private key is written to **GCP Secret Manager** and reaches the runner through
  its secret-env channel only. It is never in instance metadata, Terraform state, the job
  or the directory row. Destroy deletes it.
- **Status means the tunnel works.** The link is `available` only after the peer pings a
  domain controller through the tunnel. Until your end is up it is `awaiting_onprem`; paste
  the commands, then use **Check link**, which re-applies the configuration and probes again.

Then use **Extend to GCP** for a DNS link. It pre-fills the peer's internal address as the
DNS server: the peer answers for the domain from inside the VPC, so Cloud DNS never has to
reach on-prem itself. Joins then run as described below. Destroying the site link is refused
while a DNS link resolves through it.

What it needs:

- the `vyos-cell` image baked from VyOS **1.4** with `VYOS_RUNNER_PUBKEY` set to the public
  half of the GCP Ansible key (`gcp_ssh_key_secret_name`). See
  [provisioners/net/README.md](../../provisioners/net/README.md);
- the GCP Ansible runner with direct VPC egress into the peer's network, and Secret Manager
  in the runner's project;
- the runner image with paramiko (`chrweav/ansible-winrm` from this release), which
  `network_cli` needs;
- your on-prem firewall to allow the tunnel (`10.255.255.0/30`) and the VPC ranges to reach
  the DCs on the AD ports below.

The AD ports to allow from the cloud networks to the DCs:

- TCP and UDP 53 (DNS), 88 (Kerberos), 389 (LDAP) and 464 (password change);
- UDP 123 (time);
- TCP 135 (RPC), 445 (SMB), 636 (LDAPS) and 3268–3269 (Global Catalog);
- the RPC dynamic range, TCP 49152–65535, unless your DCs pin RPC to fewer ports.

The VPN encrypts all of it. Several of these protocols are not encrypted on their own.

## Extending to AWS: an AD Connector

Use **Extend to AWS** on a registered on-prem AD. Give it:

- the region and VPC, and two subnets in different Availability Zones;
- the IP addresses of on-prem DNS servers, normally the DCs, reachable over the VPN;
- a size: Small, about $36/month, or Large, about $110/month.

The dashboard creates the connector through the Directory Service API, not Terraform, so
the service account's password never lands in Terraform state. The password comes from
the directory's Password Safe account, is used once, and is not stored.

**Turn off automatic rotation for that account in Password Safe.** AWS has no API to update
a connector's copy of the password, so a rotation breaks the connector until someone
updates it in the AWS console. The account needs only read rights and the right to join
computers.

When the connector is Active it appears in the AWS deploy form's **Join Active
Directory** picker. Joining works as for any AWS directory, through seamless domain join.
A connector that fails keeps its AWS id, so **Destroy** can delete it. Destroy is refused
while servers are joined.

## Extending to GCP: a DNS link

GCP has no AD Connector. Use **Extend to GCP** on a registered on-prem AD to build a
**DNS link**: a Cloud DNS private forwarding zone (about $0.20/month) that sends the
domain's queries from the VPC networks you name to your DNS servers over the VPN. Cloud DNS
sends those queries from 35.199.192.0/19, so your firewall and VPN routes must allow that
range to reach the DNS servers on port 53.

Choose the DNS link in the GCP deploy form's **Join Active Directory** picker:

- At first boot, the server opens a WinRM HTTPS listener (5986).
- When the deploy completes, the dashboard queues a join on the directory's own agent. The
  join runs the built-in
  [`ad-join-computer.yml`](../../examples/playbooks/directory/ad-join-computer.yml): it
  logs on as the server's stored local administrator and joins as the directory's Password
  Safe account, checked out for that run.
- No domain credential goes into instance metadata.
- The deploy job links to the join job (`ad_join_job_id`). A join that cannot be queued is
  a warning on the deploy (`ad_join_error`).

For this to work:

- the agent's `ansible.targets` must cover the server's subnet on 5986;
- the VPC firewall must allow 5986 from the agent's side of the VPN.

**For resilience**, add one read-only domain controller in GCP, on a small Windows VM, so
logons keep working when the VPN is down. The dashboard does not build one. Promote it
with your usual AD tooling, and point the DNS link at it first.

## Hybrid Entra join

If **Entra Connect** synchronises your on-premises domain to Entra ID with hybrid join
configured, an AWS or GCP Windows server joined to that domain can become **Microsoft Entra
hybrid joined**. It is then both domain joined and known to Entra ID, with no domain
controller in the cloud. The domain join is the one set up above, through an
[AD Connector](#extending-to-aws-an-ad-connector) on AWS or a
[DNS link](#extending-to-gcp-a-dns-link) on GCP, over your VPN.

**Declare the domain.** **Hybrid join** on the on-prem AD row records two things:

- that Entra Connect syncs this domain for hybrid join;
- the OU servers should join. It must be in Entra Connect's sync scope, or the computer
  object never reaches Entra ID.

The AD Connector and DNS link that extend the domain inherit both. This is only a
declaration: nothing in Entra Connect, the service connection point or the sync scope is
changed from here. The AD picker on the deploy forms marks these directories
*Entra hybrid*.

**Deploy.** On the AWS or GCP deploy form, pick the AD Connector or DNS link under **Join
Active Directory** and **Hybrid join** under **Microsoft Entra ID**. The server joins the
domain as before, into the declared OU unless the deploy names another. A follow-up job
(`windows_hybrid_check`) then checks Microsoft Graph for a device of the server's name with
`trustType` `ServerAd`, every 5 minutes for up to 90 minutes. The result is recorded on the
deploy job as `entra_hybrid_state`:

| State | Meaning |
|---|---|
| `joined` | Entra ID has the device as hybrid joined (`entra_device_id`) |
| `pending` | It had not appeared when the check gave up. The note says what to check |
| `unverifiable` | The dashboard's Azure identity cannot read devices: grant it the Graph application permission **Device.Read.All** |

A hybrid join that is not confirmed is never a failed deploy: the server is domain joined
either way.

**What has to be true on your side:**

- Entra Connect 1.1.819 or later, with **hybrid join configured**, which creates the
  service connection point in the forest;
- the OU in Entra Connect's **sync scope**, and the default device attributes synced;
- the server reaching `enterpriseregistration.windows.net`, `login.microsoftonline.com` and
  `device.login.microsoftonline.com` on 443;
- patience: a sync cycle runs about every 30 minutes, and the server's device-registration
  task registers it after the computer object syncs.

**Destroy** does not remove the server's computer object from AD, so the synced device stays
in Entra ID until it is deleted on-premises. Run
[`ad-remove-computer.yml`](../../examples/playbooks/directory/) through the domain's remote
agent to remove it.
