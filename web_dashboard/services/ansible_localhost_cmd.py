"""Pure command-builder for the *localhost* Ansible run used by Kubernetes-cluster
and cloud-database Config-Management targets.

Unlike the VM runners (which SSH *to* a host: ``-i '<ip>,' -u <user> --private-key``),
these targets run a ``hosts: localhost, connection: local`` play that reaches *out*
to the cluster API (kubeconfig) or the DB endpoint (login vars). All four runner
backends — the three cloud task fns (ECS / ACI / Cloud Run) and the local sibling
container used for on-prem clusters — decode the same env vars and run the same
command; this module is the single, side-effect-free source of that command so it
can be unit-tested without launching anything.

Env contract (set by the runner task fns):
  PLAYBOOK_B64   — base64 playbook, always present (rides the task-def env)
  CONN_VARS_B64  — base64 JSON of DB connection extra-vars (database targets)
  KUBECONFIG_B64 — base64 token-prepped kubeconfig    (kubernetes targets)
CONN_VARS_B64 / KUBECONFIG_B64 ride the *ephemeral* task override env (they carry
the DB password / the kubeconfig bearer token). On the local runner all three ride a
0600 ``--env-file`` instead — see ``build_local_docker_argv``.

There is deliberately **no ``set -x``**: tracing would echo the base64 blobs — and
thus the credential — into the runner logs.
"""

# 0600 so the decoded credential files aren't world-readable inside the container.
_CONN_VARS_PATH = "/tmp/conn_vars.json"
_KUBECONFIG_PATH = "/tmp/kubeconfig"
_PLAYBOOK_PATH = "/tmp/playbook.yml"


def build_localhost_command(*, with_conn_vars: bool = False,
                            with_kubeconfig: bool = False) -> str:
    """Return the ``sh -c`` command string for a localhost Ansible run.

    The caller knows at build time whether the run is a database run
    (``with_conn_vars``) or a kubernetes run (``with_kubeconfig``), so the decode
    steps are emitted unconditionally for the relevant material — no runtime shell
    ``[ -n … ]`` guard that could trip ``set -e``."""
    lines = [
        "set -e",
        f'printf %s "$PLAYBOOK_B64" | base64 -d > {_PLAYBOOK_PATH}',
    ]
    if with_kubeconfig:
        lines += [
            f'printf %s "$KUBECONFIG_B64" | base64 -d > {_KUBECONFIG_PATH}',
            f"chmod 600 {_KUBECONFIG_PATH}",
            f"export K8S_AUTH_KUBECONFIG={_KUBECONFIG_PATH} KUBECONFIG={_KUBECONFIG_PATH}",
        ]
    if with_conn_vars:
        lines += [
            f'printf %s "$CONN_VARS_B64" | base64 -d > {_CONN_VARS_PATH}',
            f"chmod 600 {_CONN_VARS_PATH}",
        ]
    playbook_cmd = f"ansible-playbook -i 'localhost,' -c local {_PLAYBOOK_PATH}"
    if with_conn_vars:
        playbook_cmd += f" -e @{_CONN_VARS_PATH}"
    lines.append(playbook_cmd)
    return "; ".join(lines)


def build_local_docker_argv(*, image: str, env_file_path: str,
                            with_conn_vars: bool = False,
                            with_kubeconfig: bool = False) -> list:
    """``docker run`` argv for the **local** runner — the on-prem Kubernetes case.

    A cluster registered with ``cloud="local"`` lives on the corporate LAN, where a
    transient in-cloud task has no route. The dashboard host runs it instead, in a
    sibling container off the same ``ansible_cloud_image``, executing the identical
    command as the three cloud runners (above) so there is no second command string
    to drift.

    Every value — ``PLAYBOOK_B64``, ``CONN_VARS_B64``, ``KUBECONFIG_B64`` and the
    injected ``PASSWORD_SAFE_*`` / ``PORTAINER_*`` env — is passed via ``--env-file``
    and never ``-e KEY=value``: docker CLI arguments land in the host's process list,
    where ``ps`` would expose the kubeconfig's client key to any local user. Only the
    file *path* appears in this argv; the caller writes it 0600 into a per-run temp
    directory that is deleted when the container exits.

    Pure and side-effect-free (like ``build_localhost_command``) so the argv can be
    asserted in a unit test without Docker present.
    """
    return [
        "docker", "run", "--rm",
        "--env-file", env_file_path,
        image, "sh", "-c",
        build_localhost_command(with_conn_vars=with_conn_vars,
                                with_kubeconfig=with_kubeconfig),
    ]
