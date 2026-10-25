# Building a CA

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are building the lab's private certificate authority, root or subordinate.

Part of [The Certificate Lab](../certificate-lab.md).

**Workload Lab → Certificates → Build a CA.** Pick a cloud; one Terraform module per cloud does the
rest. On **GCP** that is a CAS pool on the DevOps tier, a self-signed root CA, and the
enrollment service account the plugin authenticates as. On **AWS** it is a Private CA, its
self-signed root certificate, and the IAM user the plugin authenticates as.

The cloud selector only appears when more than one module is built — the list comes from
the modules that actually exist, so a cloud can never be offered without one behind it.

## What the root may sign is decided here, once

The build form asks what the root permits beneath it, and **it cannot be changed after the
root is created**.

| Choice | The root's path length | Serves |
|---|---|---|
| End-entity certificates only *(default)* | 0 | the Certificate platform |
| Can also sign a subordinate CA | 1 | both platforms |

This is **the one prerequisite that stops a subordinate-CA build dead.** A root created to
issue leaves has a path length of zero and will refuse to sign a subordinate at all — and
it is the *certificate authority* that refuses, with a policy error naming neither this
choice nor the flag behind it. On GCP that flag is `--max-chain-length=1` at root creation
(`max_issuer_path_length` in the module); on AWS it also decides whether the enrollment
IAM policy permits a `SubordinateCACertificate` template at all.

So the dashboard records it on the CA row, shows it in the **Signs** column, and refuses a
subordinate against a leaf-only root **at the click** rather than letting it fail at the CA
hours later. Every CA built before this choice existed reads as leaf-only, which is what
those roots genuinely are.

Leaf-only stays the default: a root that permits a CA beneath it is a wider grant than a
lab needs by default, and widening it for every existing-style build would be a silent
change to what the button produces. A subordinate-capable root still issues end-entity
certificates exactly as before, so there is no downside to picking it when you intend to
demonstrate both — and demonstrating both from one CA is the clearest way to show why the
split exists, since the *same* address on the other platform issues the other kind of
thing.

## The build also creates the functional account

This is the step that used to be manual, and it had to move because **the enrollment
credential exists for exactly one moment**. Both clouds' APIs return it once — a GCP
service account key, an AWS secret access key — so it is live in the apply's outputs and
in no other place a person can reach. Recovering it by hand meant minting a *second* key.

So the build composes the account and writes it to Password Safe, in whichever of the two
shapes above is configured: on the OAuth path username `<enrollment-principal>` and
password `<enrollment-secret>` whole, with the client id and secret in the account's API
key and secret fields; on the packed one username `<enrollment-principal>:<run-as-user>`
and password `<enrollment-secret>:<api-key>`. On GCP the secret is the `private_key`
**field** out of the key JSON, PEM armour and all — never the whole file, which is the most
common way to get this wrong by hand.

It is also the only path that fits. `ps-cli` caps a functional-account password at 1,000
characters; a GCP `private_key` PEM is about 1,700. The REST API the dashboard uses
accepts 3,216.

**A failure here does not fail the build.** The CA is real and billing the moment the
apply returns, and a rebuild is not a free retry — CAS never hands a deleted pool id back.
So the row stays *available* with the error underneath it, and **Wire up Password Safe**
retries against the credential still recorded in the CA's own terraform state.

Teardown deletes the account, but only one the dashboard minted: in `reference` mode the
account is an operator's own and may be shared by every CA on the platform.

## Every id here is single-use

**CAS never gives a deleted resource id back.** Once a pool is destroyed,
`projects/<p>/locations/<l>/caPools/<id>` stays reserved permanently, and an apply that
asks for it again fails:

```
Error: Error waiting to create CaPool: Error code 3, message: Previously used CaPool ids
may not be reused. A `CaPool` for `projects/…/caPools/demo-pipeline-pool` has previously
been deleted
```

