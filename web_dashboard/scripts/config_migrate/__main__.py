"""CLI for the config migration tool.

Usage:
  python -m web_dashboard.scripts.config_migrate export  --source URL  [--out PATH]
  python -m web_dashboard.scripts.config_migrate diff    --bundle PATH --target URL
  python -m web_dashboard.scripts.config_migrate import  --bundle PATH --target URL --apply
  python -m web_dashboard.scripts.config_migrate export-local          (inside the container)

Most operators reach this through ``scripts/migrate-config.sh`` or
``scripts/Migrate-Config.ps1``; both are thin launchers over this module.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from . import bundle as bundle_mod
from . import classify, regions

# ── Output helpers ───────────────────────────────────────────────────────────
# Mirrors scripts/sandbox/Linux/lib/common.sh: everything conversational goes to
# stderr so `... export-local` can put JSON on stdout and stay pipeable.

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _unicode_ok() -> bool:
    """Whether the console can render the tick/cross this tool would like to use.

    A legacy Windows console is cp1252, and PowerShell runs this Python natively
    — so the glyphs either mojibake into `?` or raise UnicodeEncodeError mid-run.
    ``scripts/sandbox/Windows/lib/Common.ps1`` hit the same wall and works around
    it too. Probing the stream's own encoding beats guessing from the platform,
    because a modern Windows Terminal handles them fine.
    """
    try:
        "✓✗──".encode(sys.stderr.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_GLYPHS = ({"ok": "✓", "bad": "✗", "rule": "──", "secret": "‹secret›"} if _unicode_ok()
           else {"ok": "OK", "bad": "X", "rule": "--", "secret": "<secret>"})


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:    print("  " + msg, file=sys.stderr)
def ok(msg: str) -> None:      print(_c("0;32", _GLYPHS["ok"] + " ") + msg, file=sys.stderr)
def warn(msg: str) -> None:    print(_c("0;33", "! ") + msg, file=sys.stderr)
def err(msg: str) -> None:     print(_c("0;31", _GLYPHS["bad"] + " ") + msg, file=sys.stderr)
def section(msg: str) -> None: print("\n" + _c("1;35", f"{_GLYPHS['rule']} {msg}"), file=sys.stderr)


def die(msg: str, code: int = 1) -> NoReturn:
    err(msg)
    raise SystemExit(code)


# ── Shared argument plumbing ─────────────────────────────────────────────────

def _add_conn_args(p: argparse.ArgumentParser, which: str) -> None:
    p.add_argument(f"--{which}", required=True, metavar="URL",
                   help=f"{which.capitalize()} dashboard base URL, e.g. https://dash.example.com")
    p.add_argument("--admin-user", default=os.environ.get("DASHBOARD_ADMIN_USER", ""),
                   help="Admin username (env: DASHBOARD_ADMIN_USER)")
    p.add_argument("--admin-password", default=os.environ.get("DASHBOARD_ADMIN_PASSWORD", ""),
                   help="Admin password (env: DASHBOARD_ADMIN_PASSWORD; prompted if omitted)")
    p.add_argument("--token", default="", help="Existing admin JWT, instead of logging in")
    p.add_argument("--ca-bundle", default=os.environ.get("DASHBOARD_CA_BUNDLE", ""),
                   help="PEM bundle for a TLS-inspecting corporate proxy")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification (last resort)")
    p.add_argument("--timeout", type=int, default=30, help="Per-request timeout, seconds")


def _connect(url: str, args: argparse.Namespace):
    """Build an authenticated client, prompting for the password if needed."""
    from .client import ApiError, Client
    if args.insecure:
        warn("TLS verification is off. Use --ca-bundle instead wherever you can.")
    client = Client(url, token=args.token, ca_bundle=args.ca_bundle,
                    insecure=args.insecure, timeout=args.timeout)
    try:
        status = client.setup_status()
    except ApiError as exc:
        die(f"Cannot reach {url}: {exc.detail}")
    if not status.get("complete"):
        warn(f"{url} reports setup is NOT complete — this looks like a fresh stack.")
    if client.token:
        return client
    user = args.admin_user or input(f"Admin username for {url}: ").strip()
    password = args.admin_password or getpass.getpass(f"Admin password for {user}: ")
    if not user or not password:
        die("An admin username and password are required (or pass --token).")
    try:
        client.login(user, password)
    except ApiError as exc:
        die(f"Login failed against {url}: {exc.detail}")
    return client


def _only_filter(config: dict, prefixes: list[str]) -> dict:
    """Restrict a config map to keys starting with any of ``prefixes``.

    Exists so a real cutover can be taken in tranches — flat keys first, verify
    the UI, then regions, then secrets — rather than as one irreversible push.
    """
    if not prefixes:
        return config
    return {k: v for k, v in config.items() if any(k.startswith(p) for p in prefixes)}


# ── export ───────────────────────────────────────────────────────────────────

def _export_via_docker(compose_file: str, service: str) -> dict:
    """Run ``export-local`` inside the running container and parse its stdout.

    This is the only way to recover the four keys ``get_all_public`` masks, and
    the only way to read notification endpoint URLs at all — the API never
    returns either.
    """
    cmd = ["docker", "compose"]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["exec", "-T", service, "python", "-m",
            "web_dashboard.scripts.config_migrate", "export-local"]
    info(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        die("docker not found on PATH. On Windows run this from WSL, or use --via http.")
    if proc.returncode != 0:
        die(f"docker compose exec failed (rc={proc.returncode}):\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        die(f"export-local did not return JSON:\n{proc.stdout[:400]}")
    return {}


def cmd_export(args: argparse.Namespace) -> int:
    section(f"Exporting from {args.source}")

    endpoints: list = []
    if args.via == "docker":
        raw = _export_via_docker(args.compose_file, args.service)
        config = raw.get("config", {})
        endpoints = raw.get("notification_endpoints", [])
        method = "docker"
        ok(f"Read {len(config)} keys and {len(endpoints)} notification endpoints from the container.")
    else:
        client = _connect(args.source, args)
        from .client import ApiError
        try:
            config = client.get_config()
        except ApiError as exc:
            die(f"Could not read config: {exc.detail}")
        method = "http"
        ok(f"Read {len(config)} keys over HTTP.")

    portable, excluded = classify.partition(config, include_on_prem=args.include_on_prem)
    portable, region_map, malformed = regions.extract(portable)

    doc = bundle_mod.build(source_url=args.source, method=method, config=portable,
                           excluded=excluded, regions=region_map, endpoints=endpoints)

    out = Path(args.out) if args.out else bundle_mod.default_path()
    bundle_mod.write(doc, out)

    section("Bundle")
    counts = doc["meta"]["counts"]
    info(f"{counts['portable']} portable keys, {counts['excluded']} held back, "
         f"{counts['vault_ref']} vault references")
    info(f"regions: {regions.summarize(region_map)}")
    if malformed:
        warn(f"{', '.join(malformed)} is not parseable JSON on the source — it is "
             f"already being ignored there (flat keys are winning) and was not carried.")
    if counts["masked"]:
        warn(f"{counts['masked']} value(s) came back redacted and were held back. "
             f"Re-run with --via docker to capture them, or set them by hand on the target.")
    _report_excluded(excluded)

    findings = bundle_mod.scan_report(out)
    if findings:
        warn(f"{len(findings)} literal secret(s) in this file — it is mode 0600, "
             f"keep it off shared storage and delete it after cutover.")
    pw = bundle_mod.permission_warning(out)
    if pw:
        warn(pw)
    ok(f"Wrote {out}")
    info("Next: review it, then run `diff` against the target before applying.")
    return 0


def _report_excluded(excluded: dict) -> None:
    """Say what was held back and why. A migration's unrecorded half."""
    if not excluded:
        return
    by_reason: dict[str, list[str]] = {}
    for key, reason in excluded.items():
        by_reason.setdefault(reason, []).append(key)
    labels = {
        classify.INSTANCE_LOCAL: "instance identity (origin, proxy, listener, database)",
        classify.RUNTIME_HANDLE: "handles on live resources the source provisioned",
        classify.LOCAL_PATH:     "paths on the source host's filesystem",
        classify.ON_PREM:        "on-premises targets the destination cannot reach (--include-on-prem to carry them)",
        classify.MASKED:         "redacted by the API (use --via docker)",
    }
    for reason, keys in sorted(by_reason.items()):
        info(f"held back — {labels.get(reason, reason)}: {len(keys)}")
        info("    " + ", ".join(sorted(keys)[:8]) + (" …" if len(keys) > 8 else ""))


