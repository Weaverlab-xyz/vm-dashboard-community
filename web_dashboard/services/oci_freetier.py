"""
OCI Always-Free tier guardrail — a pure, dependency-free evaluator.

The OCI deploy form defaults to Always-Free compute; this module encodes the
free-tier envelope and decides whether a given deploy selection stays inside it.
It is **advisory** (warn-and-allow): the dashboard can't see an OCI tenancy's
cumulative account usage, so this evaluates a single deploy request against the
per-request caps, optionally factoring in the counts of VMs *this dashboard* has
already deployed (passed in by the caller). Going beyond the envelope is allowed
— the API requires an explicit ``acknowledge_charges`` acknowledgment and the
form surfaces the warnings (see api/oci.py, templates/oci/index.html).

Always-Free envelope (OCI, 2026 — the Ampere A1 allocation was halved this year
from 4 OCPU / 24 GB to the 1,500 OCPU-hour + 9,000 GB-hour monthly budget below):

  • AMD x86 compute — VM.Standard.E2.1.Micro (1/8 OCPU, 1 GB), up to 2 instances.
  • Ampere A1 (Arm) compute — VM.Standard.A1.Flex, 1,500 OCPU-hours + 9,000
    GB-hours/month ≈ 2 OCPUs + 12 GB running continuously.
  • Block storage — 200 GB total (boot + block volumes); OCI boot volumes are a
    50 GB minimum, so 2 free VMs at the 50 GB default fit.

Kept import-free so it is trivially unit-testable without the oci SDK.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

# ── Free-tier constants ───────────────────────────────────────────────────────
FREE_AMD_SHAPE = "VM.Standard.E2.1.Micro"
FREE_A1_SHAPE = "VM.Standard.A1.Flex"
FREE_AMD_MAX_INSTANCES = 2
FREE_A1_MAX_OCPUS = 2            # 1,500 OCPU-hours/month ≈ 2 OCPUs sustained
FREE_A1_MAX_MEMORY_GB = 12      # 9,000 GB-hours/month ≈ 12 GB sustained
FREE_BLOCK_STORAGE_GB = 200     # total boot + block, account-wide
FREE_BOOT_VOLUME_GB = 50        # OCI boot-volume minimum = the free default


def free_tier_catalog() -> dict:
    """Machine-readable free-tier envelope for the deploy form (network-options).
    The form uses this to render shape badges, default within-budget A1 sliders,
    and the free-vs-paid hint text."""
    return {
        "amd_shape": FREE_AMD_SHAPE,
        "amd_max_instances": FREE_AMD_MAX_INSTANCES,
        "a1_shape": FREE_A1_SHAPE,
        "a1_max_ocpus": FREE_A1_MAX_OCPUS,
        "a1_max_memory_gb": FREE_A1_MAX_MEMORY_GB,
        "block_storage_gb": FREE_BLOCK_STORAGE_GB,
        "boot_volume_gb": FREE_BOOT_VOLUME_GB,
        "free_shapes": [FREE_AMD_SHAPE, FREE_A1_SHAPE],
    }


def is_free_shape(shape: str) -> bool:
    return (shape or "").strip() in (FREE_AMD_SHAPE, FREE_A1_SHAPE)


def evaluate(
    *,
    shape: str,
    ocpus: Optional[float] = None,
    memory_gb: Optional[float] = None,
    boot_volume_gb: Optional[int] = None,
    instance_count: int = 1,
    existing_amd_count: int = 0,
    existing_a1_ocpus: float = 0.0,
    existing_a1_memory_gb: float = 0.0,
) -> Tuple[bool, List[str]]:
    """Evaluate one deploy selection against the Always-Free envelope.

    Returns ``(within_free_tier, warnings)``. ``within_free_tier`` is True only
    when ``warnings`` is empty. ``existing_*`` let the caller fold in what this
    dashboard has already deployed so the "you'd exceed the free count/budget"
    cases are caught; they default to 0 (evaluate the request in isolation).

    Advisory only — a non-empty warning list does not block the deploy, it drives
    the warn-and-confirm gate.
    """
    warnings: List[str] = []
    shape = (shape or "").strip()
    count = max(int(instance_count or 1), 1)
    is_amd = shape == FREE_AMD_SHAPE
    is_a1 = shape == FREE_A1_SHAPE

    if not (is_amd or is_a1):
        warnings.append(
            f"Shape '{shape}' is not an Always-Free shape (free tier covers "
            f"{FREE_AMD_SHAPE} and {FREE_A1_SHAPE}) — it may incur charges."
        )

    if is_amd:
        total = existing_amd_count + count
        if total > FREE_AMD_MAX_INSTANCES:
            warnings.append(
                f"This brings dashboard-deployed {FREE_AMD_SHAPE} instances to "
                f"{total}; the free tier covers {FREE_AMD_MAX_INSTANCES}. "
                "Additional AMD micro instances may incur charges."
            )

    if is_a1:
        total_ocpus = existing_a1_ocpus + (ocpus or 0) * count
        total_mem = existing_a1_memory_gb + (memory_gb or 0) * count
        if total_ocpus > FREE_A1_MAX_OCPUS:
            warnings.append(
                f"Ampere A1 OCPUs would total {total_ocpus:g}; the free monthly "
                f"budget sustains ~{FREE_A1_MAX_OCPUS} OCPUs. Beyond that may incur charges."
            )
        if total_mem > FREE_A1_MAX_MEMORY_GB:
            warnings.append(
                f"Ampere A1 memory would total {total_mem:g} GB; the free monthly "
                f"budget sustains ~{FREE_A1_MAX_MEMORY_GB} GB. Beyond that may incur charges."
            )

    # Block storage: any boot volume beyond the 50 GB free minimum eats the shared
    # 200 GB pool. Flag a single request that on its own exceeds the whole pool.
    bv = int(boot_volume_gb or FREE_BOOT_VOLUME_GB)
    if bv * count > FREE_BLOCK_STORAGE_GB:
        warnings.append(
            f"Requested boot storage ({bv} GB × {count}) exceeds the {FREE_BLOCK_STORAGE_GB} GB "
            "free block-storage allotment and may incur charges."
        )
    elif bv > FREE_BOOT_VOLUME_GB:
        warnings.append(
            f"Boot volume {bv} GB is above the {FREE_BOOT_VOLUME_GB} GB free default and draws "
            f"down the shared {FREE_BLOCK_STORAGE_GB} GB free block-storage pool."
        )

    return (not warnings), warnings
