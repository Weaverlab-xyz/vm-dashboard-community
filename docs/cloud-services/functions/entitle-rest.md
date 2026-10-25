# Wiring an Entitle REST integration

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you are pointing an Entitle custom REST integration at a function the dashboard deployed.

Part of [Cloud Functions](../functions.md).

Both adapters implement the [Remote Adapter contract](https://docs.beyondtrust.com/entitle/docs/open-api-definition).
**The verb is the path**, and Entitle lets you configure each path separately, so
point them at the routes below (or leave the defaults, which match):

```
get_assets_path            /get_assets
get_actors_path            /get_actors
get_all_permissions_path   /get_all_permissions
create_actor_path          /create_actor      (required for ephemeral accounts)
delete_actor_path          /delete_actor      (required for ephemeral accounts)
give_access_path           /give_access
revoke_access_path         /revoke_access
```

Two responses have shapes the definition's own examples do not show, and both are
built by `fnruntime.entitle` — use the helpers rather than a literal.

`get_all_permissions`: both permission fields are **maps keyed by asset id**, not
arrays (`actors_permissions` is `map[asset_id] -> [{actor_id, role_code,
direct_member}]`, and `assets_permissions` is the asset-to-asset half, which is `{}`
for every target here). An array is rejected with *Structure of "Get All Permission
Response" is invalid* on that route alone — assets and actors keep syncing green, so
the only symptom is one red line in Entitle's audit log.
`fnruntime.entitle.permissions_data` builds it.

`create_actor`: its `data` is **nested**, with exactly two properties and no others
allowed — `actor` (`identifier`, `name`, `type`, `email`, optional `last_user`, and
nothing else) and `login_info`, which is free-form and is where the credentials, the
endpoint and anything else the requester needs go. A flat object with the credentials
beside the identifier is rejected wholesale, listing `identifier`, `name` and `type`
among the unexpected properties — the fields were right, the nesting was missing. In
Ephemeral mode this is the worst route to fail, because the account has already been
created and granted by the time the response is validated: Entitle records the
request as failed, the requester is told nothing, and `delete_actor` is only ever
driven from what Entitle believes it provisioned, so the account outlives its grant.
`fnruntime.entitle.actor_data` builds it.

Authenticate with the function's shared secret as a custom header — Entitle's
`headers` config takes `"Authorization": "Bearer <TOKEN>"` verbatim, which is
exactly what the handler verifies. Get the value from **Functions → Endpoint**.

Start with `entitle_webhook_echo`: it answers every route correctly and grants
nothing, so you can confirm the network path, the front door, the shared secret and
every `*_path` mapping before a real database is involved. A 404 from it lists the
routes it actually serves, which is almost always the fix.

> **Doing this for the first time?** Follow
> [the first-deploy runbook](../../runbooks/cloud-functions-first-deploy.md), which
> orders the clouds GCP → AWS → Azure for a reason and says what fails how at each
> stage.
