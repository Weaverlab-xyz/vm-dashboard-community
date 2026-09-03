# Notes

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you have hit something odd and want to know whether it has already been investigated.

Dated investigations, kept for their conclusions rather than their narrative. Each one is
a thing that was true when it was written, written down because the next person to hit it
would otherwise spend the same afternoon on it.

Unlike [design/](../design/README.md), these are not arguments about how something should
be shaped — they are findings. Treat the dates as load-bearing: a cost figure or a
provider bug may have moved since.

| Page | Read this when |
|---|---|
| [Cloud cost guardrails](cloud-cost-guardrails.md) | your lab cloud bill is higher than the resources you can see explain. |
| [Sandbox + provisioning cost audit](sandbox-provisioning-cost-audit.md) | you are changing a sandbox bootstrapper or a provisioning path and want to know what leaked cost last time. |
| [`beyondtrust/sra` blocks `tunnel_type = "k8s"`](sra-provider-k8s-tunnel-bug.md) | a Terraform-managed k8s tunnel is refused by the provider's own schema. |
