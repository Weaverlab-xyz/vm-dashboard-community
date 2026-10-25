# Troubleshooting Password Safe

> **Audience:** operator · **Profile:** `both` · **Read this when:** sign-in, checkout, onboarding or rotation through Password Safe fails.

Part of [BeyondTrust Password Safe](../password-safe.md).

**"ps-cli not found"** — `ps-cli` comes from the `beyondtrust-bips-cli` pip package in
`web_dashboard/requirements.txt`, so this means the image was built without installing
requirements, or something is shadowing `PATH`. Confirm with
`docker compose exec app ps-cli --version`; rebuild the image if it is genuinely absent.

**"Authentication failed" from ps-cli** — verify the Client ID and Client Secret
in **Settings → Integrations → Password Safe** match the API Registration in
Password Safe and that the registration has not expired.

**Secrets retrieved are empty** — check that the API Registration has **Secrets →
Read** and **Credentials → Read** permissions, and that the specific secret is
in scope for the registration.

**A file secret retrieves with no contents** — expected, if you reached for
`ps-cli secrets get`. That verb projects a **per-type** field set, and the file one is
metadata only: `FileName` and `FileHash`, with no content field at all (a text secret gets
`Text`, a credential gets `Password`). `-d` / `--decrypt` cannot rescue it — the flag only
adds a `decrypt=true` query parameter, and there is no payload field in the projection for
it to fill. The body comes from a different verb, which takes **only** a GUID (there is no
`--title` and no `--path`), so fetching a file secret by name is always two calls:

```bash
ps-cli secrets get -t 'api-gateway-chain'
```

```bash
ps-cli secrets download -id <GUID-from-the-Id-column> -s ./chain.pem
```

`download -s` writes the file `0600`. One adjacent trap: `secrets get -id <GUID> -d`
**ignores** `--decrypt` — only the `--title` branch enables it — so resolving by ID hands
back a masked credential and no error.

A `raw` call reaches the same endpoint in **one** step if you already hold the GUID.
This is the route **the dashboard itself takes** — `read_bt_secrets_safe` spots
`SecretType: File` and follows up with it, so a `bt_safe://` reference to a text bundle
resolves normally rather than to an empty string:

```bash
ps-cli raw GET "Secrets-Safe/Secrets/<GUID>/file/download"
```

**Neither route is binary-safe, and that is a ps-cli limit rather than an API one.** The
endpoint itself returns `application/octet-stream` — raw bytes, faithfully — but every
path ps-cli offers decodes them to text before you see them: `download-secret-file` hands
back `response.text`, and `raw` falls through its JSON parse to print `response.text` too.
A PEM bundle is ASCII and survives; a `.pfx`, `.p12` or DER payload is **corrupted rather
than refused**. So keep certificate material in file secrets as PEM — or, if it genuinely
has to be a PKCS#12, call
[`GET .../file/download`](https://docs.beyondtrust.com/bips/reference/get-api-public-v3-secrets-safe-secrets-secretid-file-download)
directly and keep the bytes, the way the agent worker already calls `Requests` and
`Credentials`.

In a playbook none of this applies for text bundles — the `beyondtrust.secrets_safe`
lookup resolves all three types in one call by `folder/title` (it decodes as text too, so
the PEM-only caveat carries over). See [Secrets in a Remote Worker run](../../platform/ansible/secrets.md#in-playbook-password-safe-lookup-beyondtrustsecrets_safe).

**A checkout returns `4031` / 403** — usually the API identity is missing the **Requestor**
role or an access policy granting View on a Smart Rule containing the account. There is no
Smart Rule API, so this is out-of-band; see [Operator prerequisites](kubernetes-tokens.md#operator-prerequisites).

Password Safe returns the same 4031 when the account is not **API-enabled**, and when the
`SystemID` on the request does not own the `AccountID` — `POST Requests` authorises the
*pair*. So a 4031 that survives a correctly granted Requestor role is not the grant. The
dashboard sends the account's real managed system (read from the account when the caller
does not already hold it) and quotes Password Safe's response body in the job error, which
is where the numeric code that separates these lives: `4034` is a request awaiting approval
and `4035` the account's concurrent-request cap — both also 403.
