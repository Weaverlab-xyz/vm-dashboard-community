# Config Migration

Move a dashboard's **Settings** configuration from one instance to another —
dev to prod, a laptop to a hosted deployment, or a rebuild after a host dies.

> This is about dashboard *configuration*, not the Config Management feature.
> For Ansible playbooks and drift detection see
> [config-management.md](config-management.md).

---

## Read this before you reach for pg_dump

The obvious approach does not work, and it does not tell you so.

Every value in the `app_config` table is Fernet-encrypted with a key derived
from `JWT_SECRET_KEY`. Two instances have different JWT keys — the onboard
script generates one per deployment — so a `pg_dump` of that table restores as
ciphertext the target cannot read.

The part that makes this genuinely dangerous is the failure mode.
`config_service._decrypt` catches `InvalidToken` and returns the raw string, so
that it can keep reading plain-text legacy rows. A restored row therefore
resolves to *the ciphertext itself*: no exception, no log line, and a Settings
panel full of base64. You find out when a cloud provider rejects the
credential.

So the migration moves **plaintext**, and lets the target re-encrypt with its
own key. That is what `POST /api/setup/import` already does for the
[sandbox bootstrappers](CLOUD_SANDBOX.md), and this tool is the other half of
that path: the export the sandbox scripts never needed.

---

## What moves

Everything reachable from the **Settings** panel:

| | |
|---|---|
| Cloud credentials | AWS, Azure, GCP, OCI — from the setup wizard |
| Integration panels | all 21: BeyondTrust, Entitle, Portainer, Ansible, Kubernetes, Databases, Cost Explorer, Guardrails, Auto-delete, Notifications, SSO, Worker, … |
| Feature flags | including the preview flags |
| Per-region resource sets | `<cloud>_region_configs`, merged rather than replaced |
| Storage and Secrets backends | `storage_*` and `secrets_*` keys |
| Notification endpoints | the webhook rows edited in Settings → Notifications |

Vault references migrate as references. If a secret lives in
`azure_kv://bt-client-secret`, the bundle carries that pointer and the value
never leaves the vault — see [secrets-management.md](secrets-management.md).
Migrating your secrets to a vault *before* exporting is the cleanest way to run
this whole exercise.

## What does not, and why

**Held back automatically** — copying these breaks the target:

- **Instance identity.** `public_base_url`, `trusted_proxy_hosts`,
  `agent_base_url`, the WebAuthn origin, listener and database settings. The
  proxy host in particular must be the literal IP of *this* deployment's proxy;
  see [SECURITY.md](../SECURITY.md).
- **Handles on live resources.** `rancher_server_url`, `portainer_pat`,
  `entitle_agent_token_ref`, Web Jump tfstate, the expiry sweeper's arm time.
  The application writes these itself, and they point at infrastructure the
  *source* provisioned. A target holding them believes it owns machines it has
  never seen.
- **Host filesystem paths.** Meaningless on a different host, and on Azure
  Container Apps there is no volume at all.
- **On-premises targets** — VMware, Proxmox, vSphere, Hyper-V, Nutanix, XCP-ng,
  UNC storage. Portable in form, but a cloud-hosted dashboard has no route to
  your lab. Pass `--include-on-prem` if you want them staged anyway.

**Out of scope** — these live on their own admin pages, not in Settings, and
need moving by hand: users, workgroups, OIDC group mappings, registered images,
remote agents, secret vault registrations.

**Cannot be migrated at all:**

- **Security keys.** WebAuthn credentials are bound to the origin, so keys
  enrolled against `localhost` are invalid at `dash.example.com`. Re-enrol.
- **Personal access tokens.** Stored as hashes; re-issue them.
- **Remote agents.** The private half of an agent's Ed25519 identity never
  leaves the agent host. Re-enrol against the new audience — see
  [remote-agents.md](remote-agents.md).

**Deliberately never migrated:** deployed databases, clusters, gateways and
desktops. Two dashboards holding deploy records for one set of real resources
will both run the auto-delete sweeper against them, and Terraform state has a
single owner.

---

## Doing it

Both launchers take the same arguments and call the same implementation:

```bash
./scripts/migrate-config.sh --help
```

```powershell
.\scripts\Migrate-Config.ps1 -Command export -?
```

### 1. Export

From a Compose instance, prefer `--via docker`:

