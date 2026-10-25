# No static secret on the agent host

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you want to show the agent host holds no stored credential, and why the ps hop is worth it.

Part of [Agent Demo Cell](../agent-demo-cell.md).

The worker has four token sources, and three of them store nothing.

| `--token-source` | What sits on the host | Honest name for it |
|---|---|---|
| `spiffe` | **nothing** | the SVID is the credential: exchanged at the dashboard's own token endpoint. No cloud, no Workload Credentials — only the SPIRE agent on the host |
| `file` (default) | a 0600 file holding the PAT or client pair | a static secret, smaller than an env var but still a static secret |
| `wlc` | **nothing** | the platform vouches for the machine; Workload Credentials serves the PAT from its own store |
| `ps` | **nothing** | Workload Credentials hands over the Password Safe API client pair, and the worker *requests* the credential from the vault |

`spiffe` is covered above. In `wlc` and `ps`, the worker starts the same way:

1. it asks its platform for **its own identity token**;
2. it presents that to **Workload Credentials** in place of a PAT, with
   `X-BT-Service-Name` naming which registered Workload Identity it satisfies.

`wlc` stops there and reads the PAT out of WC. `ps` goes one step further, and that step
is the argument:

3. WC returns the **Password Safe API client id and secret**;
4. the worker signs in to Password Safe with that pair and **requests** the credential —
   `POST Auth/Connect/Token` → `SignAppIn` → `POST Requests` → `GET Credentials/{id}` →
   `PUT Requests/{id}/Checkin`, the same sequence `services/ps_api_service` uses.

Everything the worker is *configured* with — site id, service name, audience, base URL,
the two WC secret **names**, the account id — is **non-secret**.
`services/workload_credentials_service` puts its half plainly: *"Two auth modes, and the
second one stores nothing."*

## Why `ps` is worth the extra hop

Password Safe authenticates an application with a client-credentials pair, so that pair is
a standing credential and always was. The question is **where it lives** — and in `ps` mode
it lives in Workload Credentials, not on this box. WC becomes a *bootstrap for the vault*
rather than a second vault beside it, which is what lets the worker reach anything Password
Safe governs rather than only what was copied into WC.

Everything Password Safe already governs then governs this too. Say it in the room while
the credential is in flight:

- the retrieval is a **recorded request**, with a duration and a reason;
- it can be made to **require approval** — the worker simply waits, and says so;
- the credential behind it **rotates** on its own schedule, and the worker re-requests.

A PAT in a file has none of those properties and never will.

**This is the suite answering its own question.** Password Safe (old) holds and governs the
secret; Workload Credentials (new) brokers the way in against an identity the platform
vouches for; the workload holds nothing. Neither product does that alone, and the seam
between them is the thing worth showing — a competitor with one half cannot.

It also removes the asterisk this cell was carrying. "A non-human principal that holds no
standing credential" is the argument, and a PAT in a file was that argument with a caveat.

## The identity is not Azure-only

Every major cloud federates non-human identities over OIDC. `--identity-platform` picks
which issuer is asked, and one of them is not a cloud at all:

| Platform | Where the token comes from |
|---|---|
| `azure` | IMDS, or `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` where the runtime injects them |
| `gcp` | the metadata server's `instance/service-accounts/default/identity` endpoint |
| `aws` | the projected token at `AWS_WEB_IDENTITY_TOKEN_FILE` (IRSA) or `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` (EKS Pod Identity) |
| `spire` | a JWT-SVID from the SPIRE agent already on this host |
| `file` | any other projected token on disk, such as a Kubernetes ServiceAccount token |
| `auto` | whichever of the above this host declares — it refuses rather than guessing |

Two of these rows are worth saying out loud:

- **AWS has no OIDC endpoint on IMDS.** A plain EC2 instance gets SigV4 credentials and a
  signed identity document, not an OIDC JWT. The projected file is how an AWS non-human
  identity holds one, and it comes from a cluster. On plain EC2, use `spire`.
- **`spire` needs no cloud.** The Workload Lab already publishes the trust domain as an
  OIDC issuer, so a bare-metal host federates on the same mechanism a cloud VM does. That
  is the row that covers the hardware in a rack — the shape the network and OT cells have.

`auto` detects by marker and **refuses rather than guessing** when the host declares
nothing. No file tells a bare Azure VM apart from a bare GCE one, and `169.254.169.254` is
both their metadata addresses; probing would mean a request that has to time out to say no.
Name the platform — it is one word and never wrong.

> **Unproven, so `file` is still the default.** No Workload Credentials tenant, no
> registered Workload Identity and no federation trust has been stood up for this. The
> dashboard's *own* WC client is Azure-only besides (`AUTH_MODE_ENTRA` calls IMDS and
> nothing else) — the worker has all five platforms, the app has one. The `ps` source
> additionally needs an API-enabled managed account with the Requestor role and an access
> policy that auto-releases, the usual out-of-band prerequisites in
> [password-safe.md](../../../beyondtrust/password-safe.md). The worker names its token
> source on every line it logs, so which mode is in play is never in doubt.

Re-running the install play with `agent_token_source: wlc` or `ps` **removes** any token a
previous `file` install left behind — "nothing is stored on this host" must not be
contradicted by a file in `/etc`.
