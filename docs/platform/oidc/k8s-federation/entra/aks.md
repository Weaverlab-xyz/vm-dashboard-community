# Entra federation to AKS

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are federating an AKS cluster with Entra ID, or connecting to one as its user.

Part of [Entra ID → Kubernetes real-identity federation](../entra.md).

AKS needs **no trust wiring**: dashboard-provisioned clusters are created with
`azure_active_directory_role_based_access_control { managed = true, azure_rbac_enabled = true }`,
so the API server already validates Entra tokens. **Enable federation** on an AKS row is
a recorded no-op that just lights up the kubeconfig download — there is nothing to
disable later either.

Because `azure_rbac_enabled = true`, the cluster runs the Azure authorization webhook
*alongside* Kubernetes RBAC. Kubernetes authorizers are OR'd, so the **Entra group →
Bind group** ClusterRoleBinding is what grants access here — no Azure role assignment
is needed, and the group Object ID is the subject exactly as on EKS.

## Federate an AKS cluster

1. **Entra federation → Enable federation** (no-op; records state).
2. **Entra group → Bind group.**
3. **API tunnel → Create tunnel** + connect it in the rep console.
4. **Entra federation → Download Entra kubeconfig.**

## Connecting as the end user (AKS)

Prereqs: `kubectl`, the **Azure** `kubelogin` (`az aks install-cli`, or winget/brew
`Azure/kubelogin` — *not* int128's), and for the recommended path the `az` CLI.

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

The first call prints a **device code** to complete in any browser as the correct Entra
account — InPrivate if the machine is joined to a different tenant, same reasoning as EKS.

**No `kubelogin convert-kubeconfig` step.** The download rewrites the stored exec into an
interactive device-code sign-in (well-known AKS AAD client app + your tenant, `--server-id`
preserved), so it authenticates as the *user*. The stored kubeconfig the dashboard's own
runners use is `--login spn` — service-principal mode, which reads
`AAD_SERVICE_PRINCIPAL_CLIENT_ID`/`_SECRET` from whoever's environment runs it — and
handing that to an end user either failed outright or silently authenticated as the
dashboard's SP. A **registered** kubeconfig already on a user mode (`azurecli` /
`devicecode` / `interactive`, or the legacy `azure` auth-provider) is passed through
untouched.

**3. Verify:** `kubectl auth whoami` shows your Entra **object ID** — with no `entra:`
prefix (that prefix is EKS-only, set by us at association time).

## Troubleshooting the user's connection (AKS)

| Symptom | Cause | Fix |
|---|---|---|
| `AAD_SERVICE_PRINCIPAL_CLIENT_ID` / client-secret error from kubelogin | the file is in `--login spn` mode — you are using the **API tunnel** kubeconfig, not the **Entra** one (only the latter is rewritten to device-code) | re-download via **Entra federation → Download Entra kubeconfig** |
| It "works" but RBAC never matches and `whoami` shows an app/SP id | same cause, on a machine that *has* the dashboard SP's env vars set — you authenticated as the SP | unset `AAD_SERVICE_PRINCIPAL_*` and use the Entra download |
| Download fails with a message about `azure_tenant_id` | device-code needs the tenant; the setting is empty | set **Azure tenant ID** in Settings, then re-download |
| `kubelogin: command not found` | Azure kubelogin missing (int128's does **not** substitute) | `az aks install-cli`, or install `Azure/kubelogin` |
| `Unable to connect to the server: dial tcp 127.0.0.1:6443` | tunnel not connected, or the port is held by another cluster | connect the Protocol Tunnel Jump |
| device-code sign-in lands in the wrong tenant | machine SSO picked the joined tenant | complete the code in an InPrivate window as the lab account |
| `Forbidden: User "<oid>" cannot list …` | no ClusterRoleBinding for the group, or the Entitle grant lapsed | **Entra group → Bind group**; re-request the grant |
| revoked the grant, access continues | cached token | delete `~/.kube/cache/kubelogin` (Windows `%USERPROFILE%\.kube\cache\kubelogin`) |
