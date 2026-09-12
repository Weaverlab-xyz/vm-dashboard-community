# Design: a workload reaching Kubernetes with a short-lived token

> **Audience:** contributor · **Profile:** `demo` · **Read this when:** you are about to build the Workload Lab's Kubernetes tab, or you are deciding whether a SPIFFE identity should be able to authenticate to a cluster at all.

**Built, and never run against a live pair.** The five stages in
[The playbooks](#the-playbooks) are written, their invariants are pinned by
`tests/test_playbook_spire.py` and `tests/test_playbook_k3s.py`, and the dashboard drives
them from the **Kubernetes** action on a SPIRE lab's row (`POST /api/spire-lab/{id}/k8s-link`,
job type `spirelab_k8s_link`). Every play is still marked NEVER LIVE-VALIDATED in its own
header. Treat the first real run as the validation.

**This is not a tab.** It was, briefly, and that was the wrong shape: a tab explaining a
by-hand procedure with copy-pasted join tokens is precisely what the SPIRE page exists to
replace — standing this up by hand is the afternoon that page eliminates. It is a panel on
the lab's own row, and the orchestrator passes the join token and trust bundle between hosts
so nothing is copied by hand.

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

That is a real short-lived token and it is **not** what this replaces. It is what the
pattern here has to be better than, on one axis:

| | Bound-mode SA token (built, live) | JWT-SVID over the Workload API (plays written, unvalidated) |
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

Note the discovery provider is a **third daemon**, not a SPIRE server flag, and it ships in
the `spire-extras` tarball rather than the `spire` one the server play fetches. It must also
be reachable by the API server over TLS, which makes it a network question as much as a
packaging one: `spire-oidc-provider.yml` refuses an `oidc_domain` of `localhost` for exactly
that reason. Neither is the SPIRE agent something the lab had before —
`spire-server-install.yml` deliberately installs none, because the plugin only ever talks to
the server's API.

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

## The playbooks

**Four, not the three this note first sketched.** The sketch put the workload's registration
entry in `k3s-spiffe-auth.yml`, which cannot work: `spire-server entry create` needs the
server's own CLI and its admin socket, and that play targets the k3s node. Anything the SPIRE
server has to be *told* is therefore its own play, on its own host.

| Play | Host | Does |
|---|---|---|
| `spire/spire-oidc-provider.yml` | SPIRE server | The OIDC Discovery Provider, publishing `/.well-known/openid-configuration` and JWKS over TLS on an address the API server can reach |
| `spire/spire-k8s-entry.yml` | SPIRE server | A join token for the k3s node, the workload entry carrying the `k8s` audience, and the trust bundle both later plays need |
| `spire/spire-agent-install.yml` | k3s node | The SPIRE agent, and a JWT-SVID fetched *as the workload account* to prove the chain before reporting success |
| `k3s/k3s-spiffe-auth.yml` | k3s server | The `AuthenticationConfiguration`, the apiserver flag as a `config.yaml.d` drop-in, an RBAC binding whose subject is the SPIFFE ID, and the exec-auth binary plus a credential-free kubeconfig |

Five stages, because a plain VM has to become a k3s server first
(`k3s/k3s-server-init.yml` runs before the four above). All are fetched by bare filename from
the storage backend and run through Config Management, as the `spire-*` four are — uploading
them to Storage is the same prerequisite.

Three things about the orchestration are worth recording, because none is recoverable from
the code alone:

- **The two hosts keep separate connection identities.** They are deployed independently
  and do not share an SSH key. A blank set for either means auto-derive from *that* host's
  deploy job — which already works, because `_find_cloud_deploy_meta` matches the deploy job
  on the target address and each stage targets its own machine. The tempting fallback,
  inheriting the SPIRE host's chosen account when the k3s set is empty, is the bug: it
  connects to one VM with another VM's credential and fails as
  `Permission denied (publickey)` several stages in. `_cred_fields` is the one place that
  decides, and a test pins that its k3s branch reads no `ansible_*` column.
- **Two hosts, both ATTACHED, never created.** The lab provisions no compute and this does
  not change that: both VMs come from the dashboard's own deploy rows through the existing
  `resolve_host`, for the reason `docs/spiffe.md` already gives — accepting an address the
  request supplies would be accepting a request to run privileged playbooks against a host
  of the caller's choosing. `start_k8s_link` refuses the SPIRE host as the k3s node, because
  an agent attesting over loopback proves the mechanism but not that it crosses a network.
