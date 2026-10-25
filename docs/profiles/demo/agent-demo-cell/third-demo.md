# Demo 3: the credential nobody can take away

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are presenting just-in-time access whose approval is the only moment anyone has a say.

Part of [Agent Demo Cell](../agent-demo-cell.md).

The three episodes are worth running in order, because the arc teaches what no single one
does:

| Episode | The credential | How you take it away |
|---|---|---|
| the loop | its MCP **PAT** | **revoke it** — the worker stops mid-poll, visibly |
| cluster access | a Workload Lab **token** | **gated at retrieval** — a person decides; once released it lives out its TTL |
| this one | a **certificate** | **neither** |

`docs/preview/workload-lab/certificates.md` states the third position rather than hiding it:

> **No revocation checking.** The plugin consults neither CRLs nor OCSP. Short lifetimes
> are the mitigation, and that is a deliberate design position.

With a `certificates` link in place, `mcp_agent.py --cert-episode`:

```
[agent] spiffe://weaverlab.test/agent/mcp-reader · requesting the certificate identity behind svc-deploy-pipeline
[agent] holding nothing: the Password Safe client pair came from Workload Credentials against this machine's own identity
[agent] spiffe://weaverlab.test/agent/mcp-reader · WAITING for approval (20s) — this agent cannot authorise its own access
[agent] spiffe://weaverlab.test/agent/mcp-reader · passphrase released; downloading the bundle from Secrets Safe
[agent] spiffe://weaverlab.test/agent/mcp-reader · identity proved — the endpoint answered 200 and echoed svc-deploy-pipeline
[agent] spiffe://weaverlab.test/agent/mcp-reader · the request was checked back in
```

**Two halves, and neither is usable alone.** That is the tab's design and the agent
honours it: the PKCS#12 **passphrase** is the managed account's credential, fetched
through the same recorded request as the cluster token; the **bundle** it opens is a
Secrets Safe file secret. Retrieving one without the other yields nothing.

Secrets Safe is part of Password Safe, so **both halves come down one session**: the
session that released the passphrase reaches the bundle unchanged. No second sign-in, no
second credential, and nothing on this host that was not there a moment ago.

**Not through `ps-cli`, though it is what the dashboard uses.** It cannot carry these
bytes. [password-safe.md](../../../beyondtrust/password-safe.md) establishes it and
`secrets_backend_service` refuses on it: the endpoint returns `application/octet-stream`
faithfully, but every route ps-cli offers decodes the body to text first, so a PEM bundle
survives and **a `.pfx` is corrupted rather than refused**. That is the worst of the three
outcomes — the corruption is silent at the transport and surfaces three steps later, as
what looks like the wrong bundle in Secrets Safe. So the worker calls
`GET Secrets-Safe/Secrets/{id}/file/download` and keeps the bytes, which is what that page
prescribes for exactly this case.

Because the bundle arrives as a **file**, the episode opens its directory before it
fetches anything: the bundle lands there, `openssl` opens it there, and the whole directory
goes at the end. One guarded place, named in the section below rather than left to be
discovered.

## The approval is the only moment anybody gets a say

Both halves are governed, and the **passphrase** goes through the same approval-gated
request as the cluster token — so a person decides before this agent gets an identity at
all.

That matters more here than anywhere else in the cell, and the reason is the next section:
a certificate cannot be revoked out from under the agent. With the PAT you can change your
mind afterwards. With the cluster token you can at least wait out a TTL you chose. Here,
**the approval is the last decision anybody makes about this identity** until it expires.

> **The worker refuses an ungated release.** If Password Safe hands the passphrase over on
> the first ask, no person was consulted — and an episode that printed its usual success
> line would be describing something that did not happen. So it stops, with exit code
> **5**, and says which access policy to change.
>
> This worker cannot *make* Password Safe require approval; that is the managed account's
> access policy, set in BeyondInsight with auto-release off. What it can do is refuse to
> pretend. Pass `--no-require-approval` to run it as an ungated fetch — and then say so
> when you present it.
>
> The same check now runs on the [cluster episode](second-demo.md),
> where the page's claim that the agent "cannot authorise its own access" was equally
> untrue on an auto-releasing policy.

## Then do the thing that does not work

**Disable the managed account — which revokes the certificate on a backend that can — and
run the episode again. It still works.**

Nothing on this path checks a CRL or an OCSP responder. The agent stops when the
certificate **expires**, not when somebody takes it away.

Say that out loud. It is the opposite of the PAT demo and it is the reason the PAT demo
matters: a room that has just watched a revoke stop an agent dead will understand exactly
what it means that this one does not.

## What this demo does not prove

- **The bundle and the private key touch disk**, in one place. There is no version of
  this that keeps them out of the filesystem: the bundle is a file secret and ps-cli
  downloads it, `openssl` needs a file to open a PKCS#12, and Python's `ssl` needs file
  paths for a client certificate. So the episode bounds it instead — **one** `0700`
  temporary directory holds the bundle, the certificate and the key; the key is written
  `0600`, the passphrase reaches `openssl` through `PFXPASS` rather than the command line,
  and the directory goes on the failure path too. It is the one unavoidable exception to
  "nothing is stored on this host", and it is better said than found.
- **It needs BeyondInsight 26.1.0.878 or newer.** Below that, file secrets downloaded
  through the API came back larger than the original, so this episode would retrieve a
  corrupt bundle however careful it is — and the DER check does not catch it, because a
  too-large bundle still starts `0x30`. It is already a
  [Certificate Lab prerequisite](../../../preview/workload-lab/certificate-lab.md#password-safe);
  it is named again here because this episode depends on it silently.
- **Revocation on six of nine backends only.** EST, step-ca and `selfsigned` have no
  revocation operation at all, so on those the certificate stays valid until it expires
  whatever you do. Check which backend the CA uses before promising a revoke.
- **Nothing here has been run against a real CA**, a real Password Safe tenant or a real
  mTLS endpoint. The probe is exercised against a generated CA and a local server in
  `tests/test_agentcell_cert_episode.py`, and that is all it is.
