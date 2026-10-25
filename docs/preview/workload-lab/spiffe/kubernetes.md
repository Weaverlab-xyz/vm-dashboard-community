# Reaching Kubernetes with a JWT-SVID

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want a workload to authenticate to a Kubernetes API server with its SPIFFE identity, and to rehearse the two ways it fails.

Part of [SPIFFE and SPIRE](../spiffe.md).

Everything above is about identities that are **minted and vaulted**, which
[Minting is a deliberate downgrade](../spiffe.md#minting-is-a-deliberate-downgrade-and-the-honest-version-matters)
is explicit about being a downgrade. This is the un-downgraded path: a workload attests
itself, fetches a token that lives five minutes, and reaches a Kubernetes API server with it.
Nothing is stored in a vault, on disk, or anywhere else. (The dashboard's own token
endpoint is the other relying party — see
[Service accounts → SPIFFE workloads](../../../admin/service-accounts.md#spiffe-workloads-authenticate-with-the-svid-hold-nothing).)

The linked k3s node is also **the one host in the lab that runs a SPIRE agent**, which
makes it where the [agent demo cell](../../../profiles/demo/agent-demo-cell.md)'s worker goes.

**Preview.** Run so far, against SPIRE 1.15.3 locally: the exec credential plugin
(`k8s-spiffe-workload-jwt-exec-auth` 0.3.0) against a real agent on the lab's socket path
returned a `client.authentication.k8s.io/v1` ExecCredential carrying the workload's
JWT-SVID. **Nobody has yet watched the whole chain against a live pair** — the API server
accepting that token is the part still to prove. Treat the first run as the validation.

## Building it: one button, two VMs

**Kubernetes**, on the lab's row, once the lab is `available`. It takes a **second host**
and runs five playbooks alternating between the two machines, passing the join token and the
trust bundle between them so nothing is copied by hand.

| | |
|---|---|
| The SPIRE VM | the one the lab was already built on |
| The k3s VM | a second, plain VM — k3s is installed by the run. 2 vCPU / 4 GB is plenty |

**Both are attached, not created**, exactly as the SPIRE host always was: deploy them from
the normal cloud page first, and each keeps its own SSH key, auto-delete timer and Destroy.
The dashboard re-derives both from its own deploy rows rather than trusting an address it is
handed, and it **refuses the SPIRE host as the k3s node** — an agent attesting over loopback
proves the mechanism but not that it crosses a network, which is the half worth showing.

Upload these on the **Storage** page too; the build form's prerequisite list covers the
four `spire-*` plays, and these are the ones this action adds:

| Order | Play | Host |
|---|---|---|
| 1 | `k3s-server-init.yml` | k3s node |
| 2 | `spire-oidc-provider.yml` | SPIRE |
| 3 | `spire-k8s-entry.yml` | SPIRE |
| 4 | `spire-agent-install.yml` | k3s node |
| 5 | `k3s-spiffe-auth.yml` | k3s node |

Each stage is its own `ansible_local` job row, so a failed stage's Ansible output is
somewhere you can read it, and the panel links all five. Three things worth knowing:

- **A failed link leaves the trust domain alone.** It stays `available` and goes on
  governing everything it did before — the governance half is what most labs are built for,
  and it stands on its own. Only the link is marked failed, and **Retry the link** re-runs it.
- **The firewall change is on the SPIRE host only**, opening tcp/8081 and tcp/8443 to the
  k3s node's private address. The k3s node needs nothing inbound, because the workload runs
  on it.
- **The lab has never had a SPIRE agent before.** `spire-server-install.yml` installs none,
  because the plugin only ever talks to the server's API. Stage 4 is what makes the
  un-vaulted path possible at all, and it proves itself by fetching a JWT-SVID *as the
  workload account* before reporting success.

**The two VMs do not share an SSH key, and the panel asks separately.** Leaving its
credential fields blank is the normal case and already correct: the runner derives a keypair
from the deploy job of the host it is *connecting to*, and the Kubernetes stages target the
k3s node. What it deliberately does **not** do is inherit whatever the SPIRE host was given
at build time — that would connect to one VM with another VM's credential, and the failure is
`Permission denied (publickey)` several stages into a run that looked configured. The same
either/or rule applies: a Password Safe managed account **or** an SSH-key secret, not both.

## The two upstream halves

Both are Apache-2.0 projects in the **SPIFFE GitHub org**, and both carry a "Development
Phase" badge with single-digit stars — real and officially owned, not production-proven.
That belongs in front of a customer rather than in a footnote.

| | |
|---|---|
| [`k8s-spiffe-workload-jwt-exec-auth`](https://github.com/spiffe/k8s-spiffe-workload-jwt-exec-auth) | A client-go **exec credential plugin**. Reads a JWT-SVID over the Workload API socket and hands it to `kubectl` as the bearer token. Audience from `SPIFFE_JWT_AUDIENCE`. |
| [`k8s-spiffe-workload-auth-config`](https://github.com/spiffe/k8s-spiffe-workload-auth-config) | The server half: the kube-apiserver `AuthenticationConfiguration`, with the SPIFFE trust bundle as `certificateAuthority` and the OIDC Discovery Provider as the issuer. |

The workload's entire kubeconfig user is four lines, and holds no credential:

```yaml
user:
  exec:
    apiVersion: "client.authentication.k8s.io/v1"
    command: "k8s-spiffe-workload-jwt-exec-auth"
    args: ["-timeout=10s"]
    interactiveMode: Never
```

`-timeout` is there because the plugin's default waits forever for the Workload API, and
`kubectl` would hang rather than fail. The play installs **0.3.0** from upstream's
goreleaser archive (`…_Linux_x86_64.tar.gz` / `…_Linux_arm64.tar.gz`, pinned by SHA-256),
into its own version directory behind a symlink. That version was run against a SPIRE
1.15.3 agent on the lab's socket path and returned a `client.authentication.k8s.io/v1`
ExecCredential carrying the workload's JWT-SVID.

It also takes `SPIFFE_JWT_SOURCE=server-admin-api` to mint from the server's admin API
instead. **Do not use that mode here.** It is the same attestation bypass this page spends a
section refusing to make the default, and choosing it would demonstrate the downgrade while
claiming to demonstrate the upgrade.

## Why it is k3s, and why that is a real limit

`--authentication-config` is a **kube-apiserver flag**. EKS, AKS and GKE do not expose it, so
none of the clusters on the [Kubernetes](../../../cloud-services/kubernetes.md) page can do this at all — k3s takes
arbitrary API server flags, which is why the lab's second VM runs k3s. Structured
Authentication is GA in Kubernetes **1.34** (`apiserver.config.k8s.io/v1`), and the play
refuses an older cluster rather than writing a config the API server will reject and fail to
start on.

State that limit plainly. A customer running only managed clusters cannot adopt this
pattern, and finding that out mid-demonstration is worse than saying it first.

## What it is being compared against

The dashboard already ships a short-lived Kubernetes token: `POST /clusters/{id}/ps-token`
in **Bound mode**, described in [Kubernetes](../../../cloud-services/kubernetes.md#access--identity) with its
reasoning in [the design note](../../../design/k8s-sa-token-rotation.md). This does **not** replace
it — a cluster whose broker needs a vaulted credential still needs Bound mode. The two
answer different questions, and the interesting claim is not "short-lived tokens are good"
but *what each one still leaves lying around*:

| | Bound-mode SA token | JWT-SVID over the Workload API |
|---|---|---|
| Where the credential rests | Password Safe, and PRA Vault via the synced account | **Nowhere** — process memory, for minutes |
| Who can replay it | Anyone who can retrieve it from either vault | A caller on the attested workload's own machine |
| What binds it | The ServiceAccount's `uid` claim | Node and workload attestation |
| Works on EKS / AKS / GKE | Yes | **No** — needs a self-managed API server |
| Governed as an inventory row | Yes | No — there is no credential to govern |

Showing both, on one page, is a stronger demonstration than either alone.

The Workload Lab's **Kubernetes tab** is the Bound-mode column made operable against a
cluster you already have, with the scoping this comparison leaves implicit: two profiles, a
namespace-scoped Deployer and a cluster-wide Reader that cannot read Secrets, neither of
them `cluster-admin`. See [Workload access to Kubernetes](../kubernetes.md). It is the
path to reach for when the fourth row above rules this one out — which it does on every
managed cluster.

## Proving it, and the two failures worth rehearsing

Run as the workload account on the k3s node:

```bash
sudo -u deploy-bot kubectl get pods -A
```

Then break it deliberately, because both of these are the interesting part:

1. **A wrong audience must be refused.**
   `sudo -u deploy-bot env SPIFFE_JWT_AUDIENCE=wrong kubectl get pods -A`
   The audience is the only thing stopping a token minted for another relying party being
   replayed at this API server.
2. **Stop the SPIRE agent and wait out the TTL.** Access stops within five minutes, with no
   revocation step anywhere. That is the whole argument for a short-lived token, and it is
   also the boundary in [Boundaries](../spiffe.md#boundaries) below: containment is only as fast as the
   TTL.

A 401 on the *first* attempt is almost always the issuer string or clock skew, not RBAC:

```bash
journalctl -u k3s | grep -i 'authentication\|oidc\|jwt'
```

**Labs built before the server set an issuer.** The API server matches a token's `iss`
against the issuer it was given, exactly. Every mode's server now signs with the OIDC
provider's URL (`jwt_issuer` in vm and docker mode, `jwtIssuer` in the Helm values). A vm
or docker lab built earlier signs with no `iss`, and its k3s link refuses every token. It
picks the setting up the next time its install stage runs: **Upgrade** does that when the
SPIRE version changes. Otherwise, re-run the mode's install play by hand with
`-e jwt_issuer=https://oidc.<trust domain>:8443`. Seeded entries, the admin credential
and the CA are kept.

The full argument, including the four traps the playbooks encode, is in
[the design note](../../../design/workload-k8s-short-lived-token.md).
