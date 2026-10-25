# POVs on OCI

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you are running POVs on OCI and need to know how compartments map to environments.

Part of [Running POVs on a public cloud](../public-cloud.md).

OCI is shaped like AWS: no native environment object, so a teardown unpicks resource types
in dependency order rather than deleting one group. Give the API user **manage** on
instances, VCNs, subnets, route tables, security lists and internet gateways in its
compartment.

**A compartment is not used as the environment, despite being the obvious analogy.** It
looks like an Azure resource group and behaves nothing like one: creating a compartment
needs tenancy-level IAM a POV's credential should not have, deleting one requires it to be
empty first, the delete is a slow asynchronous operation, and the name stays reserved
afterwards. A POV that could not be torn down in one pass, or whose name could not be
reused for months, would be worse than the tagging used instead.

The compartment is still **recorded** on the POV, the way GCP records its project, so a
teardown weeks later aims at the compartment the environment actually went into.

**Three things are OCI's own, and each fails by naming something other than the cause:**

- **Every VCN gets a default security list that allows SSH from `0.0.0.0/0`.** A POV placed
  on it would be a customer environment with port 22 open to the internet. This driver
  creates its own list — the POV's subnet in, everything out — and never attaches the
  default.
- **A Flex shape is refused without an explicit OCPU count**, and the API's error names
  `shapeConfig` rather than the template field. Most of OCI's current catalogue is Flex, so
  a template's instance type accepts a size suffix rather than needing a column only one
  cloud would read:

  | Instance type | Result |
  |---|---|
  | `VM.Standard.E4.Flex` | 2 OCPUs, 16 GB — the defaults |
  | `VM.Standard.E4.Flex:4` | 4 OCPUs, 16 GB |
  | `VM.Standard.E4.Flex:4:32` | 4 OCPUs, 32 GB |
  | `VM.Standard2.2` | a fixed shape; a suffix on one of these is **refused**, because it means the author believed they were sizing something |

  The POV page reports the shape back in the same form, so you are never translating a
  `shapeConfig` into a suffix by hand.
- **`user_data` must be base64.** The SDK passes it through untouched, and plain text gives
  an instance that boots fine and never runs its bootstrap. Azure has the same wrinkle;
  AWS and GCP do not.

Two smaller notes. Availability domains are **listed, never assembled** — an AD is named
like `Uocm:PHX-AD-1`, a tenancy-specific prefix plus a region code, so there is nothing to
build. And suspend uses **SOFTSTOP**, which asks the guest to shut down before falling back
to a hard stop: a POV somebody resumes next morning should not have been pulled out at the
cord every night.
