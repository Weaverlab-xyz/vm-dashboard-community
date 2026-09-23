"""One vocabulary for tags, labels, attributes and categories — and who owns each one.

Every platform in this estate names the same idea differently and hands it to us in a
different SHAPE. AWS and Azure call them tags and send a dict. GCP calls them labels and
sends a dict with stricter rules. OCI calls them freeform tags. Proxmox calls them tags
and sends a semicolon-joined string. The hypervisor cache stores a JSON list. Password
Safe calls them attributes and PRA calls them a tag.

This module is the single place that flattens all of that into one shape a page can
render, and — more importantly — the single place that says **which keys are not the
operator's to change**.

Pure policy: stdlib only, no cloud SDK, no Session, no clock, so it is unit-testable on
dicts by file path. Same split as ``unmanaged_vms``, ``expiry_policy`` and
``vm_suspend_policy``: the per-cloud listing calls live in each ``*_service``, this
decides what their tags MEAN.

**Three authority classes, because colour has to carry information.**

  * ``system``   — the dashboard wrote it and something load-bearing selects on it.
  * ``identity`` — ``workgroup``, which decides who can SEE the resource.
  * ``user``     — everything else. The operator's own, and the only class Phase 2 edits.

Rendered grey-with-a-lock, indigo, and a per-key colour respectively. A ``user`` chip's
colour comes from :func:`tone_of`, a stable hash of the key, so ``env`` is the same colour
on ``/aws``, ``/proxmox`` and ``/inventory``. That hash is computed HERE rather than in
JavaScript so there is one implementation to test and the palette cannot drift between
pages — ``static/js/app.js`` is a pure lookup table over :data:`TONE_COUNT`.

**Why the protected set is spelled out here rather than imported.** Five modules already
own pieces of it (``cost_service``, ``unmanaged_vms``, ``pov_cloud_env``,
``vdesktop_service``, ``ephemeral_secrets``) and each knows only its own. Importing them
would make this module depend on half the service layer — including ones that pull cloud
SDKs at import time — and would still miss the keys that live in Terraform. So the list is
restated, and ``tests/test_tag_policy.py`` scans those modules to prove nothing was missed.

Matching is CASE-INSENSITIVE on the key, because the estate genuinely contains both
``purpose`` and ``Purpose``, both ``workgroup`` and ``Workgroup``, and — after a GCP label
round trip — both ``managed-by`` and ``managed_by``.
"""
import zlib
from typing import Optional

CLASS_SYSTEM = "system"
CLASS_IDENTITY = "identity"
CLASS_USER = "user"

# Ordering weight for the three classes. System first so the chips that explain what the
# dashboard is doing to a resource read before the operator's own, and identity second
# because it is the one an operator most often needs to check.
_CLASS_ORDER = {CLASS_SYSTEM: 0, CLASS_IDENTITY: 1, CLASS_USER: 2}

# How many distinct colours a `user` chip can take. Must match the length of
# `TAG_TONES` in static/js/app.js — tests/test_tag_policy.py pins the two together.
TONE_COUNT = 6


class TagPolicyError(Exception):
    """Raised when a write is attempted on a key that is not the operator's to change."""


# ── the protected set ────────────────────────────────────────────────────────

