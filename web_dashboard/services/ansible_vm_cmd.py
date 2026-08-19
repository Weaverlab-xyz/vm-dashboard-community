"""Pure argv builder for a **VM** Ansible run — the one that SSHes or WinRMs *to* a host.

The counterpart of :mod:`ansible_localhost_cmd`, which builds the ``hosts: localhost`` play
used for Kubernetes clusters and databases. Two runners execute this shape today and they
must agree exactly:

* the dashboard's local sibling container (``ansible_local_service.run_playbook``), which
  bind-mounts a per-run temp directory at ``/ansible``;
* a **remote agent**'s one-shot sibling, which has no bind mounts at all and receives the
  same files through the Docker archive API under ``/opt/job``.

Hence ``job_dir``: the two differ in *where the files are*, and in nothing else. What must
not differ is the flag set and — the part that actually bites — the **ordering**, because
``--extra-vars @file`` after an inline ``--extra-vars`` is what makes a resolved secret win
over an operator-supplied value of the same name. Two copies of this list would eventually
disagree about that, and the symptom would be a run that silently used the wrong value. The
same reasoning that gave ``agent_hypervisor_meta.PAGE_OPS`` one table instead of three.

The agent cannot import from this package — it is a standalone file with three
dependencies — so it carries a mirrored copy, and a test asserts the two produce identical
argv for identical inputs. That is the established pattern here; ``canonical_request`` and
``seal_aad`` are mirrored the same way.

Pure and stdlib-only, so the argv can be asserted without Docker or an app.
"""
import json
import shlex

# Host-key checking cannot be continuous with anything: every run is a fresh container with
# no known_hosts, so a check would refuse every first connection rather than detect a change.
SSH_COMMON_ARGS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


def build_vm_argv(*, job_dir: str, inventory: str, limit: str = "",
                  private_key: bool = False, extra_vars=None,
                  secret_vars_file: bool = False, user: str = "") -> list:
    """The ``ansible-playbook`` argv for a VM run.

    ``inventory`` is passed through verbatim because the two callers legitimately supply
    different things: a path to an inventory file, or Ansible's bare ``'<ip>,'`` one-host
    form. ``private_key`` and ``secret_vars_file`` are booleans rather than paths — the paths
    are derived from ``job_dir`` here, so a caller cannot put the key somewhere the other
    runner would not look for it.
    """
    argv = [
        "ansible-playbook",
        "-i", inventory,
        f"{job_dir}/playbook.yml",
        "--ssh-common-args", SSH_COMMON_ARGS,
    ]
    if limit:
        argv += ["--limit", limit]
    if user:
        argv += ["-u", user]
    if private_key:
        argv += ["--private-key", f"{job_dir}/id_rsa"]
    if extra_vars:
        argv += ["--extra-vars", json.dumps(extra_vars)]
    if secret_vars_file:
        # @file keeps secret values out of the process args and the logs; it is read inside
        # the container and dies with it. AFTER the inline extra-vars, so a resolved secret
        # wins on a name conflict — this ordering is the whole reason this module exists.
        argv += ["--extra-vars", f"@{job_dir}/secret_vars.json"]
    return argv


def quote(argv: list) -> str:
    """``argv`` as one shell-safe string, for a runner that must go through ``sh -c``."""
    return " ".join(shlex.quote(a) for a in argv)
