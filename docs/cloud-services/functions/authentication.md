# How functions authenticate

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are deciding how callers prove who they are to a function, and how a function reaches its cloud and the dashboard.

Part of [Cloud Functions](../functions.md).

Two **independent** gates. Getting past one does not get you past the other.

1. **The cloud's front door** — Lambda Function URL `AWS_IAM`, the Azure host key,
   or Cloud Run `roles/run.invoker`.
2. **A shared bearer secret**, checked inside the handler on every request,
   identically on all three clouds.

The defaults leave the front door open on AWS and GCP, because Entitle authenticates
with a plain header and would otherwise need a proxy — so the bearer secret is the
gate there. Tighten `auth_mode` per function if you front it with a gateway.

**Azure is the exception: its front door cannot be opened.** The Functions host
requires its own key on every route but `/api/health`, so the dashboard fetches it
after the deploy, stores it as `cloudfn/{id}/invoke-key`, and sends it as
`x-functions-key` — from Test invoke, and as a second header on the Entitle REST
integration it registers. A hand-written `curl` needs it too; the **Endpoint** button
shows it.

Which of the two gates refused you is in the **body**, not the status code:

| Response to an authenticated route | Who refused | What to do |
|---|---|---|
| `401`, **empty** body | the Azure host key (or GCP's `run.invoker`, or an AWS Function URL permission) — the front door, before your code ran | re-run Test invoke: it re-fetches the key from ARM. `GET /api/health` returning 200 confirms the app itself is healthy |
| `401 {"error": "unauthorized"}` | `fnruntime.auth` inside the handler | the bearer secret the dashboard holds is not the one the function verifies — redeploy to mint a matching pair |
| `500 {"error": "function not configured", ...}` | the workload | the named setting is missing; `POST /check_config` lists all of them |
| `500 {"error": "internal error", "request_id": ...}` | the workload raised | nothing about the cause is in the body, by design. For `db_grant`, `POST /check_config` opens a real connection and reports what failed — TLS, the firewall, the VNet route, the admin credential — as a sentence. The full detail (driver error code and message) is in the function's own log stream, and `FN_DEBUG=1` adds a traceback there |

The secret is minted at deploy, stored encrypted (`cloudfn/{id}/bearer`), and shown
via the **Endpoint** button. The handler **fails closed**: if it cannot resolve the
secret it returns 500, never 200 — including when the secret store is unreachable or
the function's role has lost its read grant.

**Where the function keeps it**, which is not the same question as where the
dashboard keeps it:

| Cloud | Where | Who can read it |
|---|---|---|
| **AWS** | a Secrets Manager secret the module creates (`<name>-fn-secret`); only its ARN is in the function's environment | the function's role, and principals you grant on that secret |
| **GCP** | Secret Manager, injected as a `secret_environment_variables` entry | the runtime service account |
| **Azure** | Key Vault, referenced from an app setting the platform resolves | the function's own managed identity, and principals you grant on the vault |

The AWS arrangement matters more than it looks: a Lambda's environment is returned
in full by `lambda:GetFunctionConfiguration`, which the AWS-managed **ReadOnlyAccess**
policy grants — so a bearer token stored there is readable by every read-only
principal in the account, and reading it is enough to call the function. It now costs
one Secrets Manager secret per function (~$0.40/month).

**Azure needs a Key Vault, and a deploy without one is refused rather than
downgraded to a readable app setting.** Configure it under Settings → Secrets →
Azure Key Vault (`secrets_azure_kv_url`); set `azure_key_vault_resource_group` too if
the vault does not live in the dashboard's own resource group. You do **not** grant
anything per function: the module creates a user-assigned identity, grants it read on
that vault, and points the app's Key Vault reference resolution at it.

> Why a user-assigned identity rather than the app's system-assigned one: a
> system-assigned identity does not exist until the app has been created, so the app
> would boot with an unresolvable reference and only recover when Azure re-checks
> references — up to 24 hours later. A user-assigned identity can be granted *before*
> the app exists, so the first cold start resolves. The system-assigned identity is
> still created, so an existing manual grant keeps working.

> The secret passes through Terraform state on AWS and GCP, because the resource that
> creates it takes the value. On Azure it does not — the dashboard writes it to the
> vault and Terraform only ever sees the reference. State lives in your configured
> backend; treat it as sensitive.
