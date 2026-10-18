#!/usr/bin/env python3
"""Report how far each pinned front-end package is behind upstream.

Dependabot covers pip, Docker and Actions. Nothing covered these, and a pin nobody looks at
just ages: Tailwind 3.4.19 shipped while the app sat on 3.4.17. The pins are read from
where they live, so a new one is found without editing this file:

  * Alpine        the vendored file's name, static/js/vendor/alpine-<version>.min.js
  * Tailwind      VERSION= in scripts/build_css.sh
  * CDN packages  every cdn.jsdelivr.net/npm/<pkg>@<version> or unpkg.com URL in a template
  * Playwright    the `playwright@<version>` the browser-smoke job installs

Exit 1 when a newer release exists on the SAME major: a patch or minor nobody has taken,
which is routine work. A newer major is reported but does not fail -- it is a migration
(Mermaid 10 -> 11 changed its API), a decision rather than an oversight.

Run by .github/workflows/frontend-pins.yml on a schedule, and by hand:
    python3 scripts/ci/frontend_pins.py
"""
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = "https://registry.npmjs.org/"


def pins():
    """[(package, version, where)] for every pin in the tree."""
    found = []
    for f in sorted((ROOT / "web_dashboard/static/js/vendor").glob("alpine-*.min.js")):
        found.append(("alpinejs", f.name[len("alpine-"):-len(".min.js")],
                      str(f.relative_to(ROOT))))
    m = re.search(r"^VERSION=(\S+)$", (ROOT / "scripts/build_css.sh").read_text(), re.M)
    if m:
        found.append(("tailwindcss", m.group(1), "scripts/build_css.sh"))
    cdn = re.compile(r"(?:cdn\.jsdelivr\.net/npm|unpkg\.com)/(@?[a-z0-9][\w.-]*(?:/[\w.-]+)?)"
                     r"@(\d+\.\d+\.\d+)")
    for t in sorted((ROOT / "web_dashboard/templates").rglob("*.html")):
        for pkg, ver in cdn.findall(t.read_text(encoding="utf-8")):
            found.append((pkg, ver, str(t.relative_to(ROOT))))
    for wf in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for ver in re.findall(r"\bplaywright@(\d+\.\d+\.\d+)", wf.read_text()):
            found.append(("playwright", ver, str(wf.relative_to(ROOT))))
    # One row per (package, version): swagger-ui-dist is pinned twice in one template.
    seen, out = set(), []
    for p in found:
        if p[:2] not in seen:
            seen.add(p[:2])
            out.append(p)
    return out


def _key(v):
    return tuple(int(x) for x in v.split("."))


def releases(pkg):
    with urllib.request.urlopen(REGISTRY + pkg.replace("/", "%2F"), timeout=30) as r:
        data = json.load(r)
    # Stable releases only: x.y.z with no -rc / -alpha suffix.
    return sorted((v for v in data["versions"] if re.fullmatch(r"\d+\.\d+\.\d+", v)), key=_key)


def main():
    behind = 0
    rows = []
    for pkg, ver, where in pins():
        stable = releases(pkg)
        same_major = [v for v in stable if _key(v)[0] == _key(ver)[0]]
        latest_same = same_major[-1] if same_major else ver
        latest = stable[-1]
        if _key(latest_same) > _key(ver):
            behind += 1
            state = f"BEHIND: {latest_same} available"
        elif _key(latest) > _key(ver):
            state = f"current on {_key(ver)[0]}.x; {latest} is a new major"
        else:
            state = "current"
        rows.append((pkg, ver, state, where))
    w = max(len(r[0]) for r in rows)
    for pkg, ver, state, where in rows:
        line = f"{pkg:<{w}}  {ver:<9} {state}   ({where})"
        print(f"::warning file={where}::{line}" if state.startswith("BEHIND") else line)
    if behind:
        print(f"\n{behind} pin(s) behind a release on the same major. Bump each where it is "
              "pinned (CONTRIBUTING.md, 'Other checks CI runs', says how for each kind).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
