# Workspace

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are giving someone a login for their day job (Cloud Ops, DevOps, DBA, ...) and want them to know where to start, or you have just been given one.

**Workspace** in the menu (`/workspace`) is a landing page for each role on an estate
instance. Use it when you do the job, not when you demo it. The [Demo hub](demo-hub.md) is
the presenter's page and [Use cases](personas/README.md) holds the scripts. A workspace is
for the person who looks after the clouds or the pipelines.

Login still lands on the **Dashboard**. A band near the top of the Dashboard shows your role
and how far you are through its getting-started steps. It links here, or, if you have no
role yet, asks you to pick one. **Dismiss** hides the band in that browser only.

## What is on it

- **Start here** is a short getting-started checklist. A step **ticks itself** once you
  have a completed job of the kind it describes. For example, *Deploy your first VM*
  ticks after your first successful cloud deploy, and *Make it recurring* ticks once you
  own a schedule for that kind of run. Only your own jobs count. Steps marked with a dot
  instead of a number, such as *Read the audit trail*, have nothing to run, so they never
  tick and do not count toward progress.
  A step that needs an integration that is off reads *Needs ...*. An administrator turns
  it on in **Settings**.
- **Your resources** lists the inventory items that belong to the role and that you can
  see. For Cloud Ops these are cloud VMs; for DevOps, VMs, clusters, functions and workload
  credentials. Each has a **Manage** link to the page that owns it. The rows come from
  the same reader as **Inventory**, with the same workgroup rule, and need the same
  `inventory:read` permission. Below the list are your most recent jobs for the role.
- **Toolkit** lists every page the role works from, grouped. It also shows the role's use
  cases from the Use cases page.

| Role | Getting started walks you through |
|---|---|
| Cloud Ops engineer | deploy a VM · stop one · bake a golden image · promote it · put a VM on a schedule · the guardrails |
| DevOps engineer | run a playbook · make it recurring · a cloud credential instead of a key · a cluster token instead of a kubeconfig · a function that fetches its secret · a cluster |
| Hypervisor admin | deploy on Proxmox · import an image · a Gateway · discover a site through an agent |
| IT engineer | a desktop pool · least privilege on Linux endpoints · one playbook across many machines · agents |
| OT / ICS engineer | an OT cell · a Gateway · discover a site through an agent |
| DBA / data platform | provision · onboard the admin account · register for JIT grants · stop out of hours |
| Security analyst | inventory · audit trail · event notifications · end dates |
| Platform / SRE | a cluster · bind your team's group · a private API server · a Rancher or Portainer node |
| Network / firewall admin | the network cell · a Gateway · what changed |
| FinOps / cloud governance | costs · stop what is idle · business hours · what has no end date |
| AI / agent platform | a SPIRE trust domain · a leased cloud credential · mint and revoke an agent · what it did |
| IAM engineer | a managed directory · directory health · audit privileged groups through an agent |
| Identity architect | map every directory · hybrid join · who can grant what · the evidence |

## Which workspace you see

The role is chosen in this order, the same order the Dashboard uses:

1. `?persona=<role>` in the URL. This affects that one page load and saves nothing.
2. A role you picked yourself, with **Switch role** here or the role picker on the
   Dashboard. The two share one setting, so picking a role here also reorders the Dashboard
   and the menu. **Use the role assigned to me** clears your pick.
3. The **Focus** an administrator set on your user, then the one on your identity-provider
   group (RBAC → Users / Groups).
4. The instance default from the setup wizard.

With none of these set, the page asks you to pick a role.

A role changes what the page shows first. It never changes access: anyone signed in can
open any role's workspace, and every list on it is filtered by that person's own
permissions. To change what someone can do, change their role in RBAC, not their
workspace.

## Onboarding a Cloud Ops or DevOps user

1. Create the user or map their identity-provider group in **RBAC**. Give it a role that
   lets them do the work, for example **Cloud Admin** for Cloud Ops, or
   **Platform / K8s** or **Operator** for DevOps. See
   [Permissions](../../admin/permissions.md).
2. Set **Focus** on the user or the group to *Cloud Ops engineer* or *DevOps engineer*. A
   group focus applies at each sign-in, so everyone who joins the group later gets it too.
3. Put them in the right workgroups, so their resources appear under **Your resources**.

At first sign-in they land on the Dashboard. The band there links to their workspace, and
the **Workspace** link is pinned second in their menu.

## Where it exists

Estate instances only, like the Demo hub. On a POV instance `/workspace` and
`/api/workspace/summary` return 404 naming the profile, and the menu link and Dashboard
band are absent. A POV instance has no roles to choose between: its dashboard leads with
the POV.

The registry is `services/workspaces.py`: one entry per role in `services/personas.py`. A
step names the job types that tick it, and `tests/test_workspace.py` checks every one
against the job types the code actually creates.
