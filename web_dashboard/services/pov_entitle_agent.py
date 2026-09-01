"""Installing an Entitle agent inside a POV environment, on a single-node k3s.

The prerequisite ``docs/pov-instance.md`` used to say the dashboard would not satisfy.
Entitle's SSH connector reaches a **private** target through an agent running inside that
network, and a POV's VMs are private by construction — so every POV integration is a
private one, and until now every POV needed an agent somebody else had deployed.

Four decisions, each of which removes something rather than adding it:

**No new agent verb.** Delivery is ``agent_ansible``, the same channel
``pov_resource_broker`` uses, so this costs no image rebuild. What it costs is a *wider
grant* on the broker's policy — one more host, chosen by name.

**No staged asset.** The playbook is generated here rather than uploaded, because unlike
the Resource Broker's installer there is nothing customer-specific to stage: k3s and the
chart both come from the internet. It rides in ``asset_bytes_b64`` the way the
agent-brokered storage backend's assets do, which means **the play text lands in the job
row** — so it contains no secret, and the agent token reaches the run through
``secret_vars`` like every other credential on this path.

**The token is per POV, not per tenant.** ``BeyondTrustTenant`` already carries an
``agent_token_name`` option, and reusing it here would be the silent cross-POV mistake:
two POVs sharing one customer's Entitle tenant would both name the first one's agent, and
Entitle would happily create integrations pointed at a network the target is not on.
Nothing errors; the SSH connector just cannot reach the host. So the mint is keyed to the
environment, the name is derived from its id, and the tenant option stays what it always
was — the manual answer for an agent an SE deployed themselves.

**k3s, not a bare container.** The agent ships as a Helm chart and nothing else, and
re-implementing it as ``docker run`` would mean owning a translation of a chart BeyondTrust
changes on its own schedule. What the chart actually needs is small — one Deployment, an
RBAC Role, a Secret, no PVC, no Service, no ingress — so a single-node k3s is enough, and
``kmsType=kubernetes_secret_manager`` keeps the agent's own key material in that cluster
rather than in a cloud KMS a POV has no account with.

**Chart defaults do not fit a POV VM** and the overrides are not cosmetic: the chart asks
for **three** replicas at 1 CPU / 1Gi of *requests* each, so on any VM an SE would give a
POV the pods stay Pending forever with no error anywhere except ``kubectl describe``. It
also runs a Datadog log-shipping sidecar even with ``datadog.enabled=false``, as a native
sidecar — an init container with ``restartPolicy: Always``, which additionally requires
Kubernetes 1.29+. Both are turned down here, and :data:`CHART_DEFAULTS` is where to look
when a future chart version moves them.
"""
from __future__ import annotations

import base64
import json
import logging

from sqlalchemy.orm import Session

from ..database import Job, PovEnvironment, PovEnvironmentVM
from . import (agent_ansible_meta, agent_service, bt_tenant_service, config_service,
               entitle_registration_service, job_service, pov_gateway)

logger = logging.getLogger(__name__)


class EntitleAgentError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# The VM in the template that hosts the agent. A convention, not a discovery — and
# deliberately not the broker VM: k3s brings its own containerd and its own iptables
# rules, and putting that on the one host whose job is to keep the agent channel up means
# the install can sever the connection it is being installed over.
DEFAULT_HOST_VM_NAME = "entitle"

# Where the host name lives. Not a secret — a VM name inside the environment — so it goes
# on the row's metadata rather than into the encrypted config space, the same split
# `pov_resource_broker` makes for its zone and asset.
_META_VM_NAME = "entitle_agent_vm_name"

# Where the minted token's NAME lives. Also not a secret, and worth keeping on the row
# rather than deriving it every time: `mint_name` is derived from the environment id, and
# a POV whose id somehow changed would otherwise orphan a token in the customer's tenant
# under a name nothing here remembers.
_META_TOKEN_NAME = "entitle_agent_token_name"