In a feature built around destroying the CA, that makes a pool id derived from the CA's
*name* alone usable exactly once — the first rebuild of `demo-pipeline`, and every rebuild
after it, is refused for good in that project and location. So the dashboard generates
`<name>-pool-<6 hex>`: the name stays the readable part of `pool=` on the address, and the
suffix is what makes a rebuild possible at all. The generated id is cut to fit CAS's
63-character cap on **both** the pool id and the `<pool>-root` CA id under it, and a free
text name is slugged (`Demo Pipeline (EU)` → `demo-pipeline-eu-…`). Those seven extra
characters come out of
[the 255-character address budget](../certificates.md#the-255-character-budget-is-real).

An id supplied through the API (`pool_id` on `POST /api/cert-lab`; the build form does not
offer the field) is honoured exactly as typed, because it goes on to name the pool in every
address built against this CA — but it is validated at the click, and typing an id that has
existed here before hits the same permanent wall.

**The enrollment identity is per CA for the same reason,** one namespace up: a GCP service
account id is unique per *project* and an IAM user name per *account*, so the modules'
shared `certauth-plugin` default only ever fits one lab. The dashboard passes a per-row
`certauth-<8 hex>` instead. Without it the second CA in a project fails with
`alreadyExists` **after the pool exists**, and so does the retry after a build that got as
far as the identity and then died.

A failed build is rolled back — `terraform destroy` over the same state — before the row
goes `failed`, so a partial apply does not leave a billing pool or a live enrollment key
behind. If the rollback itself fails, the row says `MANUAL CLEANUP REQUIRED` under the
original error and **Destroy** retries the teardown.

Teardown is the part worth understanding, because **CAS resists deletion by default in three
separate ways**, and each one leaves a pool that goes on billing:

| Guard | Without it |
|---|---|
| `deletion_protection = false` | `terraform destroy` fails outright |
| `ignore_active_certificates_on_deletion = true` | a CA that has issued anything refuses to delete — which is the normal case, not the edge case |
| `skip_grace_period = true` | the deleted CA sits in a 30-day soft-delete state, and its pool cannot be deleted while it holds one |

The module sets all three. **Prove destroy before create**: apply the module, destroy it
immediately, and confirm in the console that the pool and CA are *gone* rather than pending
deletion.

AWS resists teardown differently and less: a deleted Private CA is restorable for
`permanent_deletion_time_in_days`, which **defaults to 30**. The module asks for 7, the
floor the API accepts, because this lab exists to get rid of the thing. The IAM user carries
`force_destroy` so a key or inline policy added in the console cannot wedge the destroy.
Prove destroy before create there too — confirm the CA is gone rather than merely disabled,
and that the IAM user went with it.

The CA's **chain PEM** is on the row's *Chain* button. It is a public document by
construction — it is what every client has to trust — and it is what the mTLS endpoint
playbook takes as `ca_chain_pem`.

## The timer

Every CA built here is stamped with the default auto-delete TTL at provision time, in the
provision's own transaction. `NULL` means *never*, never "inherit the default", so nothing
that already exists is retroactively armed. Extending or pinning a timer is the ordinary
**Inventory → Extend** path.

**On AWS the timer is not optional.** At ~$400/month standing against ~$20/month for a CAS
pool, a Private CA nobody remembers is a different order of mistake — so a build is
*refused* on an instance where the reaper would stamp nothing at all, which is the case when
`resource_expiry_enabled` is off or `resource_expiry_default_hours` is `0`. GCP is
deliberately not held to this: at a twentieth of the cost the same trade does not hold, and
tightening it would change behaviour somebody already relies on.

A stamped timer is necessary and not sufficient — the reaper only *deletes* when
`resource_expiry_enforce` is on and `resource_expiry_dry_run` is off. That half is reported
on the build form's **missing** list rather than refused, because arming enforcement is
something an operator may be part-way through.

A timer that runs out enqueues **exactly the job the Destroy button creates** — there is one
teardown path, exercised both ways. A *failed* teardown deliberately does **not** re-arm the
timer: a half-destroyed pool needs a human, and re-arming would retry a failing destroy on a
loop, silently.
