# Giving a customer read access to their POV

> **Audience:** operator · **Profile:** `both` · **Read this when:** a customer stakeholder needs to see their own POV and nothing else.

Part of [Permissions](../permissions.md).

This is the case the model previously could not express, and there are two ways to do it.
They are for different people.

## A named person who should also be a real user

Give them a normal login with:

- **Permissions:** `POV` → `read` and `use`. Nothing else.
- **POV access:** their environment, chosen in the picker under the grid.

They can then open the POV page, see exactly one POV — theirs — and check off use cases
against it. Any other POV answers *"No such POV environment"*, the same as an id that does
not exist, because confirming that somebody else's POV exists is itself a leak.

Leave the POV access picker **empty** and they see every POV. That is the default, and it
is what every pre-existing user has.

The picker also decides what a POV Presenter **owns**, along with the POVs they create
themselves, and a presenter sees only what they own. An empty picker therefore shows a
presenter only the POVs they created. Add a colleague's POV to their picker to let them
co-present it. When a presenter whose picker names some POVs creates a new one, it is
added to their picker automatically.

To let someone see and manage **every** POV, give them the **POV Manager** role, or the
general `pov` levels.

`use` is the important half. With `read` alone they can look but not tick, and a use-case
checklist nobody can tick is a screenshot. With `write` they could provision and destroy.

## An anonymous prospect who should not have a dashboard login at all

Use a **POV accessor** instead — see [Customer access to a POV](../../profiles/pov/customer-access.md).
An accessor is an ephemeral credential bound to one environment, able to reach five
endpoints and nothing else in the product, and deleted when the POV is reaped. It is not a
restricted user; it is a different kind of principal.

Rule of thumb: if you would put their name in an org chart, give them a user with a POV
grant. If you are emailing a link to an evaluation, mint an accessor.
