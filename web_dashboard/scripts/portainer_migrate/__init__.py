"""Read a Portainer CE instance into a reviewable JSON bundle.

Companion to :mod:`web_dashboard.scripts.config_migrate`, and deliberately shaped
the same way: read a source over its REST API, write a plain JSON artifact the
operator can open and edit, then apply it somewhere else. Here the "somewhere
else" is the dashboard's ``portainer_import`` job rather than a second CLI
command, because the target is a managed node the dashboard already holds
credentials for.

Why a CLI at all, when the dashboard could read the backup itself? Two reasons,
both structural:

  * A Portainer ``.tar.gz`` backup is a BoltDB database (``portainer.db``) plus
    certs and keys. Nothing in this repo can read BoltDB, and the archive is only
    restorable by Portainer itself — into a *pristine* instance with an empty data
    volume, which a managed node deliberately never is (it initializes its admin
    at container start to dodge the init-timeout lockout). So the archive has to
    be opened by a throwaway Portainer, and the natural place to run one is the
    machine that already has Docker and already has the backup.
  * A cloud-hosted dashboard cannot reach a workstation's Portainer at all.

The result is that this tool runs where the data is, and hands the dashboard
something small, inspectable and free of live credentials.

**What does not travel.** Portainer's API does not return user passwords or
registry passwords, and this tool strips any credential-shaped field it sees
anyway. Environment (endpoint) definitions are recorded for reference but are not
importable: they point at a local Docker socket or a LAN address that a cloud
node has no route to. Re-establishing those means an Edge agent, not an import.
"""
