"""Move a dashboard's Settings configuration from one instance to another.

The problem this exists to solve is that the obvious approach does not work and
does not say so. Every ``app_config`` value is Fernet-encrypted with a key
derived from ``JWT_SECRET_KEY`` (``config_service._fernet``), and two instances
have different JWT keys — so a ``pg_dump``/``pg_restore`` of that table lands
ciphertext the target cannot read. It fails *silently*: ``config_service._decrypt``
catches ``InvalidToken`` and returns the raw string, so the app serves the
ciphertext as if it were the config value, with no exception and no log line.

The migration therefore moves **plaintext** and lets the target re-encrypt with
its own key, through the endpoint that already exists for exactly this shape of
write (``POST /api/setup/import``, the same one the sandbox onboarders use).

Four verbs::

    export        read a source instance   → bundle.json (mode 0600)
    export-local  same, but from inside the container (recovers masked values)
    diff          bundle + target          → ADD / CHANGE / SAME    (read-only)
    import        bundle                   → target                 (--apply)

``diff`` is the default because it is read-only; writing requires ``--apply``.

Scope is everything reachable from the Settings panel: the whole ``app_config``
store plus ``notification_endpoints`` rows, which are edited in
Settings → Notifications. See :mod:`.classify` for what is deliberately held
back, and ``docs/config-migration.md`` for the operator walkthrough.
"""
