# Action Guardrails (pre-action policy)

Action Guardrails evaluate a deploy request against [Open Policy Agent](https://www.openpolicyagent.org/)
(OPA) Rego policies **before the job is created** — so a disallowed deploy never
starts. It's the *pre-action* half of policy: it blocks. (Contrast with *post-apply*
compliance scanning, which only records findings after the fact — not part of the
community edition.)

Off by default. Turn it on in **Settings → Integrations → Action Guardrails**.

> **You decide, not the dashboard.** Out of the box this enforces *nothing* — the
> feature is disabled, no actions are gated, and every built-in policy is inert
> until you give it a value. There are two ways to use it, both operator-owned:
> set the no-Rego knobs below (regions / sizes / freeze days), and/or **bring your
> own Rego** — drop `.rego` files in the policy dir, or point `ADMISSION_POLICY_DIR`
> at a folder you mount (works on the published image with no rebuild). The bundled
> policies are conveniences you opt into, not defaults imposed on your users.

## The model

```
POST /api/aws/deploy ─► validate params ─► [GUARDRAIL] ─► create job ─► terraform/SDK
                                              │
                                              ├─ allow → proceed
                                              └─ deny  → 403 + audit, no job created
```

The gate runs at the **service layer**, right after request parameters are parsed
and before the job is created (the point of no return). It's evaluated for these
actions:

| Action | Fires on |
|---|---|
| `aws:ec2:deploy` | `POST /api/aws/deploy` |
| `azure:vm:deploy` | `POST /api/azure/deploy` |
| `gcp:gce:deploy` | `POST /api/gcp/deploy` |
| `clouddb:provision` | `POST /api/databases` |
| `k8s:provision` | `POST /api/k8s/clusters/provision` |

Each request is turned into a policy **input** document:

```json
{
  "action": "aws:ec2:deploy",
  "actor":  { "username": "alice", "is_admin": true },
  "request":{ "region": "eu-west-1", "instance_type": "t3.large", "image": "ami-…", "name": "web-1" },
  "limits": { "allowed_regions": ["us-east-1"], "denied_instance_types": [], "prod_window": ["sat","sun"] },
  "now":    { "iso": "2026-07-03T14:00:00", "weekday": "fri", "hour": 14 }
}
```

`request` is normalized across clouds: `region` is the target region/location/zone,
`instance_type` is the size/class (EC2 type, Azure `vm_size`, GCE machine type, DB
class/sku, node type). `limits` is injected from your Settings (below), and `now`
is computed by the dashboard so policies stay free of timezone math.

## Enabling it

**Settings → Integrations → Action Guardrails**, then set:

- **Gated actions** — which actions to enforce, e.g. `aws:ec2:deploy, clouddb:provision`.
  Only listed actions are gated; everything else is untouched. Blank ⇒ inert even
  when enabled.
- **Allowed regions** — allow-list; blank ⇒ no region restriction.
- **Blocked instance types** — block-list of sizes/classes.
- **Change-freeze days** — weekdays (UTC) on which deploys are frozen, e.g. `sat,sun`.

All list fields accept a comma-separated string or a JSON array. Changes take effect
immediately (no restart) — they're read live from config on each deploy.

## Deny behavior

A denied deploy returns **HTTP 403** with the reasons:

```json
{ "detail": { "error": "policy", "reasons": ["region \"eu-west-1\" is not in the allowed list [\"us-east-1\"]"] } }
```

and writes an `<action>:denied` entry to the **tamper-evident audit log**
(see [`/api/audit/verify`](secrets-management.md)). No job is created and no cloud
resource is touched.

## Fails closed

The guardrail uses the OPA binary bundled in the container image. If the gate is
**on** but OPA is unavailable (e.g. a dev container that wasn't rebuilt), gated
deploys are **denied** (403) rather than silently admitted. Un-gated actions and
the disabled state are unaffected. `OPA_BINARY` overrides the binary path;
`ADMISSION_POLICY_DIR` overrides the policy directory.

## The built-in policies

Policies live in `terraform/policy/admission/` and ship in the image. Each is a
Rego file declaring `package admission.<rule_id>` with a `deny` set of
human-readable strings; any non-empty `deny` blocks the action and its strings
become the caller's `reasons`.

| Policy | Denies when | Driven by |
|---|---|---|
| `allowed_regions.rego` | target region not in the allow-list | `admission_allowed_regions` |
| `instance_size_caps.rego` | requested size/class is blocked | `admission_denied_instance_types` |
| `prod_window.rego` | the current UTC weekday is frozen | `admission_prod_window` |

Each policy is inert when its limit is empty, so you can enable the feature and turn
on one control at a time.

## Writing your own policy

Drop a `.rego` file into `terraform/policy/admission/` (rebuild the image, or mount
it and point `ADMISSION_POLICY_DIR` at it). Read from `input` and emit `deny`
strings:

```rego
package admission.no_gpu_in_dev

import rego.v1

deny contains msg if {
	input.request.name != ""
	startswith(input.request.name, "dev-")
	contains(input.request.instance_type, "p4d")
	msg := "GPU instances are not allowed for dev-* deploys"
}
```

Policies are versioned in-repo and reviewed like code. To cap by size *class* rather
than an exact list, replace the exact-match check with a prefix/regex rule.

## What this is not

- **Not** the async human **approval gate** (two-person sign-off) — that's a
  separate, hosted-edition feature. Community policies should use `deny` for hard
  blocks; a `needs_approval` verdict is advisory-only here (logged, not enforced).
- **Not** post-apply compliance scanning — this blocks *before* a deploy; it doesn't
  scan running infrastructure.