# ── export-local (runs inside the container) ─────────────────────────────────

def cmd_export_local(args: argparse.Namespace) -> int:
    """Dump decrypted config + notification endpoints as JSON on stdout.

    Runs in-process against the application database, so it decrypts with the
    instance's own Fernet key and sees everything — including the four keys the
    HTTP API masks and the webhook URLs it never returns. Imports are inside the
    function so the other subcommands stay free of sqlalchemy.
    """
    from ...database import AppConfig, NotificationEndpoint, SessionLocal
    from ...services import config_service

    db = SessionLocal()
    try:
        config = {
            row.key: config_service.decrypt_value(row.value or "")
            for row in db.query(AppConfig).order_by(AppConfig.key).all()
            # Community installs leave workgroup NULL on every row; a scoped row
            # belongs to a multi-tenant deployment and is not ours to move.
            if getattr(row, "workgroup", None) is None
        }
        endpoints = [
            {
                "name":        ep.name,
                "url":         config_service.decrypt_value(ep.url or ""),
                "fmt":         ep.fmt or "custom",
                "secret":      config_service.decrypt_value(ep.secret or ""),
                "event_types": ep.event_types or "",
                "enabled":     bool(ep.enabled),
            }
            for ep in db.query(NotificationEndpoint).order_by(NotificationEndpoint.name).all()
        ]
    finally:
        db.close()

    json.dump({"config": config, "notification_endpoints": endpoints}, sys.stdout)
    sys.stdout.write("\n")
    return 0


