# Identity Architect

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to whoever designs the identity plane: how the directories connect, where privilege is granted, and how standing access is designed out.

Owns the **design**, not the directories. Which domains exist and how they trust each other.
Which identity provider is the source of truth for whom. How on-prem identity reaches the cloud.
Where privileged access is granted and how it ends. The IAM engineer runs what this person
specifies.

Their problem is that the design and the estate drift apart. The architecture document says
"no standing privilege" and "one forest". The estate has a shared admin account, a domain
controller someone stood up in a cloud account, and a service principal with Owner rights that
nobody remembers granting.

## Why they care

An identity design is judged by its exceptions. Every standing admin, every tier-0 asset outside
the plan and every non-human identity with permanent rights is somewhere an attacker can skip the
design entirely. The architect needs two things: the design enforced by the tooling people
already use, and a way to show where it holds.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Domain joins, AD Connectors and hybrid join are built to one pattern, not by hand per server. |
| **PRA** | The tier-0 path is brokered and recorded rather than a network route. |
| **Password Safe** | Every directory credential has a custodian and a rotation history. |
| **Entitle** | Privilege is a grant with an end time, for people, for the dashboard's own scopes, and for its cloud identities. |

## Use cases

### Every directory, and what grants access to it, on one page

The Directories page holds cloud and on-prem domains beside Entra ID, Okta and PingOne. Each row
can be pinned to the Entitle integration that grants access to it. The identity estate reads as a
map of sources and grant paths rather than a list of consoles.

**Guide:** [Directories](../../../preview/directories.md)

### Extend the forest to the cloud without a domain controller there

Join AWS and GCP servers to the on-prem domain through an AD Connector or a DNS link over your
VPN. Then declare the domain as Entra hybrid and confirm each server's hybrid join from Microsoft
Graph. That is one forest with no new domain controller in a cloud account, which means no new
tier-0 asset to protect.

**Guide:** [On-premises directories](../../../preview/directories/on-premises.md)

### Zero standing privilege, including in the tool itself

Every dashboard scope, administrator included, can be requested through Entitle. The grant is
revoked the moment it ends, through the REST mechanism. The access model the architect writes
down is applied to the console that enforces it.

**Guide:** [Entitle dashboard permissions](../../../beyondtrust/entitle-dashboard-permissions.md)

### Machine identities elevated per action, not per year

The machine-identity track replaces the dashboard's standing cloud credentials with role grants
from Entitle. Each grant is scoped to one operation and lasts about fifteen minutes. It applies
the human rule to a non-human identity. The design document includes the threat model and the
per-cloud trade-offs an architect will ask about.

**Guide:** [Cloud identity JIT](../../../design/cloud-identity-jit.md)

### Who can change the directory, and who did

The `directories` permission scope decides who may write, and the audit log records each
membership change (`directory_member_add`, `directory_member_remove`) and each Entitle pin
(`directory_entitle_pin`). An identity governance review gets the evidence without an export.

**Guide:** [Audit Log](../../../admin/audit-log.md)

## What to enable

Turn on **Directories** under Preview features, and **Entitle**. Then add:

- **Remote agents** for the hybrid card, with an agent on the on-prem domain's network;
- **Entitle user JIT** for the zero-standing-privilege card;
- the machine-identity settings on the Entitle panel for the machine-identity card.

Directories is estate-owned, so the cards that target it are masked on a
[POV instance](../../pov/README.md). The two Entitle cards and the audit card work on both
profiles.

## Talking to this buyer

They think in diagrams and exceptions, so do not lead with a click path. Open the Directories
page and ask them to point at what is missing from it. Then show where privilege enters the
picture: Entitle for people, the dashboard's own scopes, and the machine-identity track. Close on
the audit card, because "how do you prove it" is the question their design review always ends
on.

Risk analysis across the identity estate, such as attack paths, over-privilege and identities
correlated across directories, is the job of BeyondTrust Identity Security Insights. If they ask
for it, that is the product to bring in, not a gap in this demo.
