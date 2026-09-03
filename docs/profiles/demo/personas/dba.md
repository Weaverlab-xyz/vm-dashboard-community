# DBA / Data Platform

Owns the databases, and therefore the data. Their accounts are the ones an attacker actually
wants: a database administrator does not need to move laterally, because they are already at
the destination.

Their access problem is that **database privilege is coarse and permanent**. Grants are made
at the schema or instance level, they are made once, and the database itself has no notion of
an expiry — so a grant issued for a migration in March is still there in November.

## Why they care

Two things are usually true. The application's database credential has not been rotated
because nobody is certain what would break, and the human accounts have privileges nobody can
justify because removing a grant is riskier than leaving it. Both are consequences of the same
missing capability: access that ends by itself.

## The four layers, for this role

| layer | what it does here |
|---|---|
| **Provisioning** | Managed Postgres, MySQL, SQL Server or Oracle, with no public listener. |
| **PRA** | A tunnel, so a normal SQL client reaches a private database without a bastion. |
| **Password Safe** | Owns the admin credential and rotates it — including the one the application uses. |
| **Entitle** | The grant with an expiry the database cannot express itself. |

## Use cases

### Provision a managed database, under management from birth

Stand up Postgres, MySQL or SQL Server and have its admin account onboarded and rotated as
part of the same job — no window in which the database exists with an unmanaged password.

**Guide:** [Databases](../../../databases.md)

### Request, approve, grant, expire

An analyst asks for read access to one schema, an approver says yes, the grant appears in the
database and then removes itself. The full loop, and the one thing no database can do on its
own.

**Guide:** [Entitle resource registration](../../../design/entitle-resource-registration.md)

### Rotate a database admin credential with nothing breaking

Rotate the account an application depends on, and show the application keep working. Do this
live: it is the objection that stops most rotation projects, and a demo answers it better than
an argument.

**Guide:** [Password Safe](../../../integrations/password-safe.md)

### Reach a private database with no public endpoint

Connect an ordinary SQL client to a database that has no internet-facing listener, through a
brokered tunnel — so "private" stops meaning "inconvenient".

**Guide:** [Databases](../../../databases.md)

### Rotate the token a service uses to reach the database

The non-human identity nobody rotates because nobody is sure what depends on it, rotated on a
schedule with the consumer picking up the new value.

**Guide:** [Kubernetes ServiceAccount token rotation](../../../design/k8s-sa-token-rotation.md)

## What to enable

**Cloud databases**, **Password Safe** and **Entitle**. Add **Privileged Remote Access** for
the private-endpoint card, and **Kubernetes** for the service-account token card.

**This focus needs a demo instance** for its own cards: cloud databases are demo-owned, so on
a [POV instance](../../pov/README.md) they are masked. The cards say so rather than offering a
link that would 404.

## Talking to this buyer

Be precise about scope, because they will be. A grant is against a specific catalog or schema,
not "the database", and getting that wrong in a demo costs credibility immediately. Note also
that "database name" means two different things in this product — the catalog a grant applies
to, and the database an administrative session connects to — and they are not always the same
value; see [Databases](../../../databases.md) before improvising.
