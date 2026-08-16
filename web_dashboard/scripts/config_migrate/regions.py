"""Carry per-region config sets across without deleting the target's own.

The subtlety here is worth spelling out, because the obvious approach is wrong
in a way nothing reports.

``<cloud>_region_configs`` is a single JSON blob holding every region's resource
set. It *does* survive a round-trip through ``POST /api/setup/import`` —
``_region_config_cloud`` requires a trailing dot, so the flat key fails the
prefix test and falls through to a verbatim ``set_many``. Mechanically fine.

But that write is **replace-on-save**, and the target already has region config
of its own (the sandbox seeds it). Shipping the blob would delete every region
entry that exists only on the target, silently, with a success response.

The dotted form ``<cloud>_region.<region>.<field>`` takes a different path in
the importer: it routes to ``region_config.merge_region_fields``, which merges
field-by-field and never clobbers. So the exporter explodes the blob and ships
dotted keys.

Field names come from ``region_config.region_fields()`` at runtime rather than
a copy. The importer validates each dotted key against that cloud's pydantic
model and **silently drops** what it doesn't recognise — the same failure mode
``tests/test_sandbox_region_keys.py`` was written to catch for the sandbox
scripts. A hardcoded field list here would reintroduce it.
"""
from __future__ import annotations

import json

from ...services.region_config import REGION_CONFIG_CLOUDS, configs_key, region_fields

#: ``{cloud: "<cloud>_region_configs"}`` — the flat keys this module consumes.
BLOB_KEYS = {cloud: configs_key(cloud) for cloud in REGION_CONFIG_CLOUDS}

#: Reverse lookup, for spotting a blob key in an arbitrary config map.
_CLOUD_BY_BLOB_KEY = {v: k for k, v in BLOB_KEYS.items()}


def _parse_blob(raw: object) -> dict[str, dict]:
    """Parse one ``<cloud>_region_configs`` value into ``{region: {field: val}}``.

    Never raises. A malformed blob yields ``{}`` — matching
    ``region_config.load_region_configs``, which logs and falls back to the flat
    keys rather than failing the request.
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(region): fields
            for region, fields in data.items()
            if region and isinstance(fields, dict)}


def extract(config: dict) -> tuple[dict, dict[str, dict[str, dict]], list[str]]:
    """Pull the region blobs out of a flat config map.

    Returns ``(config_without_blobs, {cloud: {region: {field: value}}}, malformed)``.
    The input is not mutated. Regions are left exactly as stored — normalisation
    is the importer's job (``merge_region_fields`` calls
    ``region_catalog.normalize``), and doing it twice risks two spellings of one
    region.

    ``malformed`` names any blob that would not parse. Such a blob is *already*
    broken on the source — ``load_region_configs`` logs and falls back to the
    flat keys — so it is not carried across. But it is reported rather than
    dropped quietly, since "your region config never worked" is the sort of
    thing an operator should learn during a migration rather than after one.
    """
    remaining: dict = {}
    regions: dict[str, dict[str, dict]] = {}
    malformed: list[str] = []
    for key, value in (config or {}).items():
        cloud = _CLOUD_BY_BLOB_KEY.get(key)
        if cloud is None:
            remaining[key] = value
            continue
        parsed = _parse_blob(value)
        if parsed:
            regions[cloud] = parsed
        elif str(value or "").strip() not in ("", "{}"):
            malformed.append(key)
    return remaining, regions, malformed


def to_import_keys(regions: dict[str, dict[str, dict]]) -> tuple[dict, list[str]]:
    """Flatten ``{cloud: {region: {field: val}}}`` to dotted importer keys.

    Returns ``(keys, dropped)``. ``keys`` maps
    ``"<cloud>_region.<region>.<field>"`` → value for every field the importer
    will actually accept; ``dropped`` lists the ones it would have discarded, so
    the caller can say so out loud instead of letting them vanish.

    Blank values are omitted: ``merge_region_fields`` skips them anyway, and
    carrying them would pad the diff with no-ops.
    """
    keys: dict = {}
    dropped: list[str] = []
    for cloud, by_region in (regions or {}).items():
        if cloud not in REGION_CONFIG_CLOUDS:
            dropped.extend(f"{cloud}_region.{r}.{f}"
                           for r, fields in (by_region or {}).items()
                           for f in fields)
            continue
        known = set(region_fields(cloud))
        for region, fields in (by_region or {}).items():
            for field, value in (fields or {}).items():
                dotted = f"{cloud}_region.{region}.{field}"
                if field not in known:
                    dropped.append(dotted)
                    continue
                sval = "" if value is None else str(value).strip()
                if sval:
                    keys[dotted] = sval
    return keys, dropped


def to_replace_keys(regions: dict[str, dict[str, dict]]) -> dict:
    """Flatten back to the ``<cloud>_region_configs`` blob form.

    This is ``--regions replace``: mirror the source exactly, dropping any
    region the target has and the source doesn't. Only for deliberate
    cloning — the merge path above is the default for a reason.
    """
    out: dict = {}
    for cloud, by_region in (regions or {}).items():
        if cloud not in REGION_CONFIG_CLOUDS:
            continue
        known = region_fields(cloud)
        cleaned: dict[str, dict] = {}
        for region, fields in (by_region or {}).items():
            entry = {f: str(fields[f]).strip()
                     for f in known
                     if fields.get(f) is not None and str(fields.get(f)).strip()}
            if entry:
                cleaned[region] = entry
        out[BLOB_KEYS[cloud]] = json.dumps(cleaned, sort_keys=True)
    return out


def summarize(regions: dict[str, dict[str, dict]]) -> str:
    """One-line human summary, e.g. ``azure: eastus2, westus2; aws: us-east-2``."""
    parts = [f"{cloud}: {', '.join(sorted(by_region))}"
             for cloud, by_region in sorted((regions or {}).items()) if by_region]
    return "; ".join(parts) or "none"
