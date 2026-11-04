#!/usr/bin/env python3
"""Run pyright and fail only on findings that are NOT in the committed baseline.

Why a ratchet and not a plain pyright job: the codebase has never been type-checked, so a
plain run reports ~150 findings on day one and would be red forever or switched off.
Instead the existing findings are recorded in scripts/ci/pyright_baseline.json, and a run
fails only when a (file, rule, message) group has MORE findings than the baseline allows.
Fixing one and re-running with --update shrinks the baseline; it never grows by accident.

What is checked is deliberately narrow (see pyrightconfig.json): the rules that find real
latent bugs -- `x.first().name` where the query can return None, an index into None, a
name that may be unbound, a call that matches no overload -- and none of the
SQLAlchemy-Column assignability noise that the default `basic` mode drowns in (~4,000
findings, nearly all of it `Column[str]` vs `str`).

Line numbers are not part of the key, so editing a file above a baselined finding does not
turn it into a "new" one.

The environment is deliberately EMPTY: no requirements are installed, so every third-party
import is Unknown and the result does not depend on which package versions a runner got.
Run it the same way locally.

Usage:
    python scripts/ci/pyright_ratchet.py            # check (CI)
    python scripts/ci/pyright_ratchet.py --update   # rewrite the baseline after fixing some
"""
import collections
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = ROOT / "scripts" / "ci" / "pyright_baseline.json"


def _run() -> collections.Counter:
    r = subprocess.run(["pyright", "--outputjson"], cwd=ROOT, capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        sys.exit(f"pyright produced no JSON (exit {r.returncode}):\n{r.stdout[-500:]}\n{r.stderr[-500:]}")
    found: collections.Counter = collections.Counter()
    for d in data["generalDiagnostics"]:
        rel = pathlib.Path(d["file"]).resolve().relative_to(ROOT).as_posix()
        found[f"{rel}|{d.get('rule', '')}|{d['message'].splitlines()[0]}"] += 1
    return found


def main() -> int:
    found = _run()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(dict(sorted(found.items())), indent=1) + "\n", encoding="utf-8")
        print(f"baseline written: {sum(found.values())} findings in {len(found)} groups")
        return 0
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    new = {k: n - base.get(k, 0) for k, n in found.items() if n > base.get(k, 0)}
    fixed = sum(max(0, n - found.get(k, 0)) for k, n in base.items())
    if new:
        print(f"pyright: {sum(new.values())} finding(s) not in the baseline:\n")
        for k, n in sorted(new.items()):
            f, rule, msg = k.split("|", 2)
            print(f"  {f}  [{rule}]  {msg}" + (f"  (+{n})" if n > 1 else ""))
        print("\nFix them, or if one is a false positive, say why in the PR and run "
              "`python scripts/ci/pyright_ratchet.py --update`.")
        return 1
    note = f"; {fixed} baselined finding(s) are gone -- run --update to bank it" if fixed else ""
    print(f"pyright: no new findings ({sum(found.values())} baselined{note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
