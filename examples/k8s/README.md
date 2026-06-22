# Sample Kubernetes manifests (managed-cluster starters)

Ready-to-adapt manifests for the **EKS / AKS / GKE clusters the dashboard
manages** — provision or register a cluster, install the Portainer management
plane, and broker access via BeyondTrust PRA
(see [docs/integrations/portainer.md](../../docs/integrations/portainer.md) and
[docs/integrations/beyondtrust.md](../../docs/integrations/beyondtrust.md)).

They are the Kubernetes counterpart to [`examples/compose/`](../compose/) and
[`examples/playbooks/`](../playbooks/): the community-edition answer to an
application catalog is to apply one of these, edit the placeholders, and go —
rather than a one-click catalog (a SaaS-edition feature). Where the compose
samples deliberately omit volumes/persistence, these show the Kubernetes-native
shapes the dashboard's managed clusters add: persistence, autoscaling, scheduled
jobs, and namespace guardrails.

## How to apply

The dashboard doesn't apply raw manifests for you — you deploy them into a
managed cluster through the access it brokers:

1. **Portainer** (the management plane the dashboard installs) → your cluster's
   environment → **Applications → Create from manifest**, paste a file, deploy.
2. **kubectl through the PRA tunnel** — open the brokered session to the cluster
   (Kubernetes jump), then `kubectl apply -f <file>` from your workstation.

Either way, **apply `00-namespace.yaml` first** — every other sample is scoped to
the `sample-apps` namespace, and `kubectl delete ns sample-apps` removes the
whole set.

## What's here

| File | Kind(s) | Demonstrates |
|---|---|---|
| `00-namespace.yaml` | Namespace | Isolated `sample-apps` ns with `restricted` Pod Security enforced |
| `nginx-deployment.yaml` | Deployment · Service | Stateless web app + ClusterIP (unprivileged nginx on :8080) |
| `nginx-ingress.yaml` | Ingress | External exposure via an ingress controller |
| `nginx-hpa.yaml` | HorizontalPodAutoscaler | CPU autoscaling 2→6 (needs metrics-server) |
| `web-configmap.yaml` | ConfigMap · Deployment | Config as env var **and** mounted file |
| `app-secret.yaml` | Secret · Deployment | Secret-as-env (prefer ESO/Password Safe for real creds) |
| `redis-statefulset.yaml` | StatefulSet · Service · PVC | Persistence via a `volumeClaimTemplate` |
| `cronjob-housekeeping.yaml` | CronJob | Scheduled one-shot task |
| `namespace-guardrails.yaml` | ResourceQuota · LimitRange | Cap namespace usage; default container requests/limits |
| `network-policy.yaml` | NetworkPolicy | Default-deny ingress + targeted allows |

## Two things to know

- **Hardened by construction.** The namespace enforces the `restricted` Pod
  Security Standard, so every workload sample runs non-root, drops all
  capabilities, disallows privilege escalation, and sets a RuntimeDefault seccomp
  profile. Drop a non-compliant manifest into this namespace and the API server
  rejects it — the samples double as a hardened-pod template.
- **Some samples need a cluster add-on.** `nginx-ingress.yaml` needs an ingress
  controller, `nginx-hpa.yaml` needs metrics-server, and `network-policy.yaml`
  needs a policy-enforcing CNI. Each file's header comment calls out its
  prerequisite and what happens without it. Managed EKS/AKS/GKE clusters vary in
  what ships by default — check before relying on one.

## JIT access (optional)

If you registered the cluster as an **Entitle Kubernetes integration**
("Register in Entitle" on the cluster page; see
[docs/integrations/entitle.md](../../docs/integrations/entitle.md)), users request
just-in-time namespace access in Entitle rather than holding standing kubeconfig
credentials — orthogonal to these manifests, which define *what* runs.

## Notes

- These are starting points — review and adapt before applying to a real cluster.
- Images are pinned to a tag (`nginxinc/nginx-unprivileged:1.27-alpine`,
  `redis:7-alpine`, `busybox:1.36`); bump them deliberately.
- `tests/test_k8s_samples.py` validates every file here is well-formed multi-doc
  YAML with `apiVersion` / `kind` / `metadata.name` on each object, so a
  malformed sample can't ship.
