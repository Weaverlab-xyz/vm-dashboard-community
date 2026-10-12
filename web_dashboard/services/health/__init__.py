"""Health of the infrastructure the dashboard runs on and has built.

``registry.COLLECTORS`` lists the areas, ``collectors/`` holds one module per area, and
``runner`` runs them from the jobs worker and stores the results in ``health_checks``.
The Health page (/health) reads only what was stored.

Infrastructure only, as on the Directory health page: no collector reads accounts,
groups, roles or privilege. That is BeyondTrust Identity Security Insights' data.
"""
