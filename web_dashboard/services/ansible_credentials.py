"""Just-in-time credential resolution for a Config-Management run.

Every run — whichever runner executes it — needs the same five things turned from *refs*
into *values* at the moment of dispatch: named Secrets-Management vars, a become password,
an SSH private key, an EPM-L installation token, and a BeyondTrust Password Safe
managed-account checkout. Job metadata deliberately carries only the refs
(``ansible_run_meta``, ``agent_ansible_meta``), so this is where they become secrets.

It was inline in ``ansible_local_run_service._run_job`` until an agent-executed run needed
the identical resolution to build its sealed bundle. Two copies of *this* would be the
worst possible thing to duplicate: a divergence would not fail loudly, it would use the
wrong credential, or the right credential over the wrong channel — the same class of bug
that a shared ``PAGE_OPS`` table exists to prevent for hypervisor verbs.

**The routing distinction is preserved rather than collapsed**, because it is the whole
reason this is not one flat dict. A managed-account credential must reach a cloud runner
through an ephemeral secret store, while ``ansible_user`` is an ordinary variable — so
:class:`ResolvedCredentials` keeps ``managed_cred_vars`` and ``managed_plain_vars`` apart
and lets each caller route them. ``extra_vars`` is the pre-merged form the inline runners
(local Docker, ACI, and the agent's sibling) consume directly.

Errors raise :class:`CredentialError` rather than failing the job here. The caller owns the
job lifecycle: the local runner turns it into ``set_failed``, the agent bundle route into a
409 whose detail the agent puts straight into the job's error message.
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """A ref that could not be turned into a value. The message is operator-facing."""


@dataclass
class ResolvedCredentials:
    """Values for one run. Every string in :attr:`scrub` must be redacted from output.

    ``extra_vars`` already has ``managed_cred_vars`` and ``managed_plain_vars`` merged in,
    which is what an inline runner wants. A cloud runner reads the two separately instead
    and moves only the former through an ephemeral store — hence all three.
    """
    extra_vars: dict = field(default_factory=dict)
    ssh_pem: str | None = None
    managed_cred_vars: dict = field(default_factory=dict)
    managed_plain_vars: dict = field(default_factory=dict)
    request_ids: list = field(default_factory=list)
    scrub: list = field(default_factory=list)


def _cfg(key: str) -> str:
    from . import ansible_local_service
    return ansible_local_service._cfg(key)


async def resolve_managed_ref(db, ref: dict, target: str, cloud: str,
                              name_hint: str = "") -> dict:
    """Fill in a name-only managed-account ref against ``target``'s own host.

    A bulk run picks an account by NAME, because ``system_id``/``account_id`` belong to one
    managed system — reusing one pinned ref across a fleet would check out a single
    machine's credential and connect everywhere with it. Each job therefore resolves the
    name against the host it is actually configuring.

    Already-pinned refs (a single run from the picker) pass through untouched, so this
    costs a Password Safe lookup only on the bulk path.

    ``name_hint`` is the caller's answer to "what else might this managed system be called":
    cloud-native onboarding registers a system under the *deploy name* with a placeholder
    IP, so an IP-only lookup misses it. The caller supplies it because only the caller knows
    where to look — the cloud paths read it off the deploy job, and an **on-prem VM behind
    an agent has no deploy job at all**, so it passes ``""`` and matches on IP alone. That
    is correct rather than a degradation: an on-prem host is onboarded by address.

    Raises :class:`CredentialError` when the host has no such account, so the caller fails
    just this job and leaves the rest of a batch alone.
    """
    if not ref or ref.get("account_id") is not None:
        return ref
    from . import btapi_service, managed_accounts as ma

    wanted = (ref.get("account_name") or "").strip()
    ip, sys_name = ma.lookup_args(target, name_hint)

    systems = await btapi_service.list_ps_managed_systems_by_ip_or_name(ip, sys_name)
    accounts_by_system = {}
    for s in systems:
        sid = s.get("ManagedSystemID") or s.get("SystemId") or s.get("SystemID")
        if sid is None:
            continue
        accounts_by_system[int(sid)] = \
            await btapi_service.list_ps_managed_accounts_with_fallback(int(sid))

    found = ma.find_account_by_name(
        ma.normalize_managed_systems(systems, accounts_by_system), wanted)
    if not found:
        raise CredentialError(
            f"Password Safe has no managed account named {wanted!r} for host "
            f"{target!r}. Onboard it there, or run this host separately with an "
            f"account picked from its own list.")
    return found


async def resolve(db, *, secret_vars=None, secret_become_source: str = "",
                  secret_ssh_key_source: str = "", epml_token_var: str = "",
                  managed_account=None, managed_become=None,
                  target: str = "", cloud: str = "",
                  name_hint: str = "") -> ResolvedCredentials:
    """Turn a run's credential refs into values, once, just-in-time.

    Nothing here is cached and nothing is written down: every value exists only in the
    returned object, for the length of one dispatch. That is the property the one-shot
    runner doctrine rests on, and it holds identically for an agent run — the agent seals
    these into a bundle it holds in memory for one child container.
    """
    out = ResolvedCredentials()

    if secret_vars or secret_become_source or secret_ssh_key_source:
        from . import ansible_secrets, config_service as cs

        def _resolve_source(src: str) -> str:
            src = (src or "").strip()
            if not src:
                return ""
            return cs.resolve_reference(src) if cs.is_reference(src) else cs.get(src)

        out.extra_vars = ansible_secrets.resolve_secret_vars(
            secret_vars, get=cs.get, resolve_reference=cs.resolve_reference,
            is_reference=cs.is_reference)
        if secret_become_source:
            become = _resolve_source(secret_become_source)
            if become:
                out.extra_vars["ansible_become_password"] = become
        if secret_ssh_key_source:
            out.ssh_pem = _resolve_source(secret_ssh_key_source) or None
        out.scrub = [v for v in list(out.extra_vars.values())
                     + ([out.ssh_pem] if out.ssh_pem else []) if v]

    # EPM-L installation token — minted here, at run time, and bound to the var the
    # operator named. Registration tokens are short-lived (hours), so one fetched earlier
    # and stored would already be dead; and the job metadata carries only the var NAME, so
    # the token never reaches the database or the browser.
    if epml_token_var:
        from . import epml_service
        try:
            token = await epml_service.get_installation_token()
        except epml_service.EpmlError as exc:
            raise CredentialError(f"EPM-L token request failed: {exc}") from exc
        out.extra_vars[epml_token_var] = token
        out.scrub.append(token)

    # Managed-account checkout (BeyondTrust Password Safe), just-in-time. The account is
    # the connection identity; managed_become is an optional separate account for the
    # sudo/become password.
    if managed_account or managed_become:
        from . import ansible_local_service, btapi_service, managed_accounts as ma
        # Long enough that the request is still open after the run for the
        # rotate-on-check-in + check-in the cloud runners do.
        duration = int(_cfg("ansible_managed_request_duration_min") or 60)
        try:
            managed_account = await resolve_managed_ref(
                db, managed_account, target, cloud, name_hint)
            managed_become = await resolve_managed_ref(
                db, managed_become, target, cloud, name_hint)
        except btapi_service.BTAPIError as exc:
            raise CredentialError(f"Password Safe lookup failed: {exc}") from exc
        try:
            if managed_account:
                req_id, cred = await btapi_service.get_ps_credential_with_request(
                    managed_account["system_id"], managed_account["account_id"],
                    duration_min=duration,
                    uses_ssh_key=managed_account.get("uses_ssh_key", False))
                out.request_ids.append(req_id)
                # The account is the login identity → ansible_user. Strip any cloud-plugin
                # scope suffix (e.g. AWS Systems Manager's ``adminuser;local``) so the
                # connection uses the real OS user.
                login = ma.ssh_login_user(managed_account.get("account_name", ""))
                if login:
                    out.managed_plain_vars["ansible_user"] = login
                # The credential is an SSH private key when the account is DSS-managed
                # (uses_ssh_key) OR the AWS Systems Manager Custom Plugin — whose account
                # "password" IS the minted private key, WITHOUT the DSS flag set. Detect by
                # content so the key is used as the connection key rather than a password:
                # password routing would send it down the ephemeral-store path, and Ubuntu
                # rejects password SSH anyway. Normalize so OpenSSH accepts it.
                if managed_account.get("uses_ssh_key") or "PRIVATE KEY" in (cred or ""):
                    out.ssh_pem = ansible_local_service._normalize_key(cred)
                else:
                    out.managed_cred_vars["ansible_ssh_pass"] = cred   # SSH password (sshpass)
                    out.managed_cred_vars["ansible_password"] = cred   # WinRM targets
                out.scrub.append(cred)
            if managed_become:
                breq_id, bcred = await btapi_service.get_ps_credential_with_request(
                    managed_become["system_id"], managed_become["account_id"],
                    duration_min=duration, uses_ssh_key=False)
                out.request_ids.append(breq_id)
                out.managed_cred_vars["ansible_become_password"] = bcred
                out.scrub.append(bcred)
        except btapi_service.BTAPIError as exc:
            raise CredentialError(f"Password Safe checkout failed: {exc}") from exc
        # Inline runners consume everything through extra_vars; a cloud runner reads the
        # two dicts separately and routes managed_cred_vars through an ephemeral store.
        out.extra_vars.update(out.managed_cred_vars)
        out.extra_vars.update(out.managed_plain_vars)

    return out
