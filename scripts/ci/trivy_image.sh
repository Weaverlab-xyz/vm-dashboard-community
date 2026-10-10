#!/usr/bin/env bash
# Trivy: fail when an image ships a HIGH or CRITICAL vulnerability that has a fix.
#
# What blocks, and what does not:
#   * BLOCKS: OS packages and Python packages with a fixed version available. Both are
#     ours to fix: a base-image bump, a package upgrade, a requirements floor.
#   * REPORTED, does not block: vendored Go binaries (Terraform, its providers, Packer and
#     its plugins, OPA, kubectl, helm). Their findings are in the Go toolchain and modules
#     the vendor compiled in; only the vendor's next release changes them, so a blocking
#     check would be red on every PR with nothing a PR can do. They are listed in the job
#     summary (and, on main, uploaded to code scanning) so a new vendor release is taken
#     deliberately.
#   * Unfixed findings are left out (--ignore-unfixed): there is nothing to upgrade to.
#
# Accepted findings live in .trivyignore.yaml, scoped by path, each with a statement.
#
# Usage:
#   scripts/ci/trivy_image.sh IMAGE [REPORT.json]

set -uo pipefail
cd "$(dirname "$0")/../.."
image=$1
report=${2:-trivy-$(echo "$image" | tr '/:' '__').json}

trivy image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
  --ignorefile .trivyignore.yaml --format json --output "$report" "$image" || exit 2

python3 - "$image" "$report" <<'PY'
import collections, json, os, sys
image, path = sys.argv[1], sys.argv[2]
results = json.load(open(path)).get("Results") or []
gated, vendored = collections.defaultdict(set), collections.Counter()
for r in results:
    for v in r.get("Vulnerabilities") or []:
        if r.get("Type") == "gobinary":
            vendored[r["Target"].rsplit("/", 1)[-1]] += 1
            continue
        key = (r.get("Type"), v["PkgName"], v["InstalledVersion"], v.get("FixedVersion") or "")
        gated[key].add(f'{v["VulnerabilityID"]} ({v["Severity"]})')

lines = [f"### Trivy: `{image}`", ""]
if gated:
    lines += ["**Blocking: fixable HIGH/CRITICAL in OS or Python packages**", "",
              "| type | package | installed | fixed in | advisories |", "|---|---|---|---|---|"]
    lines += [f"| {t} | {p} | {i} | {f} | {', '.join(sorted(a))} |"
              for (t, p, i, f), a in sorted(gated.items())]
else:
    lines.append("No fixable HIGH/CRITICAL in OS or Python packages.")
if vendored:
    lines += ["", f"Vendored Go binaries (reported, not blocking): "
              f"{sum(vendored.values())} findings in {len(vendored)} binaries"]
    lines += [f"- `{b}`: {n}" for b, n in vendored.most_common()]
text = "\n".join(lines)
print(text)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
        fh.write(text + "\n\n")
if gated:
    print(f"::error::{image}: {sum(len(a) for a in gated.values())} fixable HIGH/CRITICAL "
          "findings in OS or Python packages. Upgrade, or record a reviewed exception in "
          ".trivyignore.yaml.")
    sys.exit(1)
PY