# key (lowercased) -> why it is load-bearing, phrased for an operator who just tried to
# edit it. The reason is the whole point: a bare "forbidden" teaches nobody why their
# estate broke the last time somebody retagged a VM by hand in the console.
PROTECTED_KEYS = {
    # cost_service._MANAGED_TAG_KEY, unmanaged_vms.MANAGED_TAGS. Three spellings: the
    # canonical one, the legacy Azure one from before #194, and the underscore form a GCP
    # label round trip produces (cost_service._GCP_LABEL_KEYS records why that is
    # permanent rather than a migration shim).
    "managed-by": "it is how /costs attributes this resource and how the dashboard "
                  "tells its own VMs from discovered ones",
    "managed_by": "it is the GCP label spelling of managed-by, which /costs sums",
    "managedby": "it is the legacy spelling of managed-by, still honoured by "
                 "unmanaged-VM discovery",
    # unmanaged_vms.WORKGROUP_TAG_KEYS. Editing this hands the VM to another team or
    # hides it from its own — see tests/test_workgroup_resource_tagging.py.
    "workgroup": "it decides who can see this resource; use the reassign action, which "
                 "validates the workgroup exists and refuses to hide a resource from you",
    # pov_cloud_env.TAG_ENVIRONMENT / TAG_MANAGED_BY / TAG_ROLE. The environment tag is
    # what teardown and the reconcile sweep SELECT on: change it and the resource keeps
    # billing with nothing able to find it.
    "povenvironment": "POV teardown and the reconcile sweep select on it; a resource "
                      "with the wrong value keeps running and nothing can find it",
    "povmanagedby": "it marks this as POV-owned, which teardown filters on",
    "povrole": "the POV environment's wiring depends on it",
    # vdesktop_service.POOL_TAG, in both its AWS/Azure and GCP-legal spellings.
    "dashboard:desktop_pool": "it is how a virtual-desktop pool finds its seats",
    "dashboard_desktop_pool": "it is how a virtual-desktop pool finds its seats",
    # cloud_database_service / k8s_service:541 — identity on teardown.
    "clouddb-id": "it is this database's identity on teardown",
    "k8s-cluster-id": "it is this cluster's identity on teardown",
    # ephemeral_secrets.TAG_KEY — the sweeper that removes ephemeral accounts.
    "vm-dashboard-ephemeral": "the ephemeral-account sweeper selects on it",
    # The row's own display name on AWS and OCI. Renaming via the tag editor would change
    # the name in the table without changing anything else that refers to it.
    "name": "it is this resource's display name; rename it where it was created",
}

# Load-bearing, so `system`, but NOT in PROTECTED_KEYS — the workgroup tag has its own
# purpose-built route (api/aws.py reassign_instance_workgroup and its three siblings) that
# validates against the workgroup table. `assert_editable` still refuses it here; the
# split exists so the refusal can name the route instead of just saying no.
_IDENTITY_KEYS = {"workgroup"}


def _canon(key) -> str:
    """The form PROTECTED_KEYS is keyed on: stripped and lowercased.

    Lowercasing alone is enough, and separator folding would be wrong. The three
    spellings of managed-by are listed individually above rather than folded to one,
    because ``dashboard:desktop_pool`` and ``dashboard_desktop_pool`` are also both real
    and folding separators would merge keys the clouds treat as distinct.
    """
    return str(key or "").strip().lower()


def classify(key) -> str:
    """Which authority class ``key`` belongs to."""
    k = _canon(key)
    if k in _IDENTITY_KEYS:
        return CLASS_IDENTITY
    if k in PROTECTED_KEYS:
        return CLASS_SYSTEM
    return CLASS_USER


def tone_of(key) -> Optional[int]:
    """Palette index for a ``user`` chip, or ``None`` for system/identity ones.

    ``zlib.crc32`` rather than ``hash()``: Python randomises string hashing per process
    (PYTHONHASHSEED), so ``hash()`` would give one worker a different palette from the
    next and the same tag would change colour on refresh.
    """
    if classify(key) != CLASS_USER:
        return None
    return zlib.crc32(_canon(key).encode("utf-8")) % TONE_COUNT


# ── the vocabulary each platform uses ────────────────────────────────────────

# Shown in a chip's tooltip. Small thing, but it is the answer to "does this product call
# it a tag, a label or an attribute" at the exact moment somebody is looking at one.
_NOUN = {
    "aws": "AWS tag",
    "azure": "Azure tag",
    "gcp": "GCP label",
    "oci": "OCI freeform tag",
    "proxmox": "Proxmox tag",
    "vsphere": "vSphere tag",
    "nutanix": "Nutanix category",
    "xcpng": "XCP-ng tag",
    "hyperv": "Hyper-V tag",
    "pra": "PRA tag",
    "passwordsafe": "Password Safe attribute",
}


