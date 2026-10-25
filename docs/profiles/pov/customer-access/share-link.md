# The customer-facing share link

> **Audience:** customer · **Profile:** `pov` · **Read this when:** you want to share a POV's results with the customer through a password-protected link that expires.

Part of [What the customer sees](../customer-access.md).

Everything above is something an SE touches. This is the one artifact a **customer** opens:
a single URL onto the POV's desktops, published by the lab platform. On Skytap it is a
*publish set*; on the POV page it is the **Share** column.

Three properties are not configurable, because each one exists to stop a specific way a
POV link goes wrong.

## It always has a password, and the password is generated

Skytap treats the publish set's password as optional. This does not, and there is no field
to type one into. A blank password box is how an anonymous door into a lab holding a
Gateway, a Resource Broker and a set of Entitle integrations gets left open — and an SE's
usual password is barely better.

So the dashboard generates a 20-character password, hands it to the platform, and stores it
Fernet-encrypted alongside the POV's other secrets (`pov/<env-id>/share_password`, the same
shape as the Gateway deploy key). It is shown once when you share, and re-readable
afterwards from the **password** button — that read is audited, because "who has this
link's password" is the first question asked when a URL turns up somewhere it should not
have.

The alphabet excludes `I l 1 O 0`. These get read down a phone line more often than anyone
plans for.

## It always expires

There is no "never" option. The failure mode of a POV share is not that someone guesses the
URL — it is that the link *outlives everyone's attention*: the evaluation ends, the
environment sits on `suspend_on_idle`, and the URL keeps working for months.

The expiry is chosen in this order:

1. what you asked for, if you typed a number of days (1 to 90, refused outside that rather
   than clamped);
2. the POV's own auto-delete date, if it has one — so the link cannot outlive the
   environment it points at;
3. otherwise 14 days.

Skytap enforces the expiry itself, so an expired link is already dead. The page shows it as
`expired` rather than clearing the row, because during an evaluation the fact that a link
*was* shared is worth keeping.

The expiry crosses to Skytap as **two** fields, `expiration_date` and its companion
`expiration_date_tz`. The date carries no zone of its own and Skytap refuses the pair when
only the date is sent — with `{"field":"expiration_date_tz","message":"Expiration date tz
is invalid"}`, an error naming a field nothing set. The dashboard sends the date in UTC and
the zone as `UTC`, together, and refuses an unparseable expiry before the call rather than
letting Skytap answer for it. If an account rejects that zone name, the error says which
one went out and where to change it.

## Re-sharing replaces; revoking is its own button

**Re-share** revokes the current publish set before creating a new one, and mints a new
password. Leaving the old one would give the POV two live URLs and one stored id — the
second could never be revoked from here again. It also means re-sharing is a real remedy
when a link went to the wrong person.

**Revoke** kills the link without touching the POV. The platform call happens first and its
failure is *not* swallowed: clearing the row while the URL still worked would leave a live
share nobody could find, let alone kill.

Both are also reachable from the API:

```
POST   /api/pov/managed/{id}/share          {"days": 7}   -> the link and its password
DELETE /api/pov/managed/{id}/share                        -> revoke
POST   /api/pov/managed/{id}/share/reveal                 -> the password again, audited
```

The URL is shown with a **copy** button rather than as a hyperlink. It is a customer-facing
address, and an accidental middle-click from this page is an SE opening a session as the
customer.

## A platform that cannot do this

The **Share** column reads `not supported` when the platform's `share_link` capability is
false, and the API refuses with a message pointing at PRA instead — the jump items the
wire-up created already reach these VMs, which is the better answer for a customer who
needs *audited* access rather than a URL. Skytap supports it; the column exists so the
second adapter degrades explicitly instead of 500ing from inside a job.

## Teardown

Destroy revokes the share **before** anything else, ahead even of the PRA jump items. It is
the only artifact somebody outside the account can be holding, so the window where it still
works is the one worth making shortest.

It never blocks the destroy. Deleting the environment removes its publish sets server-side
anyway, so a failed revoke costs a note in the job log rather than a stranded POV — and the
row is cleared regardless, so a destroyed POV is never left advertising a link.