- **The join token crosses hosts as a REF, not a value.** It is minted on the SPIRE host and
  spent on the k3s node minutes later, so it can be neither stored on the row nor put in a
  job log — a job's output *is* a captured log. The entry play writes it to Secrets Safe
  under the lab's own folder and the agent stage binds it through `secret_vars`, which
  resolves at run time and is scrubbed from the output. That also keeps every `vars_for`
  builder reading only the row and config, which is what lets a resumed run rebuild an
  identical one.
- **The entry stage always re-runs.** A join token is one-use and expires in ten minutes, so
  a link resumed after a later stage failed cannot reuse the first attempt's token — it
  would fail attestation with a message about an unknown token, which reads like a broken
  agent. Re-running mints a fresh token and another node entry; node entries are excluded
  from discovery, so the documented 8-of-11 count does not move.

The ACL is deliberately **asymmetric**: only the SPIRE host gains a rule, for tcp/8081 and
tcp/8443, sourced from the k3s node's private address alone. The k3s node needs nothing
inbound, because the workload runs on it.

Four things the writing settled, each of them a trap:

- **The node entry must come from the join token, not by hand.** `spire-server token generate
  -spiffeID X` creates the node registration entry itself, with the token's own UUID as the
  selector value — the only value an attesting agent can present. Copying
  `spire-seed-entries.yml`'s hand-written `join_token:bootstrap` entry produces something no
  agent ever matches; that play has no agent at all, and its node entry exists only to give
  the workload entries a parent and to give discovery something to exclude.
- **`WorkloadAttestor "unix"` is not optional.** Without it the agent cannot learn a caller's
  UID, every `unix:uid` selector matches nothing, and the fetch fails with "no identity
  issued" — which reads like a missing entry on the server rather than a missing plugin on
  the node.
- **`PrivateTmp` must stay off the agent's systemd unit.** The Workload API socket lives under
  `/tmp`; a private namespace gives the agent its own, the agent passes its own health check,
  and no workload can ever reach it.
- **The agent play's verification has to run as the workload.** Fetching as root is attested
  as `unix:uid:0`, matches no entry, and fails. Fetching as the workload account proves the
  entry, the selector, the audience and the attestation at once — the single assertion that
  makes the other plays meaningful.
- **The issuer must be a hostname, and this one nearly shipped wrong.** `spire-server x509
  mint -dns <x>` writes a **DNS** SAN, and Go verifies an **IP** SAN for a URL like
  `https://10.0.0.5:8443`. An IP issuer therefore fails TLS verification at the API server
  no matter how correct the trust bundle is, and the error reads as a bad CA — which sends
  you looking in entirely the wrong place. So the provider is minted for
  `oidc.<trust-domain>` and `k3s-spiffe-auth.yml` writes the `/etc/hosts` entry that
  resolves it to the SPIRE host's private address.

And two that are still open:

- **What the username should be.** The `sub` of a JWT-SVID is the SPIFFE ID
  (`spiffe://<trust-domain>/<path>`), so the RBAC subject is a URI. A `claimMappings.username`
  prefix is mandatory unless the claim is `email`, and whatever is chosen becomes the string
  every binding names — the same trap `docs/kubernetes.md` documents for Entitle's sanitized
  `entitle:karen.walker-weaverlab.xyz` subject, where nothing in this repo does the rewrite
  and the binding has to be read rather than assumed. `k3s-spiffe-auth.yml` defaults
  `username_prefix` to `spiffe:`, so the subject reads `spiffe:spiffe://<td>/<path>`. It looks
  wrong and is correct, and it is the first thing to check on a 403.
- **Whether the k3s node and the SPIRE server share a VM.** One VM is cheaper and the
  auto-delete timer already reaps it; two proves the agent actually attests over the network,
  which is the half worth proving. The plays assume two — `spire-agent-install.yml` takes
  `spire_server_address` and refuses to bootstrap without a trust bundle — though nothing
  stops pointing it at the loopback. Two doubles the lab's standing cost.

## What this still does not demonstrate

- **No revocation story.** Deleting the entry stops renewal; an SVID already issued stays
  valid for its TTL. Same boundary `docs/spiffe.md` already records, and short TTLs are the
  only mitigation.
- **No governance.** The whole point is that no credential is stored, which also means there
  is no managed account, no inventory row and nothing for Password Safe to rotate. That is
  the trade the comparison table in §1 exists to make legible, and it is the reason this tab
  never replaces the `ps-token` path.
- **Nothing about managed clusters**, per §3.