# ── diff / import ────────────────────────────────────────────────────────────

def _payload_from_bundle(doc: dict, args: argparse.Namespace) -> tuple[dict, dict, list]:
    """Build the flat import payload. Returns ``(payload, refused, dropped_region_keys)``.

    Re-runs the exclusion check even though export already did: a bundle is a
    file an operator may have hand-edited, and a denied key reintroduced there
    should still be refused.
    """
    config = dict(doc.get("config", {}))
    region_map = doc.get("regions", {})

    refused: dict = {}
    for key in list(config):
        reason = classify.exclusion_reason(key, config[key],
                                           include_on_prem=args.include_on_prem)
        if reason:
            refused[key] = reason
            config.pop(key)

    if args.regions == "replace":
        region_keys, dropped = regions.to_replace_keys(region_map), []
    else:
        region_keys, dropped = regions.to_import_keys(region_map)

    payload = {**config, **region_keys}
    payload = _only_filter(payload, args.only)
    return payload, refused, dropped


def _preflight_vault_refs(payload: dict) -> list[str]:
    """Named-vault references whose ``secret_vaults`` row must exist on the target.

    ``config_service._parse_ref`` treats the first path segment as a vault id
    only when it matches a registered ``SecretVault``; otherwise it falls back to
    legacy single-vault parsing. So an unregistered vault turns a working
    reference into a silently *wrong* one rather than an error.
    """
    ids = set()
    for value in payload.values():
        if isinstance(value, str) and classify.is_vault_reference(value):
            vault_id = classify.vault_id_of(value)
            if vault_id:
                ids.add(vault_id)
    return sorted(ids)


