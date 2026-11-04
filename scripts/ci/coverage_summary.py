#!/usr/bin/env python3
"""Combine the data files `COVERAGE=1 scripts/ci/run_tests.sh` wrote, and report.

Writes coverage.xml, coverage.json and a text report into the data directory, then
prints the least-covered modules under web_dashboard/services and web_dashboard/api.
No threshold: this reports, it never fails on a number. It exits non-zero only when
there is nothing to combine (the run produced no data), so an empty report is loud.

Usage: python scripts/ci/coverage_summary.py [DIR] [--top N]
  DIR  the data directory (default .coverage-data, the run_tests.sh default)
"""
import argparse
import json
import os
import subprocess
import sys

SECTIONS = ("web_dashboard/services/", "web_dashboard/api/")


def cov(*args, **kw):
    return subprocess.run([sys.executable, "-m", "coverage", *args], **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default=".coverage-data")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    d = os.path.abspath(a.dir)
    data = os.path.join(d, ".coverage")
    names = os.listdir(d) if os.path.isdir(d) else []
    if not any(f.startswith(".coverage.") for f in names):
        print(f"::error::no coverage data files in {d}; was the run started with COVERAGE=1?")
        return 1

    if cov("combine", "--keep", f"--data-file={data}", d).returncode != 0:
        return 1
    with open(os.path.join(d, "report.txt"), "w") as fh:
        cov("report", f"--data-file={data}", "--skip-empty", stdout=fh)
    cov("xml", f"--data-file={data}", "-o", os.path.join(d, "coverage.xml"), "-q")
    js = os.path.join(d, "coverage.json")
    cov("json", f"--data-file={data}", "-o", js, "-q")

    with open(js) as fh:
        j = json.load(fh)
    print(f"TOTAL line coverage of web_dashboard: {j['totals']['percent_covered']:.1f}%")
    for sec in SECTIONS:
        rows = []
        for name, f in j["files"].items():
            n = name.replace("\\", "/")
            s = f["summary"]
            if sec in n and s["num_statements"] >= 20:
                rows.append((s["percent_covered"], s["missing_lines"], n))
        rows.sort()
        print(f"\nLeast covered under {sec} (files with >= 20 statements):")
        print(f"{'cover':>7} {'missed':>7}  file")
        for pct, miss, n in rows[: a.top]:
            print(f"{pct:6.1f}% {miss:7d}  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
