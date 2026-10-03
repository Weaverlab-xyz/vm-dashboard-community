# Dex samples (`dex/`)

Dex is the **optional** single OIDC issuer for *people*. It federates the IdP you already
run (Entra, Okta, Keycloak, Google) so that the dashboard's SSO and every cluster's API
server trust one issuer and see the same user and groups.

Nothing here is required:
- The dashboard's direct OIDC sign-in ([docs/integrations/oidc.md](../../../docs/integrations/oidc.md)) keeps working, pointed straight at your IdP.
- Clusters keep their native authentication.

Dex is something you opt in to, cluster by cluster.

Why Dex for people and SPIRE for workloads, and how the two share a k3s API server:
[docs/design/agent-and-human-identity.md](../../../docs/design/agent-and-human-identity.md).

| File | Target | What it does |
|---|---|---|
| `dex-helm.yml` | k3s server node (SSH) | Installs Dex 2.45.1 (chart 0.25.2, image pinned by digest) on k3s. It is non-root, read-only, with no capabilities, and keeps its state in CRDs (no database). Signs in through an upstream `oidc` connector, or one static lab user. Defines a public `kubernetes` client for kubelogin and, optionally, a confidential `dashboard` client. Publishes **HTTPS only** on the node address as `dex-public`. Generates a lab CA unless you pass a public certificate. Ends by fetching the discovery document as a relying party would and checking the issuer string. |
| `../k3s/k3s-dex-auth.yml` | k3s server node (SSH) | Adds a Dex JWT authenticator to the API server. Usernames become `dex:<email>` and groups `dex:<group>`. Optionally binds one upstream group to a ClusterRole. **Shares** the AuthenticationConfiguration with `k3s-spiffe-auth.yml` and replaces only its own entry. |
| `dex-remove.yml` | k3s server node (SSH) | Removes the Dex authenticator (keeping SPIFFE's), the binding, the release, the namespace and Dex's CRDs. Keeps the lab CA unless `remove_lab_ca=true`. |

## Order

1. `k3s/k3s-server-init.yml`, and helm on the node. `spire/spire-helm.yml` installs a checksummed one; any helm 3 works.
2. `dex/dex-helm.yml`:
   ```
   -e dex_hostname=dex.lab.test
   -e upstream_issuer=... -e upstream_client_id=... -e upstream_client_secret=...
   -e dashboard_redirect_uri=https://<dashboard>/api/auth/oauth/oidc/callback
   ```
   At the upstream IdP, register Dex's callback: `https://<dex_hostname>:<dex_port>/callback`.
3. `k3s/k3s-dex-auth.yml`:
   ```
   -e dex_issuer_url=https://dex.lab.test:5554 -e dex_ca_pem="$(cat ca.crt)" -e admin_group=k8s-admins
   ```
   Use the CA that `dex-helm.yml` printed, or leave `dex_ca_pem` blank for a public certificate.
4. **Optional:** point the dashboard's SSO at Dex. In **Settings → Integrations → Single sign-on**:
   - **Issuer:** `https://dex.lab.test:5554`
   - **Client ID:** `dashboard`
   - **Client secret:** the value in `/opt/dex/dashboard-client-secret` on the node.

## Managed clusters

EKS, GKE, OKE and AKS (in preview) can all trust Dex too: see the matrix in the design note. They fetch Dex's discovery document and JWKS from the internet, so for them Dex needs:
- a **public** DNS name,
- a **public-CA** certificate (`dex_tls_cert_pem` / `dex_tls_key_pem`).

The lab CA is only good for k3s.

## What has been checked, and what has not

**Checked:**
- **The shared AuthenticationConfiguration**, as written by `k3s-spiffe-auth.yml` and `k3s-dex-auth.yml` together:
  - It was booted on a real k3s v1.34.12 API server, which came up ready with both authenticators loaded.
  - The Dex entry was then exercised with tokens signed by a stand-in issuer serving the same discovery and JWKS shape:
    - a valid token arrives as `dex:alice@example.com` with groups `dex:k8s-admins` and `dex:system:masters`, the prefix neutralising the upstream `system:masters`;
    - a token with `email_verified: false` is refused;
    - a token for another audience is refused.
- **Merge and unlink behaviour**, under ansible-core 2.19:
  - each play replaces only its own entry, in place;
  - re-running either play is a no-op;
  - teardown keeps the other play's entry.
- **`dex-helm.yml`'s values** were rendered through the real chart with `helm template`.

**Not checked:** Dex itself has not run here, and **no play here has run against a live node**. Treat the first real run as the validation.

## If k3s will not come back

Delete `/etc/rancher/k3s/config.yaml.d/10-spiffe-auth.yaml` and run `systemctl restart k3s`.

That removes the `--authentication-config` flag, for Dex and SPIFFE both, and leaves the cluster as it was before either play. Re-run the plays once the cause is fixed.