def _show(key: str, value: object) -> str:
    """Render a value for the diff, redacting anything secret-shaped."""
    if classify.is_secret(key):
        return _GLYPHS["secret"]
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _diff(payload: dict, current: dict) -> dict[str, list]:
    """Bucket the payload against the target's current config."""
    buckets: dict[str, list] = {"ADD": [], "CHANGE": [], "SAME": [], "UNKNOWN": []}
    for key, value in sorted(payload.items()):
        if key.startswith(tuple(f"{c}_region." for c in regions.REGION_CONFIG_CLOUDS)):
            # Region keys don't exist as flat rows on the target, so a
            # side-by-side comparison would call every one of them an ADD.
            # merge_region_fields is idempotent, so listing them is honest.
            buckets["ADD"].append((key, value))
            continue
        if key not in current:
            buckets["ADD"].append((key, value))
        elif classify.is_masked(current[key]):
            buckets["UNKNOWN"].append((key, value))
        elif str(current[key]) == str(value):
            buckets["SAME"].append((key, value))
        else:
            buckets["CHANGE"].append((key, value))
    return buckets


def cmd_diff(args: argparse.Namespace, *, apply: bool = False) -> int:
    doc = bundle_mod.read(Path(args.bundle))
    payload, refused, dropped = _payload_from_bundle(doc, args)
    if not payload:
        die("Nothing to import — the bundle is empty after filtering. Check --only.")

    section(f"{'Importing to' if apply else 'Comparing against'} {args.target}")
    info(f"bundle: {args.bundle} (exported {doc['meta'].get('exported_at', '?')} "
         f"from {doc['meta'].get('source_url', '?')})")

    client = _connect(args.target, args)
    from .client import ApiError
    try:
        current = client.get_config()
    except ApiError as exc:
        die(f"Could not read the target's config: {exc.detail}")

    # Not redundant with the check in _payload_from_bundle, which only runs over
    # the flat config: region fields arrive via regions.to_import_keys and never
    # pass through exclusion_reason, so this is the only guard on that half. A
    # bulleted value reaching the importer lands as the literal bullet string —
    # a key that looks configured in the UI and fails at cloud-call time.
    masked = [k for k, v in payload.items() if classify.is_masked(v)]
    for key in masked:
        payload.pop(key)
    if masked:
        warn(f"Dropped {len(masked)} redacted value(s): {', '.join(sorted(masked))}")

    buckets = _diff(payload, current)

    section("Plan")
    for label in ("ADD", "CHANGE", "UNKNOWN", "SAME"):
        entries = buckets[label]
        if not entries:
            continue
        info(f"{label}: {len(entries)}")
        if label == "SAME":
            continue
        for key, value in entries[:40]:
            info(f"    {key} = {_show(key, value)}")
        if len(entries) > 40:
            info(f"    … {len(entries) - 40} more")
    if buckets["UNKNOWN"]:
        info("UNKNOWN means the target's value is redacted, so no comparison is possible.")

    if dropped:
        warn(f"{len(dropped)} region field(s) the importer would drop — not sent: "
             + ", ".join(dropped[:6]) + (" …" if len(dropped) > 6 else ""))
    if refused:
        info(f"{len(refused)} key(s) in the bundle are not portable and were skipped.")

    vaults = _preflight_vault_refs(payload)
    if vaults:
        warn(f"This config references named vaults: {', '.join(vaults)}. "
             f"Register them on the target's /secrets page first — an unregistered "
             f"vault id resolves to the wrong secret rather than erroring.")

    endpoints = doc.get("notification_endpoints", [])
    if endpoints and not args.only:
        info(f"{len(endpoints)} notification endpoint(s) to reconcile by name.")

    if not apply:
        section("Dry run")
        info("Nothing was written. Re-run with `import … --apply` to apply.")
        return 0

    if not (buckets["ADD"] or buckets["CHANGE"] or buckets["UNKNOWN"]):
        ok("Target already matches the bundle — nothing to write.")
    else:
        section("Applying")
        try:
            resp = client.import_config(payload)
        except ApiError as exc:
            die(f"Import failed: {exc.detail}")
        ok(f"Wrote {len(payload)} keys. Server said: {json.dumps(resp)[:200]}")

    if endpoints and not args.only:
        _sync_endpoints(client, endpoints)

    _print_out_of_scope()
    return 0


