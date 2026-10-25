# What the Password Safe public API can and cannot do

> **Audience:** operator · **Profile:** `both` · **Read this when:** you need to know what the dashboard can do through Password Safe's public API, and what it has to leave to the console.

Part of [BeyondTrust Password Safe](../password-safe.md).

Researched against BeyondTrust's own endpoint tables — the [BeyondInsight
APIs](https://docs.beyondtrust.com/bips/v24.3/docs/beyondinsight-api) and [Password Safe
APIs](https://docs.beyondtrust.com/bips/v24.3/docs/password-safe-api) references — while
scoping the POV readiness panel. Recorded here so nobody has to establish it twice, and so
a plan that assumes "we can automate the console" gets corrected before it is written.

**Creatable:**

| Object | Call |
|---|---|
| User groups, their permissions and memberships | `POST UserGroups`, `POST UserGroups/{id}/Permissions`, `.../Users` |
| Workgroups | `POST Workgroups` |
| Assets | `POST Workgroups/{workgroupID}/Assets` |
| Address groups and their addresses | `POST AddressGroups` |
| API registrations | `POST` |
| Users | `POST Users` |
| Functional accounts | `POST FunctionalAccounts` — already used by `pov_functional_account` |
| Directory managed systems | `POST Workgroups/{id}/Directories` |
| Managed accounts | `POST ManagedSystems/{systemID}/ManagedAccounts` |
| Attributes and attribute types | `POST` |
| Smart Rules — **one shape only** | `POST SmartRules/FilterAssetAttribute` |

**Actionable:**

| Action | Call |
|---|---|
| Re-process a Smart Rule | `POST SmartRules/{id}/Process` — used by `pov_ps_config.process` |
| Rotate a managed account's credential | `POST ManagedAccounts/{id}/Credentials/Change` |
| Test an access policy | `POST AccessPolicies/Test` |

**Not creatable — read-only, or no endpoint at all:**

| Object | State |
|---|---|
| Resource zones, resource brokers | **no endpoint of any kind.** Independently confirms why `ps_application_host_id` was never the broker handle — see [`docs/profiles/pov/design/resource-broker.md`](../../profiles/pov/design/resource-broker.md) §6 |
| Discovery credentials | no endpoint |
| Discovery scans | no endpoint; run from the console |
| Directory queries | no endpoint |
| Password policies | read-only |
| Access policies | read, plus the Test above |
| Applications | read-only |
| Smart Rules, generally | read, delete and process; create is the one narrow shape above |

The practical consequence: **the engine of a Password Safe POC — an authenticated discovery
scan and the Smart Rules that act on its results — cannot be built by this dashboard.** What
it can do is verify the work and re-run a rule, which is what `pov_ps_config` does.

Absence from the documentation is not proof of absence from the product, and most of the
table above was not checked against a live tenant. The `_probe` helper in `ps_api_service`
is built for that uncertainty: a `404` is reported as "this Password Safe version does not
serve that endpoint" rather than as a failure, so a tenant that *does* serve one of these
shows up as readable instead of broken.

## Assets and attributes — verified live, 2026-09-23

*This is contributor-level API evidence. For using attributes, see
[Inventory — Password Safe attributes](../../operations/inventory.md#password-safe-attributes).*

Building the `/inventory` attributes column settled four of these against a real tenant.
All four were guesses before, and two of them were wrong:

| Call | Result | Note |
|---|---|---|
| `GET Assets` | **404** | There is **no flat asset collection.** |
| `GET Workgroups/{id}/Assets` | 200 | Assets are **workgroup-scoped on read**, matching the POST. Reading them all means one call per workgroup. |
| `GET AttributeTypes` | 200 | The vocabulary — `Criticality`, `Business Unit`, `Geography`, `Status`, `Operating System`, `Retire Date`, `Workgroup`, `Manufacturer`. |
| `GET Attributes` | **404** | No flat attribute collection either. |
| `GET Assets/{id}/Attributes` | 200 | Per object, which is why the read is capped and only matched objects are fetched. |
| `GET ManagedSystems/{id}/Attributes` | 200 | Exists, and is commonly empty. |
| `POST`/`DELETE ManagedSystems/{id}/Attributes/{attributeID}` | **unverified** | Documented by BeyondTrust; not yet exercised against a tenant from here. See the write note below for what the dashboard does about that. The SPIRE lab's **Prepare probe** is the first caller that will. It also calls `POST AttributeTypes` and `POST AttributeTypes/{id}/Attributes` when the tenant lacks `SpiffeTrustDomain`, and its job log records every call's status, so its first live run settles all three rows. |

**An attribute's shape is easy to read backwards**, and doing so produces chips that look
broken. A row is:

```json
{"AttributeID": 10000, "AttributeTypeID": 10000, "ShortName": "Online",
 "LongName": "Online", "ValueInt": 0, "ChildAttributes": []}
```

`ShortName` is the **value**; the *type* is the category it was chosen from. So
`AttributeTypeID 10000` + `ShortName "Online"` renders as **`Status = Online`** — not
`Online = ""`. The type is also the half `SmartRules/FilterAssetAttribute` keys on, so
losing it loses the point. `ps_attribute_catalog.to_chips` takes the `AttributeTypes` map
for exactly this.

**Writing an attribute — verified live the same day.** `POST` and `DELETE` on
`Assets/{assetID}/Attributes/{attributeID}` both work; a `DELETE` of an attribute the
asset does not carry answers **404**, which the dashboard treats as success (the caller
asked for it gone and it is gone, so a retry after a partial apply does not report errors
for the targets that already succeeded).

**Managed systems take attributes too, and that is the path that matters here.**
`ManagedSystems/{managedSystemID}/Attributes/{attributeID}` is the same POST/DELETE shape
in a different collection. It is the one most of `/inventory` needs: everything this
dashboard onboards lands as a managed system, so while the write path was asset-only, most
matched rows could have their attributes *read* and never changed.

One asymmetry follows from the table above: the managed-system **read** is verified and
the **write** is not. So the 404-is-success rule cannot simply be reused — an appliance
that does not serve the endpoint at all answers 404 to exactly the same DELETE. Before
accepting a 404 as "already gone", `ps_api_service._set_object_attribute` issues one
`GET {collection}/{id}/Attributes`: a 200 proves the endpoint family exists and the
attribute is genuinely absent, anything else is reported as a failure. Reporting a removal
that never happened is the worse of the two errors by a wide margin.

A resource matched to **both** an asset and a managed system gets the change written to
both records. They are separate rows in Password Safe and can disagree; the inventory
page's chips flatten them into one, so writing to only half means a removal leaves the chip
on screen and reads as a failed write.

An attribute is **assigned, not typed**: a type owns a fixed set of values, each with its
own `AttributeID`, so `GET AttributeTypes/{id}/Attributes` is the picker and there is no
free text anywhere in the write path. Some types are `IsReadOnly: true` — `Criticality`
is, in the tenant checked — and the dashboard refuses those by name rather than letting
Password Safe refuse them once per target in a bulk apply.

**An asset's address is not guaranteed usable.** In the tenant checked, 33 of 35 assets
carried a routable `IPAddress`; the other two carried an IPv6 **link-local** (`fe80::…`),
which cannot identify a host. Separately, every managed system this dashboard onboards
through a cloud-native plugin carries the `127.0.0.1` placeholder with a packed locator in
`DnsName` — so it has **no usable address at all**, and is matched by the
`ps_managed_system_id` recorded on its deploy job instead. See `ps_attribute_catalog`.
