# The dashboard's own SPIFFE identity

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the dashboard to authenticate to a cloud with a short-lived token from its own SPIRE server instead of a stored key, or you are checking why a token file is missing or refused.

Part of [Remote Agents](../remote-agents.md). The same SPIRE server that attests agents can give the dashboard an identity of its own, which a cloud can trust instead of a stored key.

**Preview, and only the first half.** This page covers what is built: the dashboard keeps a
short-lived JWT-SVID per audience in a file, and publishes the discovery document and keys
a cloud fetches to verify it. Pointing each cloud's SDK at those files, and the Azure code
path that reads one, are the next slice. Until then a file is used only by something you
point at it yourself. The design, including what is not built, is
[The dashboard as a SPIFFE workload](../design/dashboard-workload-identity.md).

## What it does

With **Settings → Integrations → Remote Agents → Give the dashboard its own SPIFFE
identity** ticked, the app keeps one token file per audience you tick:

| Audience | File | Token `aud` |
|---|---|---|
| AWS | `/run/spiffe-tokens/aws.jwt` | `sts.amazonaws.com` |
| Azure | `/run/spiffe-tokens/azure.jwt` | `api://AzureADTokenExchange` |
| GCP | `/run/spiffe-tokens/gcp.jwt` | the workload identity provider's full resource name, as entered |
| Workload Credentials | `/run/spiffe-tokens/wlc.jwt` | the identity audience under Settings → Workload Credentials |

Every token is for `spiffe://<trust domain>/dashboard`, lives fifteen minutes, and is
re-minted at half that. The files sit on a memory-only volume: written by the app, readable
by the worker, never on the host disk. Unticking an audience deletes its file at once, so
anything still pointed at it fails now rather than when it expires.

**Nothing changes which credential a cloud call uses.** A stored key still wins, exactly as
before. Clear one only after its cloud has accepted the token.

## Setting it up

1. **Run the SPIRE server overlay** with an issuer:

   ```bash
   SPIRE_TRUST_DOMAIN=dashboard.example.com \
   SPIRE_JWT_ISSUER=https://agents.example.com/spiffe \
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

   ```powershell
   $env:SPIRE_TRUST_DOMAIN = 'dashboard.example.com'
   $env:SPIRE_JWT_ISSUER   = 'https://agents.example.com/spiffe'
   docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d
   ```

2. **Tick the setting** and the audiences you want. The **Issuer** field shows what this
   dashboard publishes. By default that is the pinned agent audience plus `/spiffe`,
   because the agent gateway is the part of an install built to be reachable from
   outside, and its Caddyfile publishes `/spiffe/*`. `SPIRE_JWT_ISSUER` must equal it
   character for character.
3. **Check the files.** The panel lists each one with its expiry or the reason it is
   missing.
4. **Register the issuer with the cloud.** Use the issuer URL, and pin the subject to
   `spiffe://<trust domain>/dashboard` exactly.

## Pin the subject, never the trust domain

Remote agents and service-account workloads share the dashboard's trust domain. A trust
policy that accepts any `spiffe://<trust domain>/…` hands each of them the dashboard's cloud
access. Every cloud lets you condition on the exact `sub`; use it.

## Where it has to be reachable from

The issuer serves two public documents, both 404 until the setting is on:

- `<issuer>/.well-known/openid-configuration`
- `<issuer>/keys`, the JWT signing keys, read live from the SPIRE server and cached five
  minutes, falling back to the stored daily copy if the server is down.

AWS and Azure fetch them anonymously from the internet, over HTTPS with a publicly trusted
certificate. A plain-`http` issuer is refused before anything is minted. GCP can instead be
given the keys by upload, which works for a private dashboard but has to be repeated at
every key rotation (weekly by default).

These documents carry public keys and nothing else. Do not put them behind SSO: every cloud
that trusts the issuer would stop accepting the dashboard's tokens at once.

## What it does not protect against

The app administers the SPIRE server, so anyone who compromises the app can mint tokens
for the dashboard's identity, and use them while they are present. A stored key, by
contrast, can be copied and kept. That is the improvement: a credential that needs continued
presence, instead of one that works offline for ever. It is not "the app holds nothing".

## Troubleshooting

| Symptom | Cause |
|---|---|
| *the SPIRE server signs with issuer '(none)'* | `SPIRE_JWT_ISSUER` is blank on the server. Set it to the issuer the panel shows and restart the SPIRE container. No file is written until they match. |
| *the SPIRE server signs with issuer 'X', but this dashboard publishes discovery as 'Y'* | The two disagree. Change either one so they match exactly, trailing slash included. |
| *is not https* | The issuer resolved to `http://`. Set the issuer explicitly, or fix the pinned agent audience. |
| *container 'vmdash-spire-server' is not running* | The SPIRE overlay is not up, or its container name differs from **SPIRE server container**. The previous token file is kept until it expires. |
| *Workload Credentials is ticked but its identity audience is blank* | Set the audience under Settings → Workload Credentials first. |
| `/spiffe/keys` answers 503 | Neither the live server nor a stored bundle has a JWT key yet. Start the SPIRE server; the stored copy is written on the first sync. |