def _sync_endpoints(client, endpoints: list) -> None:
    """Create or update notification endpoints, matched by name.

    Matching on name rather than id keeps a re-run idempotent: ids are generated
    per-instance, so comparing them would duplicate every endpoint each time.
    """
    from .client import ApiError
    section("Notification endpoints")
    try:
        existing = {e.get("name"): e.get("id") for e in client.list_endpoints()}
    except ApiError as exc:
        warn(f"Could not list endpoints, skipping: {exc.detail}")
        return
    for ep in endpoints:
        name = ep.get("name") or ep.get("fmt") or "custom"
        if not (ep.get("url") or "").strip():
            warn(f"{name}: no URL in the bundle (export over HTTP cannot read it) — skipped.")
            continue
        try:
            if name in existing:
                client.patch_endpoint(existing[name], ep)
                ok(f"updated {name}")
            else:
                client.create_endpoint(ep)
                ok(f"created {name}")
        except ApiError as exc:
            warn(f"{name}: {exc.detail}")


def _print_out_of_scope() -> None:
    """State what a successful run did *not* do.

    Printed on success on purpose. The dangerous outcome of this tool is an
    operator who believes the target is now a copy of the source; it is a copy
    of the source's Settings, which is a different thing.
    """
    section("Not migrated")
    info("On their own admin pages, not in Settings — move these by hand:")
    info("    users, workgroups, OIDC group mappings, registered images,")
    info("    remote agents, personal access tokens, secret vault registrations")
    info("Cannot be migrated at all:")
    info("    security keys (WebAuthn binds to the origin — re-enrol on the target)")
    info("    personal access tokens (stored hashed) and agent identities (re-enrol)")
    info("Deliberately never migrated:")
    info("    deployed databases, clusters, gateways and desktops — two dashboards")
    info("    holding records for one set of real resources both run the auto-delete")
    info("    sweeper against them, and Terraform state has a single owner.")


# ── Parser ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config_migrate",
        description="Move dashboard Settings configuration between two instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Read a source instance into a bundle")
    _add_conn_args(exp, "source")
    exp.add_argument("--out", default="", metavar="PATH",
                     help="Bundle path (default: ~/.dashboard-migrate/bundle-<ts>.json)")
    exp.add_argument("--via", choices=("http", "docker"), default="http",
                     help="http reads the API; docker execs into the container and "
                          "also recovers redacted values and webhook URLs")
    exp.add_argument("--compose-file", default="", help="Compose file for --via docker")
    exp.add_argument("--service", default="app", help="Compose service for --via docker")
    exp.add_argument("--include-on-prem", action="store_true",
                     help="Also carry on-premises hypervisor / UNC settings")
    exp.set_defaults(func=cmd_export)

    loc = sub.add_parser("export-local",
                         help="Dump decrypted config as JSON (runs inside the container)")
    loc.set_defaults(func=cmd_export_local)

    for name, applying in (("diff", False), ("import", True)):
        p = sub.add_parser(name, help=("Apply a bundle to a target" if applying
                                       else "Show what an import would change"))
        p.add_argument("--bundle", required=True, metavar="PATH")
        _add_conn_args(p, "target")
        p.add_argument("--only", action="append", default=[], metavar="PREFIX",
                       help="Restrict to keys with this prefix (repeatable) — for "
                            "taking a cutover in tranches")
        p.add_argument("--regions", choices=("merge", "replace"), default="merge",
                       help="merge keeps region sets the target already has (default); "
                            "replace mirrors the source exactly")
        p.add_argument("--include-on-prem", action="store_true")
        if applying:
            p.add_argument("--apply", action="store_true",
                           help="Actually write. Without it this behaves as diff.")
            p.set_defaults(func=lambda a: cmd_diff(a, apply=a.apply))
        else:
            p.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        err("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