# The token VALUE and the mint's terraform state. Both Fernet-encrypted at rest by
# `config_service`, both resolvable from an external vault by reference, both carried by
# the config-migration tool — the same `pov/{env}/...` shape the Gateway deploy key and the
# Entitle SSH key already use.
#
# The state is a secret and not merely bookkeeping: Terraform records a sensitive
# attribute in state as PLAINTEXT, which is exactly why `entitle_registration_service`
# can recover an agent token's value from it at all.
_TOKEN_FMT = "pov/{env_id}/entitle_agent_token"
_STATE_FMT = "pov/{env_id}/entitle_agent_tf_state"

# The play variable the token is bound to. Only the NAME reaches the job row and the
# envelope; the value is resolved when the agent fetches the sealed bundle, exactly as
# `pov_resource_broker.KEY_VAR` does for the Resource Broker's installer key.
TOKEN_VAR = "entitle_agent_token"

# Absolute, because these commands run under sudo and `secure_path` on a RHEL-family
# guest does not include /usr/local/bin. A bare `helm` there is "command not found",
# which reads as a failed install rather than as the PATH problem it is.
K3S = "/usr/local/bin/k3s"
HELM = "/usr/local/bin/helm"

# SSH, because this is a Linux guest. Named rather than defaulted so it is visible beside
# `pov_resource_broker.RB_PORT`, which names 5985 for the same reason.
SSH_PORT = 22

# What the play installs, and the values that make the chart fit one small VM. Every entry
# is an `extra_vars` default the caller may override, so a POV on a bigger host can raise
# the limits without a code change — and so a chart version that moves one of these can be
# worked around from the outside while the fix ships.
#
# `agent_replicas` is the load-bearing one. See the module docstring.
CHART_DEFAULTS = {
    "entitle_agent_namespace": "entitle",
    "entitle_agent_release": "entitle-agent",
    "entitle_agent_chart": "entitle-agent",
    "entitle_agent_chart_repo": "https://anycred.github.io/entitle-charts/",
    "entitle_agent_chart_version": "",          # "" → whatever the repo calls latest
    "entitle_agent_replicas": 1,
    "entitle_agent_cpu_request": "250m",
    "entitle_agent_memory_request": "512Mi",
    "entitle_agent_cpu_limit": "2000m",
    "entitle_agent_memory_limit": "2Gi",
    # In-cluster Secrets. The cloud KMS backends the managed-cluster path can use all
    # assume an identity a POV VM does not have.
    "entitle_agent_kms_type": "kubernetes_secret_manager",
    # Traefik and servicelb are the two k3s add-ons that bind host ports. The agent needs
    # neither — it is outbound-only — and a POV VM that suddenly answers on :80 is a
    # change to the customer's environment nobody asked for.
    "k3s_install_exec": "--disable traefik --disable servicelb",
    "k3s_channel": "stable",
    "helm_version": "v3.16.3",
}

# How long to wait for the agent Deployment to report Available. Generous because the
# first run pulls k3s, Helm, the agent image and its sidecar over whatever the POV's
# egress is, and a timeout here reads as a broken install rather than a slow download.
ROLLOUT_TIMEOUT = "600s"


# ── stored values ────────────────────────────────────────────────────────────

def token_config_key(env_id: str) -> str:
    return _TOKEN_FMT.format(env_id=env_id)


def state_config_key(env_id: str) -> str:
    return _STATE_FMT.format(env_id=env_id)


def has_token(env: PovEnvironment) -> bool:
    return bool(config_service.get(token_config_key(env.id)))


def agent_token_name(env: PovEnvironment) -> str:
    """The Entitle agent token this POV's integrations must name, or ``""``.

    The reason ``pov_wireup`` asks here before falling back to the tenant option: an agent
    installed *in this environment* is the one that can reach it, and the tenant's is
    whatever the last SE typed.
    """
    return str(env.metadata_dict.get(_META_TOKEN_NAME) or "").strip()


