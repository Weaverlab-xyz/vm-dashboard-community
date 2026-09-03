# Personas

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting to a specific role and want their story and click path rather than a feature list.

One page per role. Each covers what that person owns, the four-layer story in their
language (provisioning → PRA → Password Safe → Entitle), the use cases to run, which
integrations each needs, and how to talk to that buyer.

The in-app **Use cases** page is the same catalog, with each card reporting whether *this*
instance can actually run it. These pages are the narrative behind those cards; the
registry that produces them is `services/personas.py`, and `tests/test_persona_docs.py` is
what stops the two drifting.

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

`/docs` lists these in the same order for everybody. The profile-aware view is `/use-cases`,
behind the auth shell — the docs shell is public, so ordering it by the instance's chosen
focus would leak that focus to anyone who asks.
