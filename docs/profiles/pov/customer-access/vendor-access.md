# Vendor access to a POV

> **Audience:** customer · **Profile:** `pov` · **Read this when:** a third party needs into the lab, with its own login, TOTP and an expiry.

Part of [What the customer sees](../customer-access.md).

**POV page → a POV's name → Access.**

The accessor below is a door into *this dashboard*. This is the door into the **lab** — a
PRA login that opens this POV's jump items and nothing else, in the customer's own
appliance, on their own clock.

It closes a real gap. A POV wired into PRA already has a jump item per VM, through the
Gateway installed inside the environment. Until this, only users who already existed in
that appliance could launch one, so the items were correct and unreachable by the prospect.
PRA's own answer to that is **Vendor Onboarding**, and this drives it.

## Four objects, all named after the POV

| In PRA | Named | What it does |
|---|---|---|
| Asset Group (a Jump Group before PRA 26.1) | `pov-<name>` | Holds this POV's jump items. Created by **Wire up**. |
| Group Policy | `pov-<name>-vendor-access` | Grants sessions on that Asset Group and nothing else |
| Vendor Group | `pov-<name>-vendors` | Holds the third-party logins; its policy is the one above |
| Vendor users | `povvnd_<name>_<random>` | One per person you let in |

The Asset Group is the part worth understanding, because it is why this could not simply be
bolted on. **A PRA Group Policy grants access by Asset Group**, and a POV's jump items used
to go into the Asset Group named on the *tenant* — one group shared by every POV on that
appliance. A vendor attached to it would reach all of them. So a POV now gets an Asset Group
of its own, and the vendor's policy names that one group.

## A POV wired before this will say so

If this POV's jump items are still in the tenant's appliance-wide group, the card refuses
and says why rather than offering a button that over-grants. Underneath the refusal it
offers **Move to its own Asset Group**, which is the fix: it destroys this POV's jump items
and rebuilds them in `pov-<name>`, then the vendor group can be created.

Two things to know before pressing it:

* **Nobody can launch a session on this POV while the job runs.** A jump item names its
  Asset Group when it is created and there is no update for that, so moving one means
  destroying it and building a new one. A Windows guest's Vault account is rebuilt with its
  jump item, because the two live in one Terraform workspace.
* **Nothing else moves.** This POV's managed systems, Entitle integrations, Gateway,
  accessors and share link are all left exactly as they are.

If an item cannot be destroyed, the job stops before creating the group and says so —
items split across two Asset Groups would let a vendor reach part of the POV with nothing on
the screen saying which part. Press **Wire up** to rebuild whatever did come out (back into
the tenant's group, where the rest still are), sort the failure out in PRA, and move again.

Pressing **Wire up** on its own will *not* move the items, and says so in the job log:
filling in a POV's gaps should not quietly tear down items that are working. And you can
decline the whole thing — the dashboard login below and the share link both still work for
a POV on the shared group.

## Giving somebody a login

Press **Create the vendor group**, then fill in an email and press **New vendor login**.

**PRA has no invite call.** Nothing is emailed: the notification settings on a Vendor Group
email your PRA administrators, not the vendor. So the dashboard creates the account and
shows you a username and password **once**, which you hand over — and the account is set to
require a password change at first sign-in, so the value you sent stops working as soon as
they have used it. The email you type is a label, and what PRA shows on the account.

The email is **required** — PRA's vendor user has four mandatory fields and the address is
one of them, alongside a display name (left blank, the dashboard uses the address). If the
appliance refuses a login, the red banner now carries PRA's own field-level reason after
*Validation Failed* — "the email address has already been taken", a password-policy
complaint, and so on — which is the thing to act on.

The password the dashboard generates satisfies PRA's **default** policy by construction:
twenty characters with at least one upper, one lower, one digit and one symbol, and none of
`Il1O0` so it survives being read down a phone line. If your appliance's policy is
*stricter* than the default — a longer minimum, a banned character — the create will still
fail, and the banner will carry the appliance's own wording under `password:`. That is a
site setting; loosen it, or say so and the generator can be widened.

They sign in to **PRA itself**, not to this dashboard, and see this POV's jump items only.

## Requiring TOTP

Tick **Require TOTP for vendor users** to set the POV's vendor Group Policy to
`two_factor_type: require_totp`. Each vendor then has to enrol an authenticator app at their
next PRA sign-in, so tell them to have one ready when you hand over the login.

The box works before or after the vendor group exists. Before, it is recorded and applied
when you press **Create the vendor group**. After, it changes the live policy in place.
It never re-creates the group, because PRA deletes a vendor group's users along with it and
every login you had handed out would stop working. Unticking it sets the policy back to
PRA's default, `optional`.

If you have built a self-registration portal in `/login`, paste its URL on the PRA tenant
(Settings → Integrations → tenants) and the card will show it with a Copy button. The
dashboard cannot create one: **Portal Settings and the Email Domain Allow List are not in
the Configuration API at all**, so there is nothing to automate. The Network address allow
list *is* in the API, and the card offers it.

## It expires, and never outlives the POV

Seven days by default, always shortened to the POV's own expiry — ask for thirty on a POV
with five left and you get five. PRA enforces that on its side, per Vendor Group, and
expired accounts are deleted a week later so you can still see who had access during a
post-mortem.

Belt and braces, as with accessors: destroying a POV removes the vendor group **first**,
ahead of everything else, because of the three doors a POV holds open this is the only one
whose holder is neither in your account nor in this dashboard. A sweep on the POV reconcile
pass catches the rest — groups whose clock ran out, and groups whose POV is already gone.

## What it will not do

It will not **adopt** anything. An Asset Group, Group Policy or Vendor Group already in the
appliance that this dashboard did not create is a refusal naming it, never a reuse — a
policy scoped to a group somebody else filled would hand a third party whatever is already
in it. Deleting follows the same rule: only names carrying the `pov-` prefix, and only user
accounts carrying `povvnd_`, so a vendor your customer added by hand is safe from this
screen.
