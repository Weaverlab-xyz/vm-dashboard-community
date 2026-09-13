# Dynamic cloud credential samples (`cloud/`)

The consumer half of the Workload Lab's **Cloud** tab: a program that reaches AWS or Azure
with a credential minted on demand by BeyondTrust **Workload Credentials**, and then proves
the credential dies on its own.

Feature reference: [docs/workload-cloud.md](../../../docs/workload-cloud.md).

| File | Target | Runner image | What it does |
|---|---|---|---|
| `ci-run-with-dynamic-creds.yml` | localhost (reaches out) | either | Uses a minted credential, then asserts the same call is refused once the lease expires |

## What makes this different from the other cloud plays

Every other play here takes the cloud credentials the dashboard injects and supplies
nothing. This one **blanks them** in an `environment:` block and uses only what was minted.
That is not tidiness: left set, the CLI falls back to them, every task passes, and the expiry
assertion never fires — the play would report a successful demonstration of a mechanism it
never touched.

## It does not mint

Deliberately. Two reasons, and the second is the one that matters:

1. **Issuance is metered.** A play that minted on every run would bill per run — and would
   always be testing a fresh credential, which makes the expiry assertion meaningless.
2. **The consumer retrieving with its OWN token is the point.** That is what puts *the
   consumer* in Workload Credentials' audit log. A play that minted on the operator's behalf
   would record the operator, which is exactly the property this mechanism exists to remove.

So mint once — Workload Lab → Cloud → **Issue**, or your own call — write the result, and
point `credential_file` at it. The expected shape is what `generate` returns verbatim:

```json
{"values": {"access_key_id": "...", "secret_access_key": "...", "session_token": "..."},
 "lease_id": "...", "expires_at": "2026-09-13T01:23:45Z"}
```

## The order to run them in

1. **Register an identity.** Workload Lab → Cloud → *Register a workload identity*, naming a
   Workload Credentials dynamic secret. Nothing is minted.
2. **Issue**, and write the result to a file.
3. **Run the play** with `wait_for_expiry=false` for a quick check that the credential works.
4. **Run it again** with `wait_for_expiry=true`. It sleeps to the lease's own expiry and
   asserts the same call is then refused. That wait is real, not simulated — a play that
   faked the clock would prove it can print a failure message, not that the credential died.

## Revocation is asymmetric

Azure leases can be released early. **AWS leases cannot** — STS will not withdraw a
credential it has already signed, so `revoke` is refused and the expiry is the only control.
That makes a short TTL matter *more* on AWS, not less. The play says which case it is in.
