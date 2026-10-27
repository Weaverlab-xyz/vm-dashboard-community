# Cloud VM lifecycle and troubleshooting

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are destroying a VM, or one cannot be reached, will not deploy or will not come back.

Part of [Cloud VMs](../vms.md).

- **Destroy** (`DELETE /api/{cloud}/instances|vms/{id}`) removes the instance, deregisters
  the PRA Shell Jump (from stored state), and off-boards Password Safe / Entitle if they were
  wired. AWS reclaims the shared NAT instance + SSM endpoints when the last VM is gone; GCP
  reclaims the on-demand Cloud NAT + egress rule when the last VM **in that region** is gone;
  Azure removes the on-demand NAT gateway + NSG allow when the subnet's last VM is gone.
- **VM can't reach the internet** — by design (private subnet). On AWS enable
  `aws_nat_instance_enabled`; on OCI the vm-subnet already has a NAT Gateway; on GCP enable
  `gcp_vm_nat_enabled` (default on); on Azure enable `azure_vm_nat_enabled`, which opens both
  the NSG's `deny-internet-out` and a private subnet's missing outbound path. Note GCP denies egress through **two** independent gates —
  the vm-subnet is left off the sandbox Cloud NAT *and* a priority-1000 rule denies egress on
  the VM network tag — so opening only one changes nothing. DNS still resolves either way
  (the metadata server is always reachable), so the symptom is a **connection timeout**, not a
  name-resolution failure. Egress is ensured at **deploy** time: a VM created before the
  feature was enabled needs a redeploy, or the gateway + rule created by hand.
- **Shell Jump shows Unavailable** — the gateway host didn't start; set the cloud's deploy
  key (`aws_ecs_docker_deploy_key` / `azure_aci_docker_deploy_key` /
  `gcp_cloud_run_docker_deploy_key`). On **OCI** you must supply your own gateway.
- **Can't SSH the VM** — the VM SG/NSG only allows SSH from the gateway; reach it through the
  PRA Shell Jump, not directly.
- **OCI deploy rejected (HTTP 400)** — a non-free-tier shape without `acknowledge_charges`;
  tick the acknowledge box or pick a free-tier shape. With a **Count**, the whole batch is
  measured against the envelope, so this can fire on a shape that is free on its own.
- **OCI deploy rejected (HTTP 400, `shape_not_launchable`)** — the shape can't launch that
  image in that availability domain: either the AD doesn't offer the shape, or the image
  doesn't support it (architecture, usually). The message lists the shapes that *do* work
  for that image there — pick one. `LaunchInstance` reports both cases, and a genuine IAM
  denial, as the same unattributed `404 NotAuthorizedOrNotFound` about a second into the
  job, which is why this is checked up front instead. It applies to `POST /api/oci/deploy`
  (including its `count` fan-out) and to `POST /api/oci/bulk-deploy`, where the shape is
  shared but **every** selected image is checked — one bad image rejects the whole
  selection and creates nothing, like a name collision does. A blank `availability_domain`
  is checked against the AD the deploy would actually use (the first one). Reaching this
  from the deploy form means the picker fell open (see the empty-picker entry below); from
  an API client it needs no picker at all. The check deliberately **fails open**: if the
  lookup can't reach OCI the deploy proceeds, so a bare 404 that survives it is most likely
  the IAM case — check the policies on the compartment holding the subnet and the image.
- **OCI Shape reset itself to “— pick a shape —”** — expected: the shape list is scoped to
  the selected availability domain and image, and the one you had isn't offered in the new
  scope. The note under the picker names it. Choose from the narrowed list; the form won't
  submit until you do. The same happens on load in a region without
  `VM.Standard.E2.1.Micro`, whose Always-Free default the form drops rather than POST.
- **OCI shape picker is empty** — two different causes, and the note under the picker says
  which, because they need opposite fixes:
  - *"No shape offered in … can launch this image"* — the AD does offer shapes, and this
    **base image** can boot none of them. Change the image, not the AD. The note lists what
    the AD offers; if those are all `A1`/`A2` (Ampere), you need an `aarch64` image. This is
    common in trial tenancies, where `ListShapes` is scoped by your **service limits** rather
    than by what hardware exists — a tenancy with quota only for Ampere sees three shapes in
    every AD of the region and cannot launch an x86 image anywhere in it.
  - *"OCI lists no shapes in …"* — OCI returned nothing for that AD at all (a failed
    `ListShapes` looks identical to an empty one). Try another AD, or **Refresh**.

  The narrowing still falls open when a lookup is *silent* — the tenancy's policy blocks
  `ListImageShapeCompatibilityEntries`, or the image publishes no entries — so the picker
  never offers less than `check_launch_placement` accepts. In that fallen-open state the
  advisory architecture warning and the `shape_not_launchable` gate above are your only
  hints, so mind them.
- **Deploy rejected (HTTP 409, `vm_name_collision`)** — the names this deploy would create
  are already taken by VMs the dashboard deployed or is deploying. Pick a different base
  name, or destroy the existing VMs first. The check is deliberately strict: Azure, GCP and
  OCI resolve a destroy by first match on name, so duplicates make teardown ambiguous.
- **A batch child is stuck `queued`** — children are created unclaimable on purpose and are
  driven by their `*_bulk_deploy` parent. Check the parent (same `batch_id`): if it failed
  or was reconciled away, its children have nothing to drive them.

For the sandbox network topology see [Cloud Sandbox](../sandbox.md); for day-2 Ansible
against deployed VMs see [Config Management](../../operations/config-management.md).