def mint_name(env: PovEnvironment) -> str:
    """The name a fresh token gets. Deterministic, and unique inside the tenant.

    Derived from the environment id rather than only its name because Entitle refuses to
    mint a name that already exists **and cannot read the value back** — so two POVs that
    happened to share a name would leave the second one permanently unable to install,
    with a remedy that involves deleting the first one's token.
    """
    slug = (env.name or "pov").strip().lower()[:24].strip("-") or "pov"
    return f"pov-{slug}-{env.id[:8]}"


def host_vm_name(env: PovEnvironment) -> str:
    return (str(env.metadata_dict.get(_META_VM_NAME) or "").strip()
            or DEFAULT_HOST_VM_NAME)


def stored_host_vm_name(env: PovEnvironment) -> str:
    """What was actually stored, or "" — as opposed to :func:`host_vm_name`, which
    substitutes the default. A caller deciding whether to fill a blank field needs to tell
    "nobody set this" from "somebody set it to the default"."""
    return str(env.metadata_dict.get(_META_VM_NAME) or "").strip()


def configure(db: Session, env: PovEnvironment, *, vm_name: str | None = None) -> None:
    """Set the non-secret half. ``None`` leaves a field alone; ``""`` clears it."""
    if vm_name is None:
        return
    meta = env.metadata_dict
    value = str(vm_name).strip()
    if value:
        meta[_META_VM_NAME] = value
    else:
        meta.pop(_META_VM_NAME, None)
    env.metadata_dict = meta
    db.commit()


def token_for_job(db: Session, job: Job) -> str:
    """The agent token for a job, derived from the job row.

    Called from the bundle assembler, never from a request body — the rule
    ``pov_gateway.deploy_key_for_job`` and ``pov_resource_broker.installer_key_for_job``
    both follow, because without it a stolen agent identity could ask for another POV's
    token against a job it legitimately owns.
    """
    env_id = str((job.metadata_dict or {}).get("pov_environment_id") or "")
    if not env_id:
        raise EntitleAgentError("this job names no POV environment")
    env = db.query(PovEnvironment).filter(PovEnvironment.id == env_id).first()
    if env is None:
        raise EntitleAgentError("the POV environment this job belongs to is gone")
    token = config_service.get(token_config_key(env.id))
    if not token:
        raise EntitleAgentError(
            "this POV has no Entitle agent token stored. Re-run the install — the token "
            "is minted in the POV's own Entitle tenant when the job is queued.")
    return token


# ── the host ─────────────────────────────────────────────────────────────────

def select_host_vm(db: Session, env: PovEnvironment) -> PovEnvironmentVM:
    """The Linux VM that will host the agent, or a refusal naming what it found.

    Exact name match, case-insensitively — the same rule ``pov_resource_broker`` and
    ``pov_broker`` follow, and deliberately not "the first Linux VM": a POV template with
    a web tier and a database has several, and installing a Kubernetes distribution on
    whichever the platform happened to list first is not a decision a position in a list
    can make.
    """
    wanted = host_vm_name(env).strip().lower()
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    if not rows:
        raise EntitleAgentError(
            "this environment has no VM rows yet, so there is nowhere to install an "
            "Entitle agent. Refresh the POV once the platform reports its VMs.")

    match = next((r for r in rows if (r.name or "").strip().lower() == wanted), None)
    if match is None:
        found = ", ".join(sorted((r.name or r.platform_vm_id) for r in rows)) or "none"
        raise EntitleAgentError(
            f"no VM in this environment is named {wanted!r}, so there is nowhere to "
            f"install the Entitle agent. Found: {found}. Rename the template's VM, or set "
            f"a different Entitle agent host on this POV.")

    family = (match.os_family or "").strip().lower()
    if family != "linux":
        # Blank means the platform did not say, and the POV feature has refused to guess
        # since slice 3. Here the cost of a wrong guess is concrete: the k3s installer is
        # a shell script, so a Windows guest fails somewhere inside a play that has
        # already logged in, which reads as a broken playbook.
        reported = match.os_family or "nothing"
        raise EntitleAgentError(
            f"{match.name!r} reports its OS as {reported}, and the Entitle agent runs on "
            f"Linux. Point this POV at a Linux VM — the k3s installer is a shell script "
            f"and a Windows guest fails partway through it.")
    if not (match.private_ip or "").strip():
        raise EntitleAgentError(
            f"{match.name!r} has no private address yet, so there is nothing to connect "
            f"to. Power the environment on and refresh the POV.")
    return match


