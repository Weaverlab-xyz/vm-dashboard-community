# Audit Log

> **Audience:** operator · **Profile:** `both` · **Read this when:** you need to show who did what, or to satisfy yourself that the record has not been edited.

The dashboard records security-relevant actions — agent enrolment and revocation,
hypervisor connection changes, image deletions, cloud destroys, policy denials — to an
append-only, hash-chained table. This page is what you can do with it.

## The chain, and what it actually promises

Every entry carries a `seq` (globally monotonic), a `prev_hash` (the previous entry's
hash) and an `entry_hash` — a SHA-256 over that entry's own fields **plus** its
predecessor's hash. Recomputing the chain from the start diverges at the first entry
whose content or link changed, so an edit, a deletion or a reordering is detectable.

What it promises is **detection, not prevention**. Anyone with write access to the
database can still change a row; what they cannot do is change it without the next
verification saying so.

Two consequences worth knowing before you plan around it:

- **There is no retention policy, and there cannot be one.** Deleting any entry breaks
  the chain by construction. The table only grows. Budget for that, and export if you
  need to move history somewhere cheaper.
- **The verification is a full walk, deliberately.** Altering an old entry breaks that
  entry's hash but not the links between the entries after it, so a check that resumed
  from a checkpoint would step straight over the tampering it exists to find. The walk
  streams rather than loading the table, which is what keeps a full check affordable.

### `ip_address` and chain versions

The chain covers `seq`, `timestamp`, `username`, `action`, `target_vm`, `details`,
`prev_hash` and — since chain **v2** — `ip_address`.

The address column existed before v2 and nothing populated it, so nothing was
unprotected in practice. But an address recorded under v1 would have been alterable
without breaking verification, which is the worst kind of integrity guarantee: one that
reads as covering a field it does not. v2 closes that.

**Upgrading is automatic, and it refuses to launder evidence.** On first start after the
upgrade the dashboard verifies the existing chain under v1 and only then re-hashes it
under v2. If the old chain does **not** verify, it is left exactly as it is and the
startup log says so — because re-hashing a tampered table is precisely what someone who
had edited a row would want, and the rewritten chain would be consistent with the
altered content. A refusal looks like this:

```
Audit chain: NOT migrated — the existing chain does not verify (first broken seq 412).
Left untouched; check GET /api/audit/verify.
```

If you see that, the table was already broken before the upgrade. Investigate it; do not
work around it.

## Where the address comes from

`log_audit` is called from ~75 places, most of them services with no HTTP request in
scope, so the address travels in a request-scoped context variable rather than through
every signature. It is:

- the value `ProxyHeadersMiddleware` resolved, so `X-Forwarded-For` is honoured **only**
  from a peer listed in `trusted_proxy_hosts` (loopback by default). Set that to your
  proxy's literal IP when you put one in front, or the column records the proxy rather
  than the client;
- **blank for anything the job worker did.** A job has no client; its actor is the user
  recorded on the job row.

> If you set `trusted_proxy_hosts` to `*`, any client that can reach the socket can
> declare its own address and this column will faithfully record the lie. The chain
> protects the value from later edits; it cannot vouch for where it came from.

## Reading it

**Settings → Audit**, or `/audit`. Admin only, as is every endpoint below.

Filter by actor, action (a prefix — actions are namespaced, so `agent.` matches
`agent.create` and `agent.revoke`), target substring, date range, or a free-text search
across action, target and details. **Verify chain** runs the integrity check on demand
and states the result either way; an intact chain is the thing an auditor wants said out
loud, not assumed.

| Endpoint | What it does |
|---|---|
| `GET /api/audit` | A page of entries, newest first, with the filters above |
| `GET /api/audit/actions` | The distinct action names present, so the filter is a list rather than a guess |
| `GET /api/audit/verify` | Recompute the chain: `{ok, count, first_broken_seq}` |
| `GET /api/audit/export?fmt=csv\|json` | Stream the matching entries, oldest first |

The export carries `prev_hash` and `entry_hash` alongside the content, and reads
oldest-first, so whoever receives it can recompute the chain themselves rather than
taking the table on trust. It is capped at 100,000 rows per request — narrow the filters
or the date range to page through more.

## The scheduled check

Nothing verified the chain on a schedule before: it ran when an administrator remembered
to ask, which makes a tamper-evident log evidence nobody ever checks. The periodic
condition scan (`notify_scanner`, hourly by default) now runs the same verification and
raises **`audit.chain_broken`** — severity `critical` — when it fails.

Enable [notifications](notifications.md) to receive it. It is in the default event set.

Like the other scanned conditions it dedupes per day, so a standing break re-notifies
once a day rather than once ever. Unlike them it also buckets on the offending `seq`, so
a *new* break notifies immediately instead of being swallowed by the message about the
old one.

## What this is not

- **Not a per-user activity view.** Entries are *about* people; a non-admin view of who
  did what is a different feature with a different blast radius, so this ships admin-only.
- **Not an external archive.** The chain is verifiable locally. Continuous export to a
  WORM bucket or a SIEM — so the trail is durable evidence off this box — is on the
  [SaaS roadmap](saas-roadmap.md), and the export endpoint here is the manual half of it.
- **Not the aggregated pane.** The roadmap's centralised audit pane pulls together
  Password Safe checkouts, signed build manifests and workflow history. Those feeds
  mostly do not exist yet. This is the one feed that does, made legible.
