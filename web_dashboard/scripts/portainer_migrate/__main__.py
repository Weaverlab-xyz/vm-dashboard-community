"""CLI for the Portainer migration tool.

Usage:
  python -m web_dashboard.scripts.portainer_migrate export --url URL [--pat TOKEN] [--out PATH]
  python -m web_dashboard.scripts.portainer_migrate inspect --bundle PATH

``export`` reads a RUNNING Portainer over its REST API. To feed it a ``.tar.gz``
backup instead, open the archive with a throwaway Portainer first - the archive is
a BoltDB database only Portainer can read, and it only restores into a pristine
instance:

  docker run -d --name portainer-scratch -p 9443:9443 portainer/portainer-ce:latest
  curl -k -X POST https://localhost:9443/api/restore \\
       -F "file=@portainer_backup.tar.gz" -F "password=<if encrypted>"
  python -m web_dashboard.scripts.portainer_migrate export \\
       --url https://localhost:9443 --username admin --insecure --out bundle.json
  docker rm -f portainer-scratch

The restore must be the FIRST thing that instance is asked to do: Portainer closes
its first-run window a short time after the container starts and then fences off
the whole API.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from . import bundle as bundle_mod
from .client import Client, PortainerCliError

# ── Output helpers ───────────────────────────────────────────────────────────
# Everything conversational goes to stderr so a future --stdout mode stays
# pipeable. Mirrors config_migrate.__main__.

_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _unicode_ok() -> bool:
    """Whether this console can render the tick/cross. A legacy Windows console is
    cp1252 and either mojibakes them or raises mid-run; config_migrate hit the same
    wall. Probing the stream beats guessing from the platform."""
    try:
        "✓✗──".encode(sys.stderr.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_GLYPHS = ({"ok": "✓", "bad": "✗", "rule": "──"} if _unicode_ok()
           else {"ok": "OK", "bad": "X", "rule": "--"})


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


# ── Connect ──────────────────────────────────────────────────────────────────

def _connect(args: argparse.Namespace) -> Client:
    """An authenticated client, prompting for a password when needed."""
    client = Client(args.url, pat=args.pat, ca_bundle=args.ca_bundle,
                    insecure=args.insecure, timeout=args.timeout)
    try:
        status = client.status()
    except PortainerCliError as exc:
        # The fenced-off state has a specific remedy, so name it rather than
        # reporting a generic unreachable-host error.
        if "initialization timeout" in (exc.detail or "").lower():
            die("This Portainer closed its admin-initialization window with no admin "
                "user, so its entire API is fenced off. Restart the container (or "
                "recreate it) and restore the backup as the FIRST thing it is asked "
                "to do.")
        die(f"Cannot reach {args.url}: {exc.detail or exc}")
    if status.get("Version"):
        info(f"Portainer {status['Version']} at {args.url}")

    if args.pat and not args.username:
        return client
    user = args.username or input(f"Portainer username for {args.url}: ").strip()
    password = args.password or getpass.getpass(f"Password for {user}: ")
    if not user or not password:
        die("A username and password are required (or pass --pat).")
    try:
        client.login(user, password)
    except PortainerCliError as exc:
        die(f"Login failed against {args.url}: {exc.detail or exc}")
    return client


# ── export ───────────────────────────────────────────────────────────────────

def cmd_export(args: argparse.Namespace) -> int:
    client = _connect(args)
    warnings: list[str] = []

    section("Reading Portainer")
    data: dict = {}
    reference: dict = {}

    def _read(label: str, path: str, into: dict, key: str) -> None:
        """One collection. A read that fails is recorded and skipped rather than
        aborting: a partial bundle an operator can inspect beats no bundle, and
        Portainer 403s some endpoints for a non-admin token."""
        try:
            into[key] = client.get_list(path)
            info(f"{label}: {len(into[key])}")
        except PortainerCliError as exc:
            msg = f"could not read {label} ({path}): {exc.detail or exc}"
            warn(msg)
            warnings.append(msg)
            into[key] = []

    _read("users", "/api/users", data, "users")
    _read("teams", "/api/teams", data, "teams")
    _read("team memberships", "/api/team_memberships", data, "team_memberships")
    _read("registries", "/api/registries", data, "registries")
    _read("environments (reference only)", "/api/endpoints", reference, "endpoints")
    _read("environment groups", "/api/endpoint_groups", reference, "endpoint_groups")
    _read("tags", "/api/tags", reference, "tags")
    try:
        reference["settings"] = client.get_obj("/api/settings")
    except PortainerCliError as exc:
        warn(f"could not read settings: {exc.detail or exc}")
        warnings.append(f"could not read settings: {exc.detail or exc}")

    # Stacks need a second call each for the compose text — a stack without it is
    # just a name and could not be recreated anywhere.
    stacks: list = []
    try:
        raw_stacks = client.get_list("/api/stacks")
    except PortainerCliError as exc:
        warn(f"could not list stacks: {exc.detail or exc}")
        warnings.append(f"could not list stacks: {exc.detail or exc}")
        raw_stacks = []
    for stack in raw_stacks:
        entry = dict(stack)
        sid = stack.get("Id")
        try:
            entry["StackFileContent"] = client.stack_file(sid) if sid else ""
        except PortainerCliError as exc:
            entry["StackFileContent"] = ""
            msg = (f"stack {stack.get('Name') or sid!r}: compose file unreadable "
                   f"({exc.detail or exc}) - it will be skipped on import")
            warn(msg)
            warnings.append(msg)
        stacks.append(entry)
    data["stacks"] = stacks
    info(f"stacks: {len(stacks)}")

    version = ""
    try:
        version = (client.status() or {}).get("Version") or ""
    except PortainerCliError:
        pass

    doc = bundle_mod.build(source_url=args.url, source_version=version,
                           data=data, reference=reference, warnings=warnings)
    problems = bundle_mod.validate(doc)
    if problems:
        # An empty bundle is the common real case here (a fresh scratch instance
        # that the restore never actually landed in), so say so plainly.
        for p in problems:
            err(p)
        die("Refusing to write a bundle that cannot be imported.")

    out = Path(args.out) if args.out else bundle_mod.default_path()
    written = bundle_mod.write(out, doc)
    section("Bundle")
    ok(f"wrote {written}")
    counts = doc["meta"]["counts"]
    info(", ".join(f"{k}={v}" for k, v in counts.items()))
    warn("Environment connections are NOT importable - see 'not_migrated' in the "
         "bundle. A cloud Portainer reaches a local Docker host only via an Edge agent.")
    info("Import it from the dashboard: Containers > Portainer > Import bundle.")
    return 0


# ── inspect ──────────────────────────────────────────────────────────────────

def cmd_inspect(args: argparse.Namespace) -> int:
    """Summarize a bundle without a Portainer anywhere in sight — for reviewing a
    hand-edited file before handing it to the dashboard."""
    try:
        doc = bundle_mod.read(args.bundle)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"Cannot read {args.bundle}: {exc}")
    problems = bundle_mod.validate(doc)
    meta = doc.get("meta") or {}
    section("Bundle")
    info(f"source:   {meta.get('source_url') or '(unknown)'}")
    info(f"version:  {meta.get('source_version') or '(unknown)'}")
    info(f"exported: {meta.get('exported_at') or '(unknown)'}")
    section("Importable")
    for name in bundle_mod.SECTIONS:
        info(f"{name}: {len((doc.get('data') or {}).get(name) or [])}")
    for line in doc.get("warnings") or []:
        warn(line)
    section("Not migrated")
    for line in doc.get("not_migrated") or []:
        info(line)
    if problems:
        section("Problems")
        for p in problems:
            err(p)
        return 1
    ok("the bundle is structurally valid")
    return 0


# ── Parser ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portainer_migrate",
        description="Read a Portainer CE instance into a reviewable JSON bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Read a running Portainer into a bundle")
    exp.add_argument("--url", required=True, metavar="URL",
                     help="Portainer base URL, e.g. https://localhost:9443")
    exp.add_argument("--pat", default=os.environ.get("PORTAINER_PAT", ""),
                     help="Access token (env: PORTAINER_PAT). Omit to log in instead.")
    exp.add_argument("--username", default=os.environ.get("PORTAINER_USER", ""),
                     help="Admin username (env: PORTAINER_USER; prompted if needed)")
    exp.add_argument("--password", default=os.environ.get("PORTAINER_PASSWORD", ""),
                     help="Admin password (env: PORTAINER_PASSWORD; prompted if omitted)")
    exp.add_argument("--out", default="", metavar="PATH",
                     help="Bundle path (default: ~/.portainer-migrate/portainer-<ts>.json)")
    exp.add_argument("--ca-bundle", default=os.environ.get("PORTAINER_CA_BUNDLE", ""),
                     help="PEM bundle for a private or corporate CA")
    exp.add_argument("--insecure", action="store_true",
                     help="Skip TLS verification - normal for Portainer's self-signed "
                          ":9443 certificate")
    exp.add_argument("--timeout", type=int, default=30, help="Per-request timeout, seconds")
    exp.set_defaults(func=cmd_export)

    ins = sub.add_parser("inspect", help="Summarize and validate a bundle offline")
    ins.add_argument("--bundle", required=True, metavar="PATH")
    ins.set_defaults(func=cmd_inspect)
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