def noun(source) -> str:
    """What ``source`` calls a tag, or the generic word when it is unknown."""
    return _NOUN.get(str(source or "").strip().lower(), "tag")


# ── normalisation ────────────────────────────────────────────────────────────

def _pairs(raw):
    """``raw`` in any shape the estate produces, as ``(key, value)`` with value possibly
    ``None``.

    ``None`` is not the empty string: a Proxmox tag or a vSphere category is a bare label
    with no value at all, and rendering ``prod=`` for it would invent a value the
    hypervisor never had.
    """
    if not raw:
        return []
    if isinstance(raw, dict):
        return [(k, v) for k, v in raw.items()]
    if isinstance(raw, str):
        # Proxmox joins with ';'. Commas appear in hand-typed values often enough that
        # splitting on them too would corrupt more than it fixed, so only ';'.
        return [(part.strip(), None) for part in raw.split(";") if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        out = []
        for item in raw:
            if isinstance(item, dict):
                # A boto3-style [{"Key":..,"Value":..}] list, in case a caller hands us
                # one straight off an SDK response rather than the flattened dict.
                key = item.get("Key", item.get("key"))
                if key:
                    out.append((key, item.get("Value", item.get("value"))))
            elif item is not None and str(item).strip():
                out.append((str(item).strip(), None))
        return out
    return []


def normalise(raw, source: str = "") -> list:
    """``raw`` as a sorted list of render-ready chips.

    Each chip is ``{key, value, cls, tone, title}``. ``value`` is ``None`` for a tag that
    genuinely has none. Sorted system, then identity, then user alphabetically — a stable
    order, so a row's chips do not reshuffle between polls of the same list.
    """
    word = noun(source)
    chips = []
    seen = set()
    for key, value in _pairs(raw):
        k = str(key or "").strip()
        if not k:
            continue
        canon = _canon(k)
        if canon in seen:
            # A dict cannot collide, but `managed-by` and `ManagedBy` on the same OCI
            # instance can, and rendering both would suggest two different facts.
            continue
        seen.add(canon)
        cls = classify(k)
        reason = PROTECTED_KEYS.get(canon, "")
        chips.append({
            "key": k,
            "value": None if value is None else str(value),
            "cls": cls,
            "tone": tone_of(k),
            "title": f"{word} — {reason}" if reason else word,
        })
    chips.sort(key=lambda c: (_CLASS_ORDER[c["cls"]], c["key"].lower()))
    return chips


def keys_of(chips) -> list:
    """The distinct keys across a list of already-normalised chips, sorted.

    For the Inventory page's tag filter, which offers keys rather than key=value pairs:
    an estate has a handful of keys and thousands of values.
    """
    return sorted({c.get("key", "") for c in (chips or []) if c.get("key")},
                  key=str.lower)


# ── the Phase 2 guard ────────────────────────────────────────────────────────

def assert_editable(keys) -> None:
    """Refuse a write touching any key the dashboard depends on.

    Called in the API layer BEFORE any cloud call, never merely reflected in the UI. The
    distinction is the same one ``unmanaged_vms.assert_not_unmanaged`` draws: hiding a
    control is a promise about one page, refusing the path is a guarantee about every
    caller — and this dashboard has an MCP server and a REST API that are not that page.

    The message names the key AND why it is protected, because the operator reading it is
    about to go and do it by hand in the cloud console instead, and should know what they
    are about to break.
    """
    for key in (keys or []):
        canon = _canon(key)
        reason = PROTECTED_KEYS.get(canon)
        if reason:
            raise TagPolicyError(f"'{key}' cannot be edited here: {reason}")
