# Cloud

> **Audience:** operator · **Profile:** `both` · **Read this when:** you are putting workloads on AWS, Azure, GCP or OCI and want to know which page covers which part.

Everything the dashboard builds in a public cloud, and what it costs. The pages are in the
order you usually need them: how a deploy works underneath, a place to run it, the things
you deploy, then the bill.

| Page | Read it when |
|---|---|
| [Infrastructure as Code](cloud/infrastructure-as-code.md) | you are about to deploy your first cloud resource and want to know what is actually running underneath |
| [Cloud Sandbox](cloud/sandbox.md) | you want an isolated cloud account for the dashboard's labs, bootstrapped rather than hand-built |
| [Cloud VMs](cloud/vms.md) | you are deploying cloud VMs, or want the dashboard to see and power ones it did not deploy |
| [Cloud Containers](cloud/containers.md) | you want a containerised app on a cloud runtime without standing up Portainer |
| [Virtual Desktops](cloud/virtual-desktops.md) | you need a pool of private desktop VMs that reps reach through the PRA Gateway rather than over the internet |
| [Image Management](cloud/image-management.md) | you are about to build a custom image and need to know how it will reach the other clouds |
| [Cloud Costs](cloud/costs.md) | you want month-to-date cloud spend, a budget alert, or a budget in the cloud that alerts while the dashboard is down |

Not here, though they also run in the cloud:
- [Databases](databases.md) and [Kubernetes](kubernetes.md), which cover managed and
  on-premises alike;
- the dashboard's own credentials for each cloud: [onboarding](ONBOARDING.md) to set them
  up, [Secrets Management](access/secrets-management.md) for where they are kept, and
  [the dashboard's own identity](oidc/dashboard-identity.md) to stop storing them at all;
- [Cloud Hosting](operations/cloud-hosting.md), which is about running the dashboard
  itself in a cloud.
