# Certificate lab prerequisites

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are about to build a certificate lab and need each cloud's prerequisites in place first.

Part of [The Certificate Lab](../certificate-lab.md).

## Password Safe

1. **BeyondInsight 26.1 or later.** This is a hard floor, not a recommendation. Before
   26.1.0.878, file secrets downloaded through the API were larger than the original and
   did not match the copy downloaded from the web console — so a PKCS#12 retrieved by a
   non-human identity on an earlier build would have been **corrupt**.
2. **The `.psplugin` imported** and the `Certificate` platform created — and, to onboard a
   subordinate CA, the **second** package too, which creates the `Subordinate CA`
   platform. They carry different plugin ids, so both install side by side and neither
   overwrites the other; upload each at **Configuration → Privileged Access Management →
   Platform Plugins → Create New Platform Plugin**. The dashboard resolves both platforms
   live by name through `GET /Platforms`, so a renamed platform just needs its new name in
   Settings (`cert_ps_platform`, `cert_ps_subca_platform`). See
   [Two packages, two platforms](../certificates/packages.md).
3. **A BeyondInsight registration for the plugin to reach Secrets Safe with.** The
   dashboard builds the functional account itself; this is the one half it cannot derive
   from the CA build. One account carries **two** credentials, and there are two shapes of
   the BeyondInsight one. **The plugin reads both and prefers OAuth.**

   | | Username | Password | API key | API secret |
   |---|---|---|---|---|
   | **OAuth** (preferred) | `<ca-account>` | `<ca-secret>` | OAuth client id | OAuth client secret |
   | **Packed** (fallback) | `<ca-account>:<bi-run-as-user>` | `<ca-secret>:<bi-api-key>` | — | — |

   `ECredentialType` is a flags enum and `CredentialParameter` carries `ApiKey` and
   `ApiSecret` as fields of their own, so one account can be `Password, ApiKey` at once.
   The OAuth path removes three things: no run-as user has to exist, **nothing is packed**
   — so a CA secret *or a CA account name* may contain a colon, which the packed form
   could not express — and the plugin presents a short-lived bearer token instead of a
   long-lived static key. On the packed path both fields are split on the **last** colon,
   deliberately: a BeyondInsight username and an API registration key contain no colon,
   but a certificate authority password may contain anything at all.

   **Check which one your console supports before you configure it.** Open the functional
   account form for a managed system on the `Certificate` platform and see whether Password
   Safe offers an **API key** credential type. The SDK models it fully, but whether the
   console exposes it for a *plugin-supplied* platform is host behaviour and is
   unverified — two minutes here decides which row above you use, and the plugin works
   either way.

   In Settings → Certificate Lab:

   | Setting | For |
   |---|---|
   | `cert_ps_bi_auth` | `auto` (default), `oauth`, or `apikey`. Pin it once you have checked |
   | `cert_ps_bi_client_id` / `cert_ps_bi_client_secret` | The OAuth path. An API registration permitting the **client credentials** grant, with write access to the Secrets Safe folder. Blank falls back to the dashboard's own `pscli_client_id`/`pscli_client_secret` — same tenant by construction, but that registration administers the whole of it, and this one is handed to a plugin on a Resource Broker, so prefer a dedicated one |
   | `cert_ps_bi_api_key` / `cert_ps_bi_run_as_user` | The packed path. The run-as user falls back to `pscli_api_account_name` |

   `auto` resolves a dedicated client id first, then an explicitly set `cert_ps_bi_api_key`,
   then the inherited `pscli_*` pair. The API key outranks the inherited pair on purpose:
   an install already working on the packed path must not be moved onto an unproven one by
   an upgrade, because a functional account that authenticates as nothing onboards **green**
   and fails hours later at a rotation.

   **Building the CA creates this account** — see [Building a CA](building-a-ca.md) below.
   The manual path is `cert_ps_functional_account_mode = reference`, which is the right
   setting for a CA this dashboard did not build.

   > Switching path on a CA that **already has** an account mints a second one. The two
   > shapes differ in the account name, so Password Safe sees a new object rather than a
   > duplicate, and only the new one is tracked — **Wire up Password Safe** says so, and
   > the old account has to be removed by hand once nothing is registered against it.
4. **A Secrets Safe safe** for the bundles. The dashboard creates the *folder tree* beneath
   it; it never creates the safe, which carries its own ACL.

## GCP

The sandbox setup scripts enable `privateca.googleapis.com` and grant the sandbox service
account `roles/privateca.admin` plus `roles/iam.serviceAccountAdmin` and
`roles/iam.serviceAccountKeyAdmin`. If your project was bootstrapped before this feature,
re-run `scripts/sandbox/Linux/setup-gcp.sh` (or `Setup-GcpSandbox.ps1`) — a missing API
surfaces as a `terraform apply` failure naming the service.

**The two `serviceAccount*` roles are not optional, and they were added later than the
first two.** The module creates the plugin's own *enrollment* service account and a key for
it, so a project with only `privateca.admin` builds the CA pool and then fails on
`iam.serviceAccounts.create` — an `IAM_PERMISSION_DENIED` that names neither this feature
nor the setup script. If you re-ran the script before they were added, re-run it again or
grant them by hand:

```bash
for ROLE in privateca.admin iam.serviceAccountAdmin iam.serviceAccountKeyAdmin; do gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SANDBOX_SA" --role "roles/$ROLE" --condition=None; done
```

Both the API enable and the IAM grants are eventually consistent — wait a couple of minutes
before retrying a build, or it can fail the same way once more. Where
`constraints/iam.disableServiceAccountKeyCreation` is enforced, the key grant is not enough
and the CA build cannot complete as written; that is an org policy rather than a role.