def linux_targets(db: Session, env: PovEnvironment) -> list[str]:
    """The addresses a POV's broker may run this playbook against.

    The named host alone when it is resolvable, and every Linux VM otherwise. That
    fallback exists for the same reason ``pov_resource_broker.windows_targets``'s does:
    the broker's ``policy.yaml`` is written at *enrolment*, usually before anyone has
    chosen a host, so the alternative is a policy that grants nothing until the POV is
    re-brokered.
    """
    try:
        return [select_host_vm(db, env).private_ip]
    except EntitleAgentError:
        from . import pov_broker
        broker = pov_broker.broker_vm_name(env).strip().lower()
        rows = (db.query(PovEnvironmentVM)
                  .filter(PovEnvironmentVM.environment_id == env.id).all())
        # The broker VM is excluded from the FALLBACK, not from the named host. Nothing
        # stops an SE naming it deliberately, but a list this dashboard guessed must not
        # include the one guest whose job is to keep the agent channel up — see
        # DEFAULT_HOST_VM_NAME.
        return sorted({(r.private_ip or "").strip() for r in rows
                       if (r.os_family or "").strip().lower() == "linux"
                       and (r.private_ip or "").strip()
                       and (r.name or "").strip().lower() != broker})


# ── the tenant ───────────────────────────────────────────────────────────────

