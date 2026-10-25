# Browsing and editing secrets

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want to see, add, change or delete individual secrets from the Secrets page, and what each backend allows.

Part of [Secrets Management](../secrets-management.md).

Migration covers the "move everything across" path. For day-to-day work
the `/secrets` page also provides full CRUD on individual secrets in any
configured backend — no need to drop into the AWS console, Azure Portal,
GCP console, or `ps-cli` for one-off edits.

The **Browse & Edit** section lets you:

- Pick any backend (Database, AWS SM, Azure KV, GCP SM, BT Secrets Safe).
- List every secret in that backend.
- Create a new secret, edit an existing one, or delete one.
- For BeyondTrust Secrets Safe specifically: navigate the full
  `Safe → Folder → Secret` hierarchy with create / rename / delete
  actions on Safes and create / delete on Folders, all driven by the
  ps-cli subcommands (`create-safe`, `update-safe`, `delete-safe`,
  `create`, `delete`). If a secret only sits one level deep, that
  folder acts as both the Safe and the Folder (per the BeyondInsight
  convention).

## JSON-only values

All secret values are constrained to **valid JSON** — same format across
every backend. The editor enforces this client-side (live parse) and
server-side (`validate_json_value` raises before the backend write). The
default scaffold for new secrets is:

```json
{
  "username": "",
  "password": ""
}
```

Add whatever fields your consumers expect — multi-line bodies (private
keys, certificates) work fine inside a JSON string. The **Format JSON**
button in the editor pretty-prints the current value to canonical
multi-line layout. This uniformity means downstream code can `json.loads`
the value from any backend without backend-specific parsing.

## What the dashboard can and cannot do per backend

| Operation | DB | AWS SM | Azure KV | GCP SM | BT Secrets Safe |
|---|---|---|---|---|---|
| List secrets | ✅ | ✅ | ✅ | ✅ | ✅ (per folder) |
| Read secret value | ✅ | ✅ | ✅ | ✅ | ✅ (file secrets: text only) |
| Create / update secret | ✅ | ✅ | ✅ | ✅ | ✅ (text or file) |
| Delete secret | ✅ | ✅ | ✅ | ✅ | ✅ |
| List Safes | — | — | — | — | ✅ |
| Create / rename / delete Safe | — | — | — | — | ✅ |
| List Folders | — | — | — | — | ✅ |
| Create / delete Folder | — | — | — | — | ✅ |

**Reading a file secret takes a second call, and it is made for you.** `ps-cli secrets
get` returns a file secret's `FileName` and `FileHash` and no contents — there is no
payload field in that projection for `--decrypt` to fill — so the read would otherwise
resolve to an empty string rather than failing. The dashboard detects `SecretType: File`
and fetches the body from `GET Secrets-Safe/Secrets/{id}/file/download` over `ps-cli
raw`. **Text payloads only:** that endpoint is byte-faithful, but every ps-cli route
decodes its response to text, so a PEM bundle, a config file or JSON round-trips and a
PKCS#12 or DER payload does not. A payload showing decode damage is **refused rather
than returned**, since a corrupt bundle that looks like a value fails much later and
somewhere unrelated. See
[Password Safe → Troubleshooting](../../beyondtrust/password-safe/troubleshooting.md).

**Writing one goes through a file, because ps-cli has no inline route.** The type is
inferred from the argument: `--text` makes a text secret, `-fp <path>` makes a file
one, and there is no way to hand the CLI a file body directly. The dashboard writes the
value to a `0600` file inside a `0700` private directory, passes that path, and removes
the directory afterwards — including when the write fails, which is when a leftover
would go unnoticed. The file's *name* is chosen rather than left as a temp name,
because Password Safe records the basename as the secret's `FileName`. Dashboard config
values stay **text** secrets by default; the file type is opt-in.

**An edit preserves the type.** Writing `--text` over a file secret would not just fail
to update it — it would change what the secret *is*, and reads route on that field. So
an update reads the existing type first and matches it, carrying the recorded
`FileName` over so the edit does not rename the file either. One consequence worth
knowing: a file secret's body comes back on ps-cli's stdout, which is stripped, so a
**trailing newline cannot survive a round trip**. That is not treated as a failed
write.

BeyondTrust hierarchy management is driven through the
[ps-cli subcommands](https://docs.beyondtrust.com/bips/docs/ps-cli-application):
`list-safes` / `create-safe` / `update-safe` / `delete-safe` for Safes,
and `list` / `create` / `delete` (parent identified via `-pid`) for
Folders. Deletes require the container to be empty — ps-cli refuses to
remove a Safe that still has Folders or a Folder that still has
Folders or Secrets, and the dashboard surfaces that error verbatim.

---

## Non-goals — what Browse & Edit deliberately does *not* include

The Browse & Edit feature is intentionally lightweight day-to-day CRUD.
It is **not** a replacement for the audit-and-compliance capabilities
of an enterprise vault (BeyondTrust Password Safe, CyberArk PAM,
HashiCorp Vault Enterprise, AWS Audit Manager + CloudTrail, etc.):

- **No enterprise audit trail.** The dashboard does not record who
  viewed, changed, or deleted a secret beyond the standard request log.
  If you need an immutable, queryable, regulator-grade audit trail —
  with session recording, four-eyes approval, time-bounded checkout,
  break-glass replay, etc. — keep using your vault's native UI / API
  for those workflows. The dashboard's Browse & Edit is for operator
  convenience, not compliance.
- **No plans to add it to the community edition.** Building a credible
  audit-and-compliance layer is a large undertaking and is not on the
  community roadmap. The community build exists to let small teams
  operate a multi-cloud workstation lab, not to replace their
  procurement decision for a PAM/secret-vault platform.
- **No plans on the SaaS roadmap either.** The hosted SaaS edition's
  differentiation (see [SaaS comparison](../../editions/comparison.md)) is
  managed hosting and operator UX — not vault feature parity.
  Customers who need an enterprise vault should keep using one and
  point the dashboard at it via the existing migration / reference
  flow above.

If you spot a per-secret operation in this page that should land in
your vault's audit log but doesn't, treat that as a sign you should
do that operation in the vault's native UI instead — not as a gap to
file against the dashboard.
