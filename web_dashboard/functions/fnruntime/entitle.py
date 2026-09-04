"""The Entitle Remote Adapter response shapes that are not what they look like.

Two routes here are validated against a schema the published OpenAPI example
contradicts. Both fail the same way — the sync looks healthy, one operation is
rejected, and the reason is only in Entitle's audit log — so both shapes are built
here, once, rather than hand-rolled per adapter.


``get_all_permissions`` — the fields are MAPS, not lists
--------------------------------------------------------

``get_all_permissions`` (and its per-asset twin) is validated by Entitle against
``Get All Permission Response``, and **both permission fields are maps keyed by
asset id**, not lists:

    actors_permissions   map[asset_id] -> [{actor_id, role_code, direct_member}]
    assets_permissions   map[asset_id] -> [{asset_id, role_code}]     (optional)

The published example in the OpenAPI definition renders them as arrays — that is
where every adapter here got it wrong — but the validator does not, and the field
descriptions say the truth: "Maps each asset ID to all permissions that users have
directly to it", and for the second "all permissions other assets (groups, etc)
have/grant to it".

An adapter that answers with a list fails only this one route. ``get_assets`` and
``get_actors`` sync green, the integration looks configured, and the single symptom
is "Failed to fetch the permissions" in the Entitle audit log — with Entitle holding
whatever it last believed about who has access, which is what it reconciles against.

``assets_permissions`` is the asset-to-asset half: which OTHER assets (a group, a
nested role) confer access to this one. None of the targets fronted here nest assets
inside one another, so ``{}`` is the honest answer rather than a placeholder — and
notably it is NOT the catalogue of role codes an asset offers, which is what
``get_assets`` already publishes in ``role_options``.


``create_actor`` — the credentials are NESTED, and the actor block is closed
----------------------------------------------------------------------------

``create_actor``'s ``data`` is validated against ``Provisioned Actor Data``, which
has exactly two properties and admits no others::

    {"actor": {identifier, name, type, email, last_user},   # required, CLOSED
     "login_info": {...}}                                   # required, free-form

Both objects are ``additionalProperties: False`` at the top level, and ``actor`` is
closed too with ``email`` REQUIRED. So the flat dict every adapter here returned —
identifier and name beside username, password, host, port — is rejected wholesale:

    Invalid response. Structure of "Provisioned Actor Data" is invalid.
    Additional properties are not allowed ('database', 'host', 'identifier',
    'name', 'password', 'port', 'role_code', 'statements_executed', 'type',
    'username' were unexpected)

Note *which* keys it lists: ``identifier``, ``name`` and ``type`` are unexpected too,
because at the top level nothing is expected but the two containers. Reading the
error as "stop sending host/password" and trimming fields is the trap — the fields
were right, the nesting was missing.

This is the worst route to fail. In Ephemeral Accounts mode ``create_actor`` IS the
grant: the account and the role are already created on the database by the time the
response is validated, so a rejected response leaves a live account nobody is told
about and Entitle believes the request failed. ``delete_actor`` is only driven from
what Entitle thinks it provisioned, which means the account then outlives the grant.

``login_info`` is the free-form half and is where everything the requester needs
goes — username, password, host, port, database — along with whatever the adapter
wants to report about the operation. Entitle presents it to the requester as-is.

Stdlib only, like the rest of fnruntime.
"""

# The closed ``actor`` object, in schema order. Anything else an adapter knows about
# the account belongs in ``login_info``; putting it here fails validation.
ACTOR_FIELDS = ("identifier", "name", "type", "email", "last_user")


def permissions_data(actors_by_asset=None, assets_by_asset=None) -> dict:
    """The ``data`` object for ``get_all_permissions`` / ``get_asset_permissions``.

    Pass every asset the adapter serves, including the ones nobody holds — an asset
    present with an empty list says "no one has this", an asset the map omits says
    nothing at all, and only the first is an answer Entitle can reconcile against.
    """
    return {
        "actors_permissions": {str(asset): list(rows)
                               for asset, rows in (actors_by_asset or {}).items()},
        "assets_permissions": {str(asset): list(rows)
                               for asset, rows in (assets_by_asset or {}).items()},
    }


def actor_data(identifier, actor_type, *, email="", name=None, last_user=None,
               login_info=None) -> dict:
    """The ``data`` object for ``create_actor``.

    ``identifier`` is the account the adapter minted and is what Entitle passes back
    to ``give_access``, ``revoke_access`` and ``delete_actor`` — so it must be the
    real account name, not the requester's.

    ``email`` is required by the schema, and the honest value is the identity the
    request arrived with: it is the requester's address whenever Entitle sent one,
    and the identifier it did send otherwise. It is NOT defaulted to the minted
    username, which would attribute the account to itself and lose the only link
    back to the person who asked. An adapter with no identity at all sends ``""``,
    which satisfies the schema without inventing an owner.

    Everything else — credentials, endpoint, role, what the adapter executed — goes
    in ``login_info``, which Entitle hands to the requester verbatim.
    """
    actor = {"identifier": str(identifier),
             "name": str(identifier if name is None else name),
             "type": str(actor_type),
             "email": str(email or "")}
    if last_user:
        actor["last_user"] = str(last_user)
    return {"actor": actor, "login_info": dict(login_info or {})}