def entitle_tenant(db: Session, env: PovEnvironment):
    """The Entitle tenant this POV is wired into.

    Explicit id, so a POV whose tenant was deleted or disabled is an **error** rather than
    a quiet fall back to the default — which here would mint a token in the wrong
    customer's tenant and hand it to a VM in this one.
    """
    if not env.entitle_tenant_id:
        raise EntitleAgentError(
            "this POV names no Entitle tenant, so there is no tenant to mint an agent "
            "token in. Set one on the POV first.")
    try:
        return bt_tenant_service.resolve(db, "entitle", env.entitle_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise EntitleAgentError(
            f"this POV's Entitle tenant could not be resolved: {exc}") from None


def _ctx(tenant):
    """The registration context for this POV's tenant. Its API key, never the install's.

    ``entitle_registration_service._api_key_of`` refuses a context with no key rather than
    falling back, which is the property that matters here: minting into the configured
    tenant instead of the customer's would look exactly like success.
    """
    return entitle_registration_service.tenant_ctx(
        api_key=tenant.secret, endpoint=tenant.base_url)


async def ensure_token(db: Session, env: PovEnvironment, tenant) -> str:
    """This POV's agent token, minting one in its tenant if there is none. Idempotent.

    Returns the value and records three things: the value, the mint's terraform state
    (which is how teardown destroys it), and the NAME — the last of which is what
    ``pov_wireup`` reads when it builds an integration, so a successful install is what
    stops the SSH wire-up refusing for want of an agent.

    A stored value short-circuits the mint. That is not just a saving: Entitle returns an
    agent token's value **only at creation** and refuses to re-mint a name it already
    holds, so a second mint against a POV that already has one is unrecoverable rather
    than merely wasteful.
    """
    existing = config_service.get(token_config_key(env.id))
    if existing:
        return existing

    name = agent_token_name(env) or mint_name(env)
    try:
        minted = await entitle_registration_service.mint_agent_token(name, _ctx(tenant))
    except entitle_registration_service.EntitleRegistrationError as exc:
        raise EntitleAgentError(
            f"could not mint an Entitle agent token in tenant {tenant.name!r}: {exc}"
        ) from None

    config_service.set(token_config_key(env.id), minted["token"])
    if minted.get("tf_state_json"):
        config_service.set(state_config_key(env.id), minted["tf_state_json"])
    meta = env.metadata_dict
    meta[_META_TOKEN_NAME] = name
    env.metadata_dict = meta
    db.commit()
    logger.info("POV %s: minted Entitle agent token %r in tenant %s",
                env.id, name, tenant.name)
    return minted["token"]


# ── the playbook ─────────────────────────────────────────────────────────────

def playbook_yaml() -> str:
    """The play, generated rather than staged or shipped as a file.

    Generated for two reasons. A file under ``provisioners/`` is not COPYed into the
    image, so it would be a runtime ``FileNotFoundError`` on a deploy rather than a build
    failure; and ``ansible_local_service.generate_playbook_yaml`` already establishes that
    a play this codebase authors is a string it builds.

    **Nothing secret is in here.** The play text travels in the job row's
    ``asset_bytes_b64``, which is database metadata in plain text. The token arrives as
    ``{{ entitle_agent_token }}`` from ``secret_vars``, resolved into the sealed bundle
    when the agent asks for it, and lands on the host only inside a 0600 values file the
    play deletes in an ``always`` block.

    Every command goes through ``k3s kubectl`` and ``helm`` rather than the
    ``kubernetes.core`` modules, because the agent's sibling Ansible image is not
    guaranteed to carry that collection and a missing-collection error reads as a broken
    playbook.
    """
    d = CHART_DEFAULTS
    play = [{
        "name": "Install the Entitle agent on single-node k3s",
        "hosts": "all",
        "gather_facts": True,
        # Not at play level: the first task decides what to sudo WITH, and a play-level
        # become would make that decision need sudo already.
        "become": False,
        "vars": {k: v for k, v in d.items()},
        "tasks": [
            {
                "name": "Sudo with the login password, when the platform supplied one",
                # A POV login comes from the lab platform, and the agent binds it to
                # ansible_ssh_pass. A key-authenticated or root login has none, and then
                # this fact is never set and become falls through to NOPASSWD sudo —
                # which is why this is a conditional set_fact and not a play var.
                "ansible.builtin.set_fact": {
                    "ansible_become_password": "{{ ansible_ssh_pass }}"},
                "when": "ansible_ssh_pass is defined and ansible_ssh_pass | length > 0",
                "no_log": True,
            },
            {
                "name": "Install and configure",
                "become": True,
                "block": [
                    {"name": "Look for an existing k3s",
                     "ansible.builtin.stat": {"path": K3S},
                     "register": "k3s_binary"},
                    {"name": "Fetch the k3s installer",
                     "ansible.builtin.get_url": {
                         "url": "https://get.k3s.io",
                         "dest": "/tmp/k3s-install.sh",
                         "mode": "0700"},
                     "when": "not k3s_binary.stat.exists"},
                    {"name": "Install k3s",
                     "ansible.builtin.command": {"cmd": "/bin/sh /tmp/k3s-install.sh"},
                     "environment": {
                         "INSTALL_K3S_CHANNEL": "{{ k3s_channel }}",
                         "INSTALL_K3S_EXEC": "{{ k3s_install_exec }}"},
                     "when": "not k3s_binary.stat.exists"},
                    {"name": "Wait for the node to report Ready",
                     # k3s writes its kubeconfig before the API server answers, so this is
                     # the first thing that proves the cluster is actually up. Retried
                     # rather than given one long timeout so the output shows progress.
                     "ansible.builtin.command": {
                         "cmd": f"{K3S} kubectl wait --for=condition=Ready node --all "
                                f"--timeout=60s"},
                     "register": "k3s_ready",
                     "retries": 10,
                     "delay": 15,
                     "until": "k3s_ready.rc == 0",
                     "changed_when": False},
                    {"name": "Look for an existing helm",
                     "ansible.builtin.stat": {"path": HELM},
                     "register": "helm_binary"},
                    {"name": "Fetch helm",
                     # The release tarball rather than get-helm-3, which pipes a script
                     # into a shell and picks its own version. A pinned URL is a pinned
                     # binary, and this one is checked against the arch ansible reported.
                     "ansible.builtin.unarchive": {
                         "src": "https://get.helm.sh/helm-{{ helm_version }}-linux-"
                                "{{ 'arm64' if ansible_architecture == 'aarch64' "
                                "else 'amd64' }}.tar.gz",
                         "dest": "/tmp",
                         "remote_src": True},
                     "when": "not helm_binary.stat.exists"},
                    {"name": "Put helm on the path",
                     "ansible.builtin.copy": {
                         "src": "/tmp/linux-{{ 'arm64' if ansible_architecture == "
                                "'aarch64' else 'amd64' }}/helm",
                         "dest": HELM,
                         "mode": "0755",
                         "remote_src": True},
                     "when": "not helm_binary.stat.exists"},
                    {"name": "Write the chart values",
                     # 0600 and deleted in `always`. The token is the one secret on this
                     # host, and a values FILE is why the helm command below can log its
                     # own output: --set would put the token in argv, where it is visible
                     # in `ps` to every local user for the length of the install.
                     "ansible.builtin.copy": {
                         "content": _values_yaml(),
                         "dest": "/root/.entitle-agent-values.yaml",
                         "mode": "0600"},
                     "no_log": True},
                    {"name": "Install the Entitle agent chart",
                     "ansible.builtin.command": {
                         "cmd": f"{HELM} upgrade --install {{{{ entitle_agent_release }}}} "
                                "{{ entitle_agent_chart }} "
                                "--repo {{ entitle_agent_chart_repo }} "
                                "{% if entitle_agent_chart_version %}"
                                "--version {{ entitle_agent_chart_version }} {% endif %}"
                                "--namespace {{ entitle_agent_namespace }} "
                                "--create-namespace "
                                "--values /root/.entitle-agent-values.yaml "
                                f"--wait --timeout {ROLLOUT_TIMEOUT}"},
                     "environment": {"KUBECONFIG": "/etc/rancher/k3s/k3s.yaml"},
                     "register": "helm_install"},
                    {"name": "Report what is running",
                     "ansible.builtin.command": {
                         "cmd": f"{K3S} kubectl get deploy,pods -n "
                                "{{ entitle_agent_namespace }}"},
                     "changed_when": False},
                ],
                "rescue": [
                    {"name": "Show why the agent did not start",
                     # The one thing an operator needs and `helm --wait` does not print.
                     # `|| true` because this runs when something already failed, and a
                     # cluster too broken to answer must not replace the real error with
                     # this task's.
                     "ansible.builtin.shell": {
                         "cmd": f"{K3S} kubectl get pods -n {{{{ entitle_agent_namespace }}}} "
                                f"-o wide || true; "
                                f"{K3S} kubectl describe pods -n "
                                "{{ entitle_agent_namespace }} || true; "
                                f"{K3S} kubectl logs -n {{{{ entitle_agent_namespace }}}} "
                                "-l app.kubernetes.io/name={{ entitle_agent_chart }} "
                                "--all-containers --tail=100 || true"},
                     "changed_when": False},
                    {"name": "Fail with the original error",
                     "ansible.builtin.fail": {
                         "msg": "The Entitle agent install failed - the pod state and "
                                "logs above say why. A pod stuck Pending is almost "
                                "always this VM being smaller than the chart's resource "
                                "requests; ImagePullBackOff is egress to ghcr.io."}},
                ],
                "always": [
                    {"name": "Remove the values file",
                     "ansible.builtin.file": {
                         "path": "/root/.entitle-agent-values.yaml",
                         "state": "absent"}},
                ],
            },
        ],
    }]
    return _to_yaml(play)


def _values_yaml() -> str:
    """The chart values, as a Jinja-templated string the play renders on the host.

    A string rather than a nested dict so the *file* the play writes is YAML the operator
    can read in the job output's task name and recognise as helm values — and so the
    numbers stay `extra_vars` a caller can override without this function knowing.
    """
    return (
        "agent:\n"
        "  token: \"{{ " + TOKEN_VAR + " }}\"\n"
        "  replicas: {{ entitle_agent_replicas }}\n"
        "  resources:\n"
        "    requests:\n"
        "      cpu: \"{{ entitle_agent_cpu_request }}\"\n"
        "      memory: \"{{ entitle_agent_memory_request }}\"\n"
        "    limits:\n"
        "      cpu: \"{{ entitle_agent_cpu_limit }}\"\n"
        "      memory: \"{{ entitle_agent_memory_limit }}\"\n"
        "kmsType: \"{{ entitle_agent_kms_type }}\"\n"
        "platform:\n"
        "  mode: native\n"
        "datadog:\n"
        "  enabled: false\n"
        # Not redundant with the line above. `enabled: false` selects the lightweight
        # sidecar INSTEAD of the full Datadog DaemonSet; it does not turn logging off.
        # The sidecar is a native sidecar (an init container with restartPolicy: Always),
        # which needs Kubernetes 1.29+ and pulls a second image a POV has no use for.
        "  sidecarLogs: false\n"
        "global:\n"
        "  environment: onprem\n"
    )


def _to_yaml(data) -> str:
    """The play as YAML. Uses PyYAML when it is importable and hand-renders otherwise.

    The fallback exists because this module is imported by the POV API on every install
    and a hard dependency for a string builder is a dependency the whole page fails on.
    PyYAML is in `requirements.txt`, so the fallback is belt-and-braces rather than a
    supported mode — but it is exercised by the tests either way.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover — PyYAML is a hard requirement of the app
        return json.dumps(data, indent=2)
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False, width=10000)


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(db: Session, env: PovEnvironment) -> tuple:
    """Everything checkable before a job row exists. Returns ``(agent, vm, tenant)``.

    Ordered cheapest-and-most-likely-wrong first, so an SE who has not pressed Broker is
    told that rather than told about a tenant they were about to be asked for anyway.
    """
    agent = pov_gateway.broker_agent(db, env)   # same refusals, same remedies

    if not agent_service.supports_ansible(agent):
        raise EntitleAgentError(agent_service.ansible_upgrade_hint(agent))
    reported = agent.reported_job_types_list
    if reported and "agent_ansible" not in reported:
        raise EntitleAgentError(
            f"the broker agent {agent.name!r} reports it may run "
            f"{', '.join(reported) or 'nothing'} — its policy.yaml predates the Config "
            f"Management grant. Press Broker on this POV to rewrite it, then try again.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise EntitleAgentError(
            f"this dashboard does not permit {agent.name!r} to run Config Management. "
            f"Widen its allowed job types on the Agents page.")

    tenant = entitle_tenant(db, env)
    vm = select_host_vm(db, env)
    return agent, vm, tenant


# ── the job ──────────────────────────────────────────────────────────────────

async def queue(db: Session, env: PovEnvironment, *, created_by: str = "",
                overrides: dict | None = None) -> Job:
    """Mint the token if needed, then queue the install on this POV's broker agent.

    The mint happens **here** rather than inside the run because it is the one step that
    changes the customer's tenant, and a job that fails at that point has already created
    a token nothing records. Doing it before the row exists means a failure is a 400 on
    the button with nothing to clean up.
    """
    agent, vm, tenant = preflight(db, env)
    await ensure_token(db, env, tenant)

    extra_vars = dict(CHART_DEFAULTS)
    for key, value in (overrides or {}).items():
        if key in CHART_DEFAULTS:
            extra_vars[key] = value

    meta = agent_ansible_meta.run_meta(
        object(),
        description=f"Install the Entitle agent for POV {env.name}",
        # No backend: the playbook rides in the row rather than being fetched from
        # storage, so there is nothing for the assembler to read.
        asset_backend="",
        run_kind="vm",
        transport="ssh",
        target_host=vm.private_ip,
        target_port=SSH_PORT,
        target_label=vm.name or vm.platform_vm_id,
        # A name, not a key. `asset_bytes_b64` below is what the assembler actually reads;
        # this only has to end in .yml so the auto-wrap treats it as a playbook rather
        # than as a file to copy and run.
        asset="entitle-agent-k3s.yml",
        # The var NAME, never the token. Resolved when the agent fetches the bundle.
        secret_vars={TOKEN_VAR: token_config_key(env.id)},
        extra_vars=extra_vars,
        # How the assembler knows to fetch this VM's login from the lab platform. Ids, not
        # credentials — the discipline every other key in RUN_META_KEYS follows.
        pov_environment_id=env.id,
        pov_vm_id=vm.platform_vm_id)
    # Outside run_meta because it is deliberately not in RUN_META_KEYS: the closed
    # allowlist is about refs and ids, and this is the play itself. Read straight off the
    # raw metadata by `agent_ansible_bundle._playbook_and_asset`, the same path the
    # agent-brokered storage backend's prefetch uses.
    meta["asset_bytes_b64"] = base64.b64encode(playbook_yaml().encode()).decode()

    job = job_service.create_job(
        db, job_type="agent_ansible", created_by=created_by,
        workgroup=env.workgroup, agent_id=agent.id, metadata=meta)
    logger.info("POV %s: queued an Entitle agent install on %s via agent %s",
                env.id, vm.name, agent.name)
    return job


# ── teardown ─────────────────────────────────────────────────────────────────

async def teardown(db: Session, env: PovEnvironment) -> str:
    """Destroy this POV's agent token and forget the rest. Returns a job-log line.

    Deliberately does **not** uninstall anything on the VM: the environment delete takes
    the whole guest moments later, and an uninstall run would need the SSH session that
    delete is about to make unreachable.

    The token is different, and is the reason this is async. It lives in the *customer's*
    Entitle tenant, so nothing this dashboard deletes locally removes it — and because
    Entitle refuses to mint a name it already holds, a surviving token wedges every future
    install for a POV that reuses the name. A failure here is reported, never raised: a
    POV that cannot be destroyed because a customer's API was briefly down is worse than
    a token an operator has to retire by hand, and the line below tells them which.
    """
    lines = []
    state = config_service.get(state_config_key(env.id))
    name = agent_token_name(env)
    if state:
        try:
            tenant = entitle_tenant(db, env)
            await entitle_registration_service.deregister(state, _ctx(tenant))
            lines.append(f"Destroyed the Entitle agent token {name or '(unnamed)'}.")
        except Exception as exc:  # noqa: BLE001 — teardown reports, never blocks
            logger.warning("POV %s: destroying the Entitle agent token failed",
                           env.id, exc_info=True)
            lines.append(
                f"Could not destroy the Entitle agent token {name or '(unnamed)'} "
                f"({type(exc).__name__}) — delete it in the Entitle tenant, or the next "
                f"POV to use that name cannot mint one.")

    for key in (token_config_key(env.id), state_config_key(env.id)):
        try:
            config_service.delete(key)
        except Exception:  # noqa: BLE001 — a key already gone is not a failure
            logger.debug("POV %s: no %s to clear", env.id, key)

    meta = env.metadata_dict
    if meta.pop(_META_TOKEN_NAME, None) is not None:
        env.metadata_dict = meta
        db.commit()

    return " ".join(lines) or "No Entitle agent to clean up."


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The agent's configured state for one POV row — no network calls.

    Deliberately does not ask the VM. This runs once per row on the POV list, and an SSH
    round trip per row would make the page as slow as the slowest customer's network.
    """
    return {
        "entitle_agent_vm_name": host_vm_name(env),
        "entitle_agent_token_name": agent_token_name(env),
        "entitle_agent_installed": has_token(env) and bool(agent_token_name(env)),
        "entitle_agent_ready": bool(env.entitle_tenant_id and env.broker_agent_id),
    }
