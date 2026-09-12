# Design: a workload reaching Kubernetes with a short-lived token

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are about to build the Workload Lab's Kubernetes tab, or you are deciding whether a SPIFFE identity should be able to authenticate to a cluster at all.

**Nothing in this note is built.** The Workload Lab's **Kubernetes access** tab is an
explainer over the argument below, and the three playbooks in
[What building it would take](#what-building-it-would-take) do not exist. This records the
reasoning while it is fresh, so the build starts from a decision rather than from a blank
page.

## The problem

The SPIRE lab mints **audience-scoped JWT-SVIDs** and nothing in this dashboard ever
presents one to anything. [`docs/spiffe.md`](../spiffe.md) proves issuance and proves
governance — discovery returns 8 of 11 entries, attestation-policy findings fire — but the
credential's whole point is that some relying party accepts it, and no page demonstrates a
relying party at all. A reviewer can reasonably ask whether the SVID works, and the honest
answer today is "the plugin's test suite says so."

Kubernetes is the relying party worth picking, because the dashboard already provisions
clusters and already has a competing answer to compare against.

## 1. There is already a short-lived token path here, and it is the baseline

[`k8s-sa-token-rotation.md`](k8s-sa-token-rotation.md) and
[`docs/kubernetes.md`](../kubernetes.md) describe `POST /clusters/{id}/ps-token`: a
ServiceAccount token onboarded as a Password Safe managed account on the *Kubernetes Service
Account Token* plugin, with **Bound mode** issuing TokenRequest-API bound tokens.

That is a real short-lived token and it is **not** what this note proposes replacing. It is
what the proposal has to be better than, on one axis:

| | Bound-mode SA token (built) | JWT-SVID over the Workload API (proposed) |
|---|---|---|
| What authenticates | a bearer token | a bearer token |
| Where the credential rests | **Password Safe, and PRA Vault via the synced account** | **nowhere** — process memory, for minutes |
| Who can replay it | anyone who retrieves it from either vault | a caller on the attested workload's own machine |
| What binds it | the ServiceAccount's `uid` claim | node + workload attestation |
| Audience-scoped | yes | yes |
| Revocation | delete the SA (invalidates every token ever issued) | delete the entry; the SVID lives out its TTL |
| Works on EKS / AKS / GKE | **yes** | **no** — see §3 |
| Governed as an inventory row | yes | no |

The row that matters is the second one. `docs/spiffe.md` already argues this in the other
direction — minting into a vault is "a strict downgrade… if a workload can reach the Workload
API, it should use the Workload API" — and then the lab never shows the un-downgraded path.
Putting both on one page, with this table, is a stronger demonstration than either alone,
because the interesting claim is not "short-lived tokens are good" but **"here is what each
one still leaves lying around."**

So this tab is not a replacement for the `ps-token` path and must not be built as one. A
cluster whose broker needs a vaulted credential still needs Bound mode; the two answer
different questions.

## 2. The pattern exists upstream, and using it beats inventing one

Two projects in the **SPIFFE GitHub org**, both Apache-2.0, do exactly this:

- [`spiffe/k8s-spiffe-workload-jwt-exec-auth`](https://github.com/spiffe/k8s-spiffe-workload-jwt-exec-auth)
  — a client-go **exec credential plugin**. It fetches a JWT-SVID over the SPIFFE Workload
  API (`unix:///tmp/spire-agent/public/api.sock`) and returns it to `kubectl` as the bearer
  token. Audience defaults to `k8s` via `SPIFFE_JWT_AUDIENCE`, and it must match what the
  API server is configured to accept. The kubeconfig side is four lines:

  ```yaml
  user:
    exec:
      apiVersion: "client.authentication.k8s.io/v1"
      command: "k8s-spiffe-workload-jwt-exec-auth"
      interactiveMode: Never
  ```

  It also takes `SPIFFE_JWT_SOURCE=server-admin-api` to mint from the SPIRE Server admin
  API instead of the Workload API. **Do not use that mode in this lab.** It is the same
  attestation bypass `docs/spiffe.md` spends a section refusing to make the default, and
  choosing it here would demonstrate the downgrade while claiming to demonstrate the
  upgrade.

- [`spiffe/k8s-spiffe-workload-auth-config`](https://github.com/spiffe/k8s-spiffe-workload-auth-config)
  — the server half. It maintains the kube-apiserver `AuthenticationConfiguration`,
  injecting the SPIFFE trust bundle as `certificateAuthority` and the SPIRE **OIDC Discovery
  Provider** as the issuer:

  ```yaml
  jwt:
  - issuer:
      url: https://oidc-discovery.example.org
      audiences:
      - k8s
  ```

  Consumed with `--authentication-config=/etc/kubernetes/pki/auth-config.yaml`.

**Both carry a "Development Phase" badge and have single-digit stars.** They are real and
officially owned, not production-proven, and that belongs on the tab rather than in a
footnote — the same way the SPIRE and Certificate features already say which of their paths
has never been run against a live authority. The value here is that the *pattern* is
upstream and standard: the API server side is ordinary OIDC, so nothing in this repo invents
a trust mechanism.

Note the discovery provider is a **third daemon**, not a SPIRE server flag. The lab installs
the server only today, so it is new surface — and it must be reachable by the API server over
TLS, which is what makes it a network question rather than a packaging one.

## 3. `--authentication-config` is what picks the cluster, and it excludes all three clouds

Structured Authentication Configuration is stable as of **Kubernetes 1.34**
(`apiVersion: apiserver.config.k8s.io/v1`; it first shipped in 1.31 behind earlier API
versions). It replaces the old single-provider `--oidc-*` flags, takes multiple JWT issuers,
maps claims to username and groups through CEL, and reloads on file change.

It is also a **kube-apiserver flag**, which is the constraint that decides everything else:

- **EKS, AKS and GKE do not expose it.** The `docs/kubernetes.md` clusters are therefore all
  ineligible. EKS has its own OIDC identity-provider association and AKS has a preview of
  structured auth, but neither is this file, and building against either would be a different
  feature.
- **k3s takes arbitrary API server flags** through `kube-apiserver-arg` in
  `/etc/rancher/k3s/config.yaml`, and [`examples/playbooks/k3s/`](../../examples/playbooks/k3s/)
  already stands a server up (`k3s-server-init.yml`), opens its ports
  (`k3s-open-ports.yml`) and emits a registration-ready kubeconfig (`k3s-kubeconfig.yml`)
  that registers as a `cloud=local` cluster.
- **KubeSolo** is on the 1.34 line as of v1.1.0, and its single-process control plane would
  otherwise suit the edge story in [`docs/kubesolo.md`](../kubesolo.md). It documents no way
  to pass API server arguments. Until that is established by experiment rather than hope, it
  is the riskier host.

So this is a **k3s** story. That is a real limitation to state plainly on the tab, not to
work around: the pattern's reach is self-managed control planes, and a customer running only
EKS cannot adopt it.

## 4. The demo must not touch the seed playbook's entry count

`spire-seed-entries.yml` writes 11 registration entries and
[`docs/spiffe.md`](../spiffe.md) asserts discovery returns exactly **8** of them. That number
is load-bearing — it is the assertion that caught the plugin defaulting its discovery path
filter to the mintable prefix, and a test reads it.

The workload's entry (a unix-UID selector, with `k8s` in its audience list) therefore goes in
the **new** playbook, not in `spire-seed-entries.yml`. Adding it to the seed set would move
the count and silently retire the one assertion that has already caught a real bug.

## What building it would take

| Play | Would do |
|---|---|
| `spire-oidc-provider.yml` | The SPIRE OIDC Discovery Provider beside the server, publishing `/.well-known/openid-configuration` and JWKS over TLS to an address the k3s API server can reach |
| `spire-agent-install.yml` | A SPIRE agent on the k3s node, joined with a join token — the lab installs the server only today, so there is no Workload API anywhere yet |
| `k3s-spiffe-auth.yml` | The `AuthenticationConfiguration` plus the `kube-apiserver-arg`, a ClusterRoleBinding whose subject is the SPIFFE ID, the workload's registration entry, and the exec-auth binary on the workload host |

All three follow the existing pattern: fetched by bare filename from the storage backend,
run through Config Management on `chrweav/ansible-winrm` (`ansible.posix` is needed and is
not in `ansible-cloud`). Uploading them to Storage is a prerequisite, as it is for the
`spire-*` four.

Two things to settle before writing them:

- **What the username should be.** The `sub` of a JWT-SVID is the SPIFFE ID
  (`spiffe://<trust-domain>/<path>`), so the RBAC subject is a URI. A `claimMappings.username`
  prefix is mandatory unless the claim is `email`, and whatever is chosen becomes the string
  every binding names — the same trap `docs/kubernetes.md` documents for Entitle's sanitized
  `entitle:karen.walker-weaverlab.xyz` subject, where nothing in this repo does the rewrite
  and the binding has to be read rather than assumed.
- **Whether the k3s node and the SPIRE server share a VM.** One VM is cheaper and the
  auto-delete timer already reaps it; two proves the agent actually attests over the network,
  which is the half worth proving. Two is probably right, and it doubles the lab's standing
  cost.

## What this would still not demonstrate

- **No revocation story.** Deleting the entry stops renewal; an SVID already issued stays
  valid for its TTL. Same boundary `docs/spiffe.md` already records, and short TTLs are the
  only mitigation.
- **No governance.** The whole point is that no credential is stored, which also means there
  is no managed account, no inventory row and nothing for Password Safe to rotate. That is
  the trade the comparison table in §1 exists to make legible, and it is the reason this tab
  never replaces the `ps-token` path.
- **Nothing about managed clusters**, per §3.
