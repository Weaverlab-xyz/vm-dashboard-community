# POVs on GCP

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running POVs on GCP and need to know its two API constraints.

Part of [Running POVs on a public cloud](../public-cloud.md).

GCP sits between the other two. Like AWS it has no native environment object, so a
teardown unpicks resource types rather than deleting one group. Unlike either, two of
Compute Engine's own rules cannot be met by the shared code.

Give the service account **Compute Admin** on its project (or the narrower roles in
[`terraform/pov_cloud_gcp_role`](../../../../terraform/pov_cloud_gcp_role/main.tf), below), with the Compute Engine API
enabled.

**A GCE label key must be lowercase.** The rule is `[a-z](../[-_a-z0-9]*)?`, so
`povEnvironment` is refused outright — with an error naming the API field rather than the
tag. Each shared key is mapped once: `pov_environment`, `pov_managed_by`, `pov_role`, and
`managed-by` unchanged.

**Only some GCE resources carry labels at all.** An Instance and a Disk do; a Network, a
Subnetwork and a Firewall have no labels field. So a POV's network layer is selected by
**name** instead — `povenv-<name>-net`, `-subnet`, `-fw` — and the instances by label. That
also means a POV name has a shorter ceiling here than elsewhere: a GCE resource name is
capped at 63 characters, and an over-long one is refused at build with the real number
rather than truncated, because truncation is how two POVs collide.

**The zone is resolved from the region, never assembled.** `us-east1` and `europe-west1`
have no `-a` zone, and GCE reports a nonexistent zone as
`403 Permission denied on 'locations/us-east1-a' (or it may not exist)` — which reads as a
credentials problem. This dashboard has paid for that mistake once already; the region's
own zone list is one call and is always right. It also guarantees the subnetwork and the
instances share a region, which GCE otherwise rejects at insert time with "Scope of the
specified subnetwork doesn't match the scope of the instance", naming neither.

**GCP is the only cloud where the POV records a project.** An AWS account and an Azure
subscription are instance-wide settings; a GCP project is a boundary an environment is
built *inside*. So `projects` is True, `PovEnvironment.project_id` records which project a
POV went into, and the teardown reads that back rather than re-deriving it from current
config — the rule `expiry_reaper` states outright, that a destroy aimed at the wrong
project is the worst version of this bug.

Two smaller differences:

- **Suspend is `stop`, not GCE's `suspend`.** GCE's suspend preserves RAM to disk and
  charges for that storage plus the reserved resources. `stop` lands on TERMINATED, where
  only the disks bill — which is the state the schedule is aiming at, despite the name.
  TERMINATED reads back as `stopped`, so a suspended POV does not appear destroyed.
- **The bootstrap goes in `user-data`, not `startup-script`.** The guest agent re-runs a
  startup script on *every* boot, and the payload carries a single-use enrolment code.

One firewall rule, allowing the POV's own subnet to reach itself. GCP denies ingress by
default and allows egress, so nothing else is needed and nothing from outside reaches in.
