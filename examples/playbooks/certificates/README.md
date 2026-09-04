# Certificate plugin samples (`certificates/`)

The two halves of the lab for the Password Safe **Certificate** custom plugin: an mTLS
endpoint that demands a client certificate, and a consumer that fetches one out of
Password Safe and authenticates with it.

Feature reference: [docs/certificates.md](../../../docs/certificates.md).
The ADCS half lives next door in
[`windows/adcs-pipeline-template.yml`](../windows/adcs-pipeline-template.yml), because it
targets a Windows CA over WinRM rather than a Linux host over SSH.

| File | Target | Runner image | What it does |
|---|---|---|---|
| `nginx-mtls-endpoint.yml` | Linux VM (SSH) | either | nginx on :8443 with `ssl_verify_client on`, echoing the presented subject DN |
| `ci-fetch-cert.yml` | Linux VM (SSH) | `ansible-winrm` | Fetches both halves from Password Safe, splits the bundle, calls the endpoint |
| `../windows/adcs-pipeline-template.yml` | Windows CA (WinRM) | `ansible-winrm` | Creates the certificate template + enrollment account on an existing AD CS CA |

## The order to run them in

1. **Build the CA.** Certificate Lab → *Build a CA* (GCP CAS), or
   `adcs-pipeline-template.yml` against your existing issuing CA.
2. **`nginx-mtls-endpoint.yml`**, passing `ca_chain_pem`. For a CAS pool the chain is on
   the CA row's *Chain* button; for ADCS it is your issuing CA's chain.
3. **Onboard an identity** — Certificate Lab → *Add identity*, then **Change Password** in
   BeyondInsight. Nothing is issued until that runs, and *Test password* correctly fails
   before it: no bundle exists yet.
4. **`ci-fetch-cert.yml`**, pointed at the endpoint.

## What each one is actually proving

**The endpoint echoes `$ssl_client_s_dn`, and that is the point.** `curl` succeeding proves
TLS completed. The endpoint answering `CN=svc-deploy-pipeline` proves the plugin's
certificate authenticated as the *right identity* — which is the step demonstrations
usually skip, and the only one that proves anything. A certificate that verifies in a
console but fails a handshake has proven nothing.

**The consumer contains no secret.** Both halves come out of Password Safe under an
approved request:

| Half | Where it lives | How it is fetched |
|---|---|---|
| The PKCS#12 **passphrase** | the managed account's credential | `beyondtrust.secrets_safe.managed_account` |
| The **bundle** it opens | a Secrets Safe file secret | `beyondtrust.secrets_safe.secret` |

Retrieving one without the other yields nothing usable, so the Secrets Safe folder's ACL
and the managed account's access policy are **both** live controls — set them to the same
population, or the weaker of the two is your real access boundary.

Credentials are auto-injected into every runner (`PASSWORD_SAFE_API_URL` / `_CLIENT_ID` /
`_CLIENT_SECRET`) when Password Safe is enabled, so there is nothing to configure here —
see [the Ansible integration doc](../../../docs/integrations/ansible.md).

**The `reason` string carries `build_id` into the audit trail**, so the audit record
answers *which build* used the certificate rather than merely that something did. And with
**Change Password After Any Release** on the account policy, the check-in at the end of the
lookup is what makes the next build get a freshly issued certificate.

## Two things worth demonstrating that are easy to skip

**Renewal is transparent.** Run **Change Password** again and re-run `ci-fetch-cert.yml`.
It still works, with a new serial. That is automated rotation being safe to turn on.

**A mid-rotation failure leaves a working identity.** Break the Secrets Safe folder's
permission and run **Change Password**. It fails — *and* `ci-fetch-cert.yml` still succeeds
on the previous certificate. The plugin writes the bundle to Secrets Safe and only then
reports success, so a failed write leaves Password Safe holding the **old** passphrase and
the **old** bundle. Every credential-management demonstration shows the happy path; this is
the one that distinguishes the plugin from a script.

## Boundaries these samples do not cross

- **The server certificate is self-signed and separate from the lab CA**, and
  `ci-fetch-cert.yml` passes `curl -k`. What is being demonstrated is the *client*
  certificate; issuing the server's own from the lab CA would blur the two. The client half
  is fully verified — nginx checks it against the chain and refuses anything else.
- **No revocation checking.** The plugin consults neither CRLs nor OCSP, so a certificate
  revoked at the CA still passes verification until it expires. Short lifetimes are the
  mitigation, and that is a deliberate design position rather than an oversight.
- **No deployment.** The plugin delivers to Password Safe; getting the certificate into an
  nginx config or a Java keystore is the consumer's job, which is exactly what
  `ci-fetch-cert.yml` does with a script.
