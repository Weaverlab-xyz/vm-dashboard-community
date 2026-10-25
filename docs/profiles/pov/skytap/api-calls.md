# The Skytap API calls the dashboard makes

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want to know exactly which Skytap v1 API calls the dashboard makes, and why each one.

Part of [Skytap](../skytap.md).

Most of what the dashboard sends is the v2 API. **Which calls have a v2 form is decided
per verb, not per path**, and the paths that look most settled are the ones that are not:
`/v2/configurations/{id}` serves `GET` and `PUT` and **not** `DELETE`, and
`/v2/templates/{id}` serves only `GET`. Every one of these was learned from a live account
answering `404 {"error":"Not Found"}` — never from the documentation, which reads as though
both objects support the full set.

| Operation | Call | Why not v2 |
|---|---|---|
| Create an environment from a template | `POST /configurations.json` — `template_id`, optionally `project_id` and `name` | no `POST` on the v2 collection |
| Create a template from an environment | `POST /templates.json` — `configuration_id`, `name` | no `POST` on the v2 collection |
| Copy VMs into an existing environment | `PUT /configurations/{id}.json` — `template_id`, `vm_ids[]` | the v2 `PUT` answers **200 and does nothing** — see below |
| **Delete an environment** | `DELETE /configurations/{id}.json` | `DELETE /v2/configurations/{id}` 404s for **every** id, including a running one |
| **Delete a template** | `DELETE /templates/{id}.json` | `/v2/templates/{id}` serves only `GET` |
| Set a template's description | `PUT /templates/{id}.json` — `description` | same; the v2 `PUT` 404s |

Everything **nested** under those paths is v2 and works: `…/vms/{id}/user_data.json`,
`…/publish_sets`, `…/vms/{id}/interfaces/{id}/services`, `…/vms/{id}/credentials`. So is
`PUT /v2/configurations/{id}` for a name, a runstate or an idle timer.

A 404 on one of these means **the endpoint, not the id** — and that is the trap worth
remembering, because the message reads exactly like "the id you sent does not exist". The
first live template build was spent inspecting a base template id that was perfectly good.

## A 404 on a DELETE is not proof the thing is gone

The delete rows above cost six environments. `delete_environment` sent the v2 path, Skytap
404ed it, and the adapter read that 404 as "somebody already deleted it" — so every POV
destroy reported a clean success, marked the row `destroyed`, wrote *"The infrastructure is
gone"* on the archive page and deleted the VM rows that were the only record of what had
been inside. The environments went on running, and on billing, invisible to every sweep:
`pov_reconcile` only asks about rows that are **not** destroyed.

The deletes are still idempotent — a teardown that fails because the thing is already gone
leaves a row nobody can ever clean up — but the evidence for "already gone" is now a
**direct read of the object**, never the status code of the delete
(`skytap_service._confirm_gone`). A 404 on the delete with a readable object raises, which
puts the POV row in `failed` with a re-runnable Destroy rather than in `destroyed` with an
orphan. It is the same refusal to guess that makes `pov_reconcile` confirm a missing
environment with a direct read before flagging it, and it is worth applying to any delete
added here later.

A read that fails with something else — a 423, a 500, a network blip — re-raises rather
than being guessed either way.

## The merge, which is quieter still

The VM copy in the table above is the one with no failure signal at all. It is v1 and a
**PUT**, at a path the v2 API also serves.

Skytap's v2 reference says plainly that "VMs are created indirectly, either by creating an
environment from a template or by merging a template into an existing environment", and
documents no endpoint for the second half. The v1 merge is that endpoint. It also accepts
`merge_configuration` in place of `template_id`, to merge from another environment; the
dashboard does not use that form.

**`PUT /v2/configurations/{id}` exists, is what `update_environment` uses for the name and
the idle timer, and answers a `template_id` with `200` and the environment unchanged.** No
error, no VMs, nothing to investigate — strictly worse than the 404 the creates give,
because a 404 at least says something happened. `skytap_service.add_vms` is the only caller
of the v1 path and `tests/test_pov_add_vms.py` pins it against the code, not the docstring.

Two behaviours worth knowing:

* **`vm_ids` is optional in the API and required by this dashboard.** Omitted, the whole
  template is merged — against a live POV that silently doubles the environment.
* **`409` has one documented cause**: a running IBM Power VM among the ones being copied,
  which cannot be suspended for the copy. Nothing in the word "conflict" says so, so
  `add_vms` names it.
* The copies arrive **stopped** even when the rest of the environment is running, which is
  why the `pov_env_add_vms` job powers the environment on afterwards unless told not to.

### If you add VMs in Skytap's own UI instead

The dashboard will not notice. `pov_reconcile` is deliberately **one collection read per
pass** — that is the property that lets it run over every POV cheaply — so it refreshes a
runstate, a rate-limit flag and an idle timer, and never a VM list. Adding a per-environment
VM read to it would turn one call into one per POV.

`pov_environment_vms` is refreshed by three things: a provision, a power action, and the
add-VMs job. **Rebuild control node** (or **Install control node**) does it too, via `run_env_broker`, which is the shortest way to pick
up a guest added outside the dashboard — and it is what you would press for a new guest
anyway, since a Config-Management grant is written at enrolment.
