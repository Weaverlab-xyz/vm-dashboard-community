# Dex

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want one OIDC issuer that both the dashboard and your on-prem clusters trust, so a person and their groups mean the same thing in the dashboard and in `kubectl`.

Part of [OIDC and single sign-on](../oidc.md).

[Dex](https://dexidp.io) can sit between the dashboard and your IdP. It is **optional**:
pointing the dashboard straight at your IdP ([OIDC and single sign-on](../oidc.md)) is the default and stays fully
supported. Dex earns its place when you also want **clusters** to trust the same issuer, so
a person and their groups mean the same thing in the dashboard and in `kubectl`. On-prem
(k3s) clusters require it for people. Managed clusters (EKS, GKE, OKE, AKS) stay on their
native authentication: Dex for them is
[paused](../design/agent-and-human-identity.md#managed-clusters-paused).

### Dex as the dashboard's SSO issuer

Nothing in the dashboard changes to use it — Dex is just another issuer. On the
[SSO settings](../oidc.md#step-2--configure-the-dashboard):

| Field | Value |
|---|---|
| Issuer URL | Dex's issuer, e.g. `https://dex.example.com:5554` |
| Client ID | `dashboard` |
| Client secret | the `dashboard` client's secret (the lab play leaves it in `/opt/dex/dashboard-client-secret`) |
| Groups claim | `groups` — Dex passes the upstream IdP's groups through |

Register the dashboard's callback, `https://<dashboard>/api/auth/oauth/oidc/callback`, as the
`dashboard` client's redirect URI in Dex, and Dex's own callback
(`https://<dex>/callback`) at the upstream IdP. To go back, put the upstream IdP's issuer
and client back in these fields.

### Dex for on-prem clusters

The dashboard needs Dex's issuer separately from SSO, because
the kubeconfig it hands people logs in to Dex with its own public client: **Settings →
Kubernetes → Dex** (`dex_issuer_url`, `dex_k8s_client_id`, and `dex_ca_pem` for a lab Dex
with its own CA), then **Trusts Dex** on the cluster's row. See
[Kubernetes](../kubernetes.md), where the API-tunnel download for a `cloud=local` cluster
is described.

### A lab Dex

A lab Dex on k3s, and the play that makes the k3s API server trust it:
[`examples/playbooks/dex/`](../../examples/playbooks/dex/README.md). Why Dex for people and
SPIRE for workloads: [design/agent-and-human-identity.md](../design/agent-and-human-identity.md).
