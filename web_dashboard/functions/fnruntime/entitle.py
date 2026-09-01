"""The one Entitle Remote Adapter response shape that is not what it looks like.

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

Stdlib only, like the rest of fnruntime.
"""


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
