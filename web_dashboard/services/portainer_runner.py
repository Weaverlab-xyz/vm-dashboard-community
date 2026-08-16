"""Auto-inject the dashboard's Portainer connection into an Ansible runner as
``PORTAINER_*`` environment variables, so an in-playbook API call works with no
per-run setup.

Mirrors ``password_safe_runner`` exactly: the sample playbooks
(``examples/playbooks/portainer/``) are localhost plays that hit the Portainer REST
API from the Ansible *controller* — i.e. inside the runner container — and read
``PORTAINER_URL`` / ``PORTAINER_PAT`` / ``PORTAINER_VERIFY_SSL`` from the process
environment. Those map onto the connection the Containers page already uses
(``portainer_url`` / ``portainer_pat`` / ``portainer_verify_ssl``), whether the
operator configured it by hand or a ``portainer_node_deploy`` job wrote it.

``PORTAINER_VERIFY_SSL`` matters: a dashboard-deployed node serves a self-signed
certificate on :9443, so the deploy turns verification off — playbooks must honour
that rather than hard-coding ``validate_certs: true`` and failing against a node the
dashboard itself created.

The PAT rides the SAME per-run connection-credential channel each runner backend
already uses for the SSH private key / DB password / Password Safe client secret —
ECS ``runTask`` override env, Cloud Run plain env, ACI ``secure_value``, a ``0600``
file locally — NOT the cloud secret store. Callers MUST add the PAT
(``runner_env()[SECRET_KEY]``) to their output scrub set.

``build_runner_env`` is pure (no imports) so it unit-tests without ``config_service``;
``runner_env`` is the thin wired entry point.
"""
import logging

logger = logging.getLogger(__name__)

# The env var carrying the API token — the one sensitive value of the three.
# Callers append its value to their scrub set so it can never leak into job output.
SECRET_KEY = "PORTAINER_PAT"


def build_runner_env(*, enabled: bool, url: str, pat: str,
                     verify_ssl: bool = True) -> dict:
    """Pure core of :func:`runner_env`. Returns the ``PORTAINER_*`` env dict, or ``{}``
    when disabled or either connection value is blank (callers treat ``{}`` as "do not
    inject"). A non-empty result always carries all three keys."""
    if not enabled:
        return {}
    url = (url or "").strip().rstrip("/")
    pat = (pat or "").strip()
    if not (url and pat):
        return {}
    return {
        "PORTAINER_URL": url,
        SECRET_KEY: pat,
        # Stringified for the env channel; playbooks read it with `| bool`, which
        # accepts "1"/"0" as well as "true"/"false".
        "PORTAINER_VERIFY_SSL": "1" if verify_ssl else "0",
    }


def runner_env() -> dict:
    """Return the ``PORTAINER_*`` env for the runner, or ``{}`` when unavailable.

    ``{}`` means "do not inject" — Portainer is disabled, or no server is configured
    (neither typed into Settings nor written by a managed-node deploy).
    """
    from . import config_service as cs
    return build_runner_env(
        enabled=cs.get_bool("portainer_enabled", True),
        # config_service.get resolves vault refs (bt_safe://, aws_sm://, …) transparently,
        # so a PAT stored as a reference arrives here already dereferenced.
        url=cs.get("portainer_url"),
        pat=cs.get("portainer_pat"),
        verify_ssl=cs.get_bool("portainer_verify_ssl", True),
    )
