# Personas

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to a specific role and want their story and click path rather than a feature list.

One page per role. Each covers what that person owns, the four-layer story in their
language (provisioning → PRA → Password Safe → Entitle), the use cases to run, which
integrations each needs, and how to talk to that buyer.

On an estate instance the in-app **Use cases** page is the same catalog, with each card
reporting whether *this* instance can actually run it. A POV instance shows that POV's own
checklist there instead — see [What the customer sees](../../pov/customer-access.md).

These pages are the narrative behind those cards; the registry that produces them is
`services/personas.py`, and `tests/test_persona_docs.py` is what stops the two drifting.

## A focus is an estate idea, and only an estate idea

A persona is a **presenting** decision: which role's story you lead with, on a day you
choose. So none of it exists on a POV instance — not the focus picker on the dashboard, not
the wizard's **Focus** step, not the **Focus** field on the RBAC Users and Groups tabs, and
not the nav reordering. `personas.applies()` answers false there and every resolver returns
neutral, so a focus cannot arrive from a cookie, a URL, a user row or an OIDC group either.
The APIs refuse a non-empty focus with a 409 rather than storing one nothing reads.

A POV instance is not presenting. It is one customer's evaluation of the products on their
own row; its dashboard leads with that POV and its checklist is grouped by the **product**
each use case proves (`services/pov_cards.py`). The role of whoever is in the room changes
nothing about what has been proved.

Note which axis does the subtracting: the **profile** removes those controls, exactly as it
removes the four cloud consoles. A persona still cannot hide anything. Every page an estate
instance reaches, a POV instance still reaches — in shipped order, which is what neutral has
always meant, and `/use-cases` stays ungated on both.

| Role | Owns |
|---|---|
| [Cloud Ops engineer](cloudops.md) | The cloud accounts — spend, footprint, and who can change what in them. |
| [DevOps engineer](devops.md) | The pipeline and the automation that runs against the estate. |
| [Hypervisor admin](hypervisor.md) | The on-prem virtualization estate: vSphere, Hyper-V, Proxmox, Nutanix, XCP-ng. |
| [IT engineer](itops.md) | The endpoints and the day-to-day access requests. |
| [OT / ICS engineer](ot.md) | The plant network — PLCs, the HMI, the historian, and the vendors who need in. |
| [DBA / data platform](dba.md) | The databases, their credentials, and who gets a session on them. |
| [Security / IAM analyst](security.md) | Who has access to what, and the proof of it. |
| [Platform / SRE](sre.md) | The clusters and the services running on them. |
| [Network / firewall admin](netadmin.md) | The routers and firewalls — the boxes every other demo assumes are already reachable. |
| [FinOps / cloud governance](finops.md) | The infrastructure nobody decided to keep, and the standing access that made it possible. |
| [AI / agent platform](aiops.md) | The things that act without a person at the keyboard, and whether you could stop one. |

`/docs` lists these in the same order for everybody. The profile-aware view is `/use-cases`,
behind the auth shell — the docs shell is public, so ordering it by the instance's chosen
focus would leak that focus to anyone who asks.
