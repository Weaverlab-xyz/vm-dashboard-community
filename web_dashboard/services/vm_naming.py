"""
Deterministic VM name expansion for count-based deploys — a pure, dependency-free helper.

The four cloud deploy forms take a base name and a count; this module turns
``("web", 3)`` into ``["web-01", "web-02", "web-03"]``. It lives here rather than in
each endpoint because all four need the identical rules, and the rules are exactly
the kind of off-by-one that belongs under a unit test instead of behind a cloud SDK
call. The browser mirrors ``expand`` in ``static/js/app.js`` to preview the names —
this module is authoritative, and ``tests/test_vm_naming.py`` pins the fixtures both
sides must agree on.

Two rules do the real work:

  * **The base is truncated, never the suffix.** Cutting ``web-server-01`` to fit a
    limit by trimming the tail would produce ``web-server-0`` and ``web-server-0`` —
    two VMs with one name. Trimming the base first keeps the series unique by
    construction, which is the whole point of the helper.
  * **The per-provider limit is the length the *expanded* name must fit in**, and for
    Azure that is 15, not the 64-char resource-name limit. See ``_LIMITS``.

``count == 1`` is the caller's "unchanged" path: the endpoints never call ``expand``
for a single deploy, and if they do it returns the base verbatim so single-deploy
naming can never drift from what it was before counts existed.

Kept import-free (stdlib ``re`` only) so it is trivially unit-testable without the
cloud SDKs — same reasoning as services/oci_freetier.py.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set

# Ceiling on one deploy request. A batch occupies exactly one worker slot and
# deploys sequentially (see the *_bulk_deploy runners), so N VMs costs N x per-VM
# wall time on a queue shared with packer builds and k8s provisions. 20 keeps the
# worst case near half an hour and sits below the default cloud vCPU quotas that
# would otherwise turn a large count into a batch of failed children.
# Mirrored by window.DEPLOY_COUNT_MAX in static/js/app.js.
MAX_DEPLOY_COUNT = 20

# Below this a numbered name is meaningless ("w-01"), and it is the signal that a
# provider's limit left no room for the base at all.
MIN_BASE_CHARS = 3

# provider -> the length the EXPANDED name must fit in.
#
#   aws    255  EC2 `Name` tag value limit. Permissive; truncation never fires in
#               practice, and AWS addresses instances by id anyway.
#   azure   15  NOT the 64-char ARM resource-name limit. azure_service.deploy_vm
#               derives the in-guest hostname as `computer_name=vm_name[:15]` for
#               Linux AND Windows, so two VMs whose names differ only past the 15th
#               character get the SAME hostname — which silently breaks Entitle and
#               Password Safe onboarding, both of which key off hostname.
#   gcp     63  RFC1035, enforced by GCE itself.
#   oci    255  display_name; oci_service.launch_instance sets no hostname label.
_LIMITS = {"aws": 255, "azure": 15, "gcp": 63, "oci": 255}

# Providers whose names are DNS labels: lowercase, [a-z0-9-], leading letter, no
# trailing hyphen. Violations are rejected rather than silently rewritten — the
# operator typed the name and then has to find it in the cloud console, so turning
# "Web Server" into "web-server" behind their back is the worse failure.
_RFC1035 = re.compile(r"^[a-z]([a-z0-9-]*[a-z0-9])?$")
_DNS_PROVIDERS = ("gcp",)


class VMNameError(ValueError):
    """A base name that cannot be safely expanded. The message is operator-facing —
    endpoints surface it verbatim in the HTTP detail."""


def suffix_width(count: int, start: int = 1) -> int:
    """Zero-pad width for a series of ``count`` names starting at ``start``.

    Derived from the highest index rather than each index, so a batch never mixes
    ``-9`` and ``-10``. Floors at 2 so every count within MAX_DEPLOY_COUNT reads
    ``-01``; a cap raised past 99 widens it automatically."""
    return max(2, len(str(start + max(int(count), 1) - 1)))


def name_limit(provider: str) -> int:
    """The length an expanded name must fit in for ``provider``.

    Public because callers that name a *single* resource still need the limit:
    ``validate_base`` deliberately doesn't apply it (a base is allowed to be long
    enough that only the expansion trims it), and ``expand`` bakes it into a suffix
    budget. A caller naming one thing has neither."""
    if provider not in _LIMITS:
        raise VMNameError(f"Unknown provider {provider!r}.")
    return _LIMITS[provider]


def validate_base(base: str, provider: str) -> str:
    """Return the stripped base, or raise VMNameError if it can never expand legally.

    Only the DNS-label providers get a character check; AWS tags and OCI display
    names accept effectively anything, and rejecting there would be inventing a
    restriction the cloud does not have."""
    if provider not in _LIMITS:
        raise VMNameError(f"Unknown provider {provider!r}.")
    stem = (base or "").strip()
    if not stem:
        raise VMNameError("A base name is required.")
    if provider in _DNS_PROVIDERS and not _RFC1035.match(stem):
        raise VMNameError(
            f"{provider.upper()} instance names must be lowercase letters, numbers "
            f"and hyphens, and must start with a letter (RFC1035) — got {stem!r}."
        )
    return stem


def expand(base: str, count: int, provider: str, *, start: int = 1) -> List[str]:
    """``("web", 3, "gcp")`` -> ``["web-01", "web-02", "web-03"]``.

    ``count == 1`` returns ``[base]`` unsuffixed and untruncated: that is the
    single-deploy path, and it must stay byte-identical to pre-count behaviour."""
    stem = validate_base(base, provider)
    n = int(count)
    if n < 1:
        raise VMNameError(f"Count must be at least 1 — got {n}.")
    if n > MAX_DEPLOY_COUNT:
        raise VMNameError(f"Count must be {MAX_DEPLOY_COUNT} or fewer — got {n}.")
    if n == 1:
        return [stem]

    width = suffix_width(n, start)
    budget = _LIMITS[provider] - (1 + width)          # the "-" plus the digits
    # Truncate the BASE, then strip a separator the cut may have left dangling, so
    # "web-server-" + "-01" can't become "web-server--01".
    stem = stem[:budget].rstrip("-.")
    if len(stem) < MIN_BASE_CHARS:
        raise VMNameError(
            f"Base name {base!r} leaves no room for a numbered suffix within "
            f"{provider.upper()}'s {_LIMITS[provider]}-character limit — "
            f"use {budget} characters or fewer."
        )
    return [f"{stem}-{i:0{width}d}" for i in range(start, start + n)]


def duplicates(names: Sequence[str]) -> List[str]:
    """Names repeated within one list, compared case-insensitively.

    ``expand`` cannot produce one, but the AWS/Azure multi-select bulk modals take
    hand-typed per-VM names, and that is where an operator types the same one twice."""
    seen: Set[str] = set()
    dupes: Set[str] = set()
    for name in names:
        key = (name or "").strip().casefold()
        if not key:
            continue
        if key in seen:
            dupes.add(name)
        seen.add(key)
    return sorted(dupes)


def collisions(names: Sequence[str], taken: Iterable[str]) -> List[str]:
    """Names already claimed elsewhere, compared case-insensitively.

    ``taken`` comes from inventory_service.live_or_pending_vm_names, which is already
    casefolded. Case-insensitive because EC2 Name tags are case-sensitive, Azure
    resource names are not, and GCE names are lowercase-only — the insensitive
    comparison is the only one safe for all three, and erring toward a false positive
    is the right direction when the alternative is an ambiguous destroy."""
    claimed = {(t or "").strip().casefold() for t in taken}
    return sorted({n for n in names if (n or "").strip().casefold() in claimed})