```bash
./scripts/migrate-config.sh export --source http://localhost:8001 --via docker
```

It execs into the container and reads the config store directly, which is the
only way to capture two things the HTTP API withholds by design: the four keys
`GET /api/setup/config` redacts (`aws_secret_access_key`, `azure_client_secret`,
`azure_oauth_client_secret`, `gcp_service_account_json`) and webhook URLs, which
are themselves bearer credentials and are never returned.

Without Docker access, `--via http` works against any reachable instance and
tells you exactly which keys came back redacted so you can set them by hand.

The bundle lands in `~/.dashboard-migrate/` at mode 0600. **It contains live
credentials.** Delete it after cutover; it is gitignored, but it should not be
in a checkout in the first place.

### 2. Review

It is a plain JSON file, and it is meant to be read:

- `config` — what will be written
- `regions` — per-region sets, keyed by cloud
- `excluded` — every key held back, with the reason
- `annotations` — per key: is it a secret, is it a vault pointer, was it redacted

Edit it if you want. Anything on the denylist is refused again at import, so a
hand-edit cannot reintroduce a key that breaks the target.

### 3. Diff

Read-only. Safe to run against production at any time:

```bash
./scripts/migrate-config.sh diff \
    --bundle ~/.dashboard-migrate/bundle-20260807T142211.json \
    --target https://dash.example.com
```

You get `ADD` / `CHANGE` / `SAME`, with secret values shown as `‹secret›`. A key
the *target* has redacted reports as `UNKNOWN` — the comparison is genuinely
impossible, so the tool says so instead of guessing.

### 4. Snapshot the target

Before writing anything, export the target's own config. That file is your
rollback artifact, and it re-imports with the same tool:

```bash
./scripts/migrate-config.sh export --source https://dash.example.com \
    --out ~/.dashboard-migrate/target-pre.json
```

### 5. Import, in tranches

`--apply` is required to write. Without it, `import` behaves as `diff`.

`--only` takes a key prefix and is repeatable, so a cutover can be taken in
stages with a look at the UI between each:

```bash
BUNDLE=~/.dashboard-migrate/bundle-20260807T142211.json
TARGET=https://dash.example.com

./scripts/migrate-config.sh import --bundle "$BUNDLE" --target "$TARGET" --only aws_ --apply
./scripts/migrate-config.sh import --bundle "$BUNDLE" --target "$TARGET" --only azure_ --apply
./scripts/migrate-config.sh import --bundle "$BUNDLE" --target "$TARGET" --apply
```

Re-running is idempotent — `set_many` upserts and region sets merge — so a
second pass produces an all-`SAME` diff and writes nothing.

---

## Region sets merge by default

`<cloud>_region_configs` is one JSON blob holding every region's resource set.
The exporter takes it apart and ships
`<cloud>_region.<region>.<field>` keys instead, because those route to
`merge_region_fields` on the way in and combine field by field.

Shipping the blob whole would take the replace-on-save path and delete every
region entry that exists only on the target — silently, with a success
response. If that is what you actually want, `--regions replace` is there.

---

## When things go wrong

**`Cannot reach <url>`** — the tool checks `/api/setup/status` first, which
needs no auth. If that fails, nothing else will.

**TLS errors behind a corporate proxy.** A TLS-inspecting proxy re-signs
everything with its own root. Point the tool at that root:

```bash
./scripts/migrate-config.sh diff --bundle b.json --target https://dash.example.com \
    --ca-bundle /usr/local/share/ca-certificates/corp-root.crt
```

`--insecure` exists and prints a warning every time it is used.

**`Login failed`.** A `vmcli_` personal access token will not work here.
`/api/setup/*` decodes the JWT itself rather than going through the dependency
that understands PATs, so the migration needs a real admin login — or a JWT
passed as `--token`.

**Named vault references.** If the config points at a *named* vault
(`azure_kv://primary/…` rather than `azure_kv://…`), register that vault on the
target's `/secrets` page first. An unregistered vault id is parsed as part of
the secret name instead, so the reference resolves to the wrong thing rather
than erroring. The tool warns when it sees one.

**`--via docker` on Windows.** Docker Desktop's WSL backend usually leaves
`docker` off the Windows PATH. `Migrate-Config.ps1` notices and re-runs the
command inside WSL; everything else runs natively.
