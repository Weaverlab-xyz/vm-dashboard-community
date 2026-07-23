# Password Safe in-playbook lookup samples (`password-safe/`)

Playbooks that fetch their own secrets from **BeyondTrust Password Safe at runtime** using
the [`beyondtrust.secrets_safe`](https://galaxy.ansible.com/ui/repo/published/beyondtrust/secrets_safe/)
Ansible Galaxy collection (see the [BeyondTrust Ansible docs](https://docs.beyondtrust.com/bips/docs/ps-ansible)).
This is the **in-playbook** pattern — Password Safe stays the source of truth and the play
pulls exactly what it needs mid-run.

It is **complementary** to the dashboard's out-of-band **Use a secret → managed account**
checkout (see [../../docs/integrations/ansible.md](../../docs/integrations/ansible.md#managed-account-checkout-beyondtrust-password-safe)):

| | Out-of-band checkout (existing) | In-playbook lookup (these samples) |
|---|---|---|
| Who calls Password Safe | the dashboard (ps-cli), before the run | the playbook, mid-run, via the lookup plugin |
| What the play sees | an injected var / connection key | it fetches values itself |
| Best for | the **connection** credential (SSH/WinRM/become) | **app** secrets, API tokens, DB creds used inside tasks |

## Credentials — auto-injected

The lookup runs on the Ansible **controller** (the runner container) and reads three env
vars. When **BeyondTrust is enabled** (`beyondtrust_enabled`) and the ps-cli OAuth client is
configured (`pscli_api_url` / `pscli_client_id` / `pscli_client_secret`), the dashboard
**auto-injects** them into every runner — you supply nothing:

```
PASSWORD_SAFE_API_URL      # normalized to https://<host>/BeyondTrust/api/public/v3
PASSWORD_SAFE_CLIENT_ID
PASSWORD_SAFE_CLIENT_SECRET
```

The client secret rides the same per-run channel each runner already uses for the SSH key
(ECS runTask override, Cloud Run plain env, ACI `secure_value`, a `0600` env-file locally) —
never the command line — and is scrubbed from job output. **No `ansible_cloud_ephemeral_secrets_enabled`
gate is needed** (that's only for the checked-out managed-account path); auto-wire works on
all runners: Local, ECS, ACI, Cloud Run.

The OAuth client (API Registration) needs the same permissions as the ps-cli integration:
**Secrets → Read**, **Requests → Create**, **Credentials → Read**, plus Managed Account /
Secrets Safe scope for the paths a play touches.

### Running standalone (no dashboard)

Export the three env vars, then run against the built runner image:

```bash
docker run --rm -v "$PWD:/pb" \
  -e PASSWORD_SAFE_API_URL -e PASSWORD_SAFE_CLIENT_ID -e PASSWORD_SAFE_CLIENT_SECRET \
  chrweav/ansible-cloud ansible-playbook -i 'localhost,' -c local \
  /pb/examples/playbooks/password-safe/lookup-secret.yml -e secret_path=Folder/Title
```

## Path formats

| `retrieval_type` | `secret_list` format | Example |
|---|---|---|
| `MANAGED_ACCOUNT` | `system_name/account_name` | `web01/svc_deploy` |
| `SECRET` | `folder/secret_title` | `app-secrets/appuser-password` |

Pass a comma-separated list (and `wantlist=True`) to fetch several at once.

## The samples

| File | Target kind | Runner image | What it shows |
|---|---|---|---|
| `lookup-managed-account.yml` | localhost | either | Fetch a rotated managed-account password (`MANAGED_ACCOUNT`) |
| `lookup-secret.yml` | localhost | either | Fetch a stored secret (`SECRET`) and write it to a `0600` file |
| `vm-secret-to-host.yml` | Linux VM (SSH) | `ansible-winrm` | Fetch a secret on the runner, deliver it to the target host |
| `db-credential-from-ps.yml` | Cloud database | `ansible-cloud` | Fetch a DB role password from PS, create the Postgres role |
| `onboard-safe-and-account.yml` | localhost | either | **Management** — create a safe/folder/secret via `beyondtrust.password_safe` modules |

> `onboard-safe-and-account.yml` uses the write/management modules, whose argument names vary
> by BeyondInsight/Password Safe version — verify them against the collection docs before use.

## Notes

- Both runner images ship the collections (`beyondtrust.secrets_safe` +
  `beyondtrust.password_safe`, via `beyondtrust-bips-library`) — see
  [`runners/ansible-winrm/`](../../../runners/ansible-winrm/) and
  [`runners/ansible-cloud/`](../../../runners/ansible-cloud/). Rebuild + push those images
  before relying on these samples.
- Every task that touches a retrieved value uses `no_log: true`; adapt the placeholder
  `secret_list` paths (or pass them as extra vars) before running against real data.
- `verify_ca` defaults to `true`; set it `false` only for a self-signed Password Safe.
