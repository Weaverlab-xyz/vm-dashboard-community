"""Read and write the migration bundle — the reviewable artifact in the middle.

The bundle exists so the migration is not a black box: it is a plain JSON file
an operator opens, reads, and edits before anything is written to the target.
That is also why it carries ``excluded`` and ``annotations`` alongside the
config itself — "what didn't come across, and why" is the half of a migration
that usually goes unrecorded.

It holds live credentials, so it is written mode 0600 **at creation** rather
than chmod'ed afterwards, and it lives outside the repo by default — the same
treatment ``scripts/sandbox/Linux/lib/common.sh`` already gives its per-cloud
``config.json``.
"""
from __future__ import annotations

import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from . import classify

SCHEMA_VERSION = 1

#: Default home for bundles: outside the repo, mirroring ~/.dashboard-sandbox/.
DEFAULT_DIR = Path.home() / ".dashboard-migrate"


def default_path(label: str = "bundle") -> Path:
    """A timestamped path under :data:`DEFAULT_DIR`."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return DEFAULT_DIR / f"{label}-{stamp}.json"


def _git_commit() -> str:
    """Best-effort commit of the tree this ran from — provenance for the bundle.

    Shells out rather than importing anything; returns "" outside a checkout.
    """
    import subprocess
    root = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def build(*, source_url: str, method: str, config: dict, excluded: dict,
          regions: dict, endpoints: list | None = None) -> dict:
    """Assemble the bundle document. Pure — no I/O, so it unit-tests directly."""
    endpoints = endpoints or []
    annotations = {k: classify.annotate(k, v) for k, v in config.items()}
    return {
        "schema": SCHEMA_VERSION,
        "meta": {
            "source_url": source_url,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "export_method": method,
            "counts": {
                "portable": len(config),
                "excluded": len(excluded),
                "masked": sum(1 for a in annotations.values() if a["masked"]),
                "vault_ref": sum(1 for a in annotations.values() if a["vault_ref"]),
                "regions": sum(len(v) for v in regions.values()),
                "endpoints": len(endpoints),
            },
        },
        "config": dict(sorted(config.items())),
        "regions": regions,
        "notification_endpoints": endpoints,
        "excluded": dict(sorted(excluded.items())),
        "annotations": annotations,
    }


def write(doc: dict, path: Path) -> Path:
    """Serialise ``doc`` to ``path`` at mode 0600, creating parents.

    ``os.open`` with the mode baked in rather than ``chmod`` afterwards: a
    ``write``-then-``chmod`` leaves the file world-readable for the window in
    between, which is exactly long enough on a shared host.

    Windows ignores the POSIX mode. The PowerShell launcher compensates by
    defaulting the bundle under ``$env:LOCALAPPDATA``; :func:`permission_warning`
    reports when the mode did not take.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
    return path


def read(path: Path) -> dict:
    """Load a bundle, rejecting a schema this build does not understand."""
    with open(Path(path), encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not a JSON object")
    schema = doc.get("schema")
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: bundle schema {schema!r}, this tool speaks {SCHEMA_VERSION}")
    doc.setdefault("config", {})
    doc.setdefault("regions", {})
    doc.setdefault("notification_endpoints", [])
    return doc


def permission_warning(path: Path) -> str:
    """A warning when the bundle's permissions are not what we asked for, else "".

    Windows has no POSIX mode, so say so plainly rather than implying the file
    is protected when it is not.
    """
    if platform.system() == "Windows":
        # ASCII only: this is the one message a legacy cp1252 console must be
        # able to render, because it is the warning about a credentials file.
        return (f"{path} holds live credentials. Windows ignores the 0600 mode - "
                f"keep it out of shared or synced folders and delete it after cutover.")
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return ""
    if mode & 0o077:
        return f"{path} is mode {mode:04o}, expected 0600 — tighten it before sharing this host."
    return ""


def scan_report(path: Path) -> list[dict]:
    """Run the repo's own secret scanner over the bundle.

    Reuses ``services/secret_scan.py`` rather than reimplementing detection, so
    the "this file is dangerous" message is calibrated the same way the Config
    Management asset scan is. Fails soft: the scanner is a nicety, not a gate.
    """
    try:
        from ...services.secret_scan import scan_text
    except Exception:  # noqa: BLE001 - never block an export on the scanner
        return []
    try:
        return scan_text(Path(path).read_text(encoding="utf-8"), str(path))
    except Exception:  # noqa: BLE001
        return []
