# Entra federation to EKS

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are federating an EKS cluster with Entra ID, or connecting to one as its user.

Part of [Entra ID → Kubernetes real-identity federation](../entra.md).

## Federate an EKS cluster (the demo)

1. **Entra federation → Enable federation.** EKS associates the EKS Entra app (§1a) as
   the cluster's OIDC identity provider and the job polls until it's **ACTIVE** (a few
   minutes; the cluster shows `UPDATING` on AWS). This is additive — IAM / `aws-auth`
   access is unchanged, and node bootstrap + console access stay on IAM.
2. **Entra group → Bind group** — binds the group's Object ID to the ClusterRole.
3. **API tunnel → Create tunnel** + connect it in the rep console (see above).
4. **Entra federation → Download Entra kubeconfig.** Token-free — the exec block is
   `kubectl oidc-login get-token` against the EKS Entra app, using the **device-code**
   grant (deliberately, not the browser authcode flow: authcode SSOs the operator into
   the *machine's own* tenant, which is the wrong one for a lab/demo tenant).

## Connecting as the end user (EKS)

Prereqs: `kubectl` and **int128's** `kubelogin` on `PATH` (see below). The user needs a
live Entitle grant for the Entra group.

**1. Confirm the tunnel session is connected** in the rep console.

**2. Point `KUBECONFIG` at the downloaded file and use it.**

```bash
export KUBECONFIG=~/Downloads/<cluster>-entra.kubeconfig
kubectl get ns
```

Windows PowerShell:

```powershell
$env:KUBECONFIG = "$HOME\Downloads\<cluster>-entra.kubeconfig"
kubectl get ns
```

The first call prints a **device-code URL + code**. Complete it in a browser as the
correct Entra account — use an InPrivate/incognito window if the machine is joined to a
different tenant, or the browser's existing SSO will sign in the wrong identity.

**3. Verify who you are:**

```bash
kubectl auth whoami
```

EKS is associated with `usernamePrefix: "entra:"`, so this shows **`entra:<your-object-id>`**
— proof the request went through the OIDC path and not IAM. Groups come through
**unprefixed**, which is why the RBAC subject is the bare group Object ID.

## Installing `kubectl oidc-login`

See [Installing `kubectl oidc-login`](../../k8s-federation.md#installing-kubectl-oidc-login) on
the hub. It is **int128's** `kubelogin`, **not** Azure's `kubelogin` (which is AKS-only).

## Troubleshooting the user's connection (EKS)

| Symptom | Cause | Fix |
|---|---|---|
| `exec: executable kubectl-oidc_login not found` / `unknown command "oidc-login"` | int128 kubelogin missing or misnamed on `PATH` | install it (above); check `kubectl oidc-login --help` runs |
| `Unable to connect to the server: dial tcp 127.0.0.1:6443` (connection refused / timed out) | the tunnel session isn't connected, or another cluster's tunnel owns the port | connect the Protocol Tunnel Jump; disconnect the other cluster's |
| `x509: certificate is valid for <hash>.<region>.eks.amazonaws.com, not 127.0.0.1` | `tls-server-name` was lost (hand-edited kubeconfig) | re-download the kubeconfig |
| device-code sign-in succeeds, then `Unauthorized` | the OIDC provider isn't `ACTIVE` yet, or the kubeconfig's client id ≠ the audience associated on the cluster | wait for ACTIVE; confirm `entra_oidc_client_id` is the **EKS** app (§1a) and re-run Enable federation |
| signed in, but `Forbidden: User "entra:<oid>" cannot list …` | authenticated, not authorized — no ClusterRoleBinding for the group, or the `groups` claim is absent | run **Entra group → Bind group**; check the §1a groups claim and the overage note |
| revoked the Entitle grant, access continues | the cached ID token is still valid — revocation doesn't invalidate an issued token | delete `~/.kube/cache/oidc-login` (Windows `%USERPROFILE%\.kube\cache\oidc-login`); otherwise access ends at token expiry |
