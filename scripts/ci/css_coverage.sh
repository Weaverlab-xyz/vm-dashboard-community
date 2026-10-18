#!/usr/bin/env bash
# Every class a page carried has a rule in the built stylesheet.
#
# web_dashboard/static/css/tailwind.css is generated from SOURCE (tailwind.config.js says
# which files), so a utility that only exists at runtime -- composed from parts, or sent
# in data the scan never sees -- would render unstyled with no error anywhere. The smoke
# test records every class name the DOM carried; this compiles exactly those names with
# the same Tailwind and fails on any rule the committed file lacks. Names Tailwind does
# not recognise (x-cloak, brand-nav, chk-*) generate nothing and are ignored.
#
# Usage: scripts/ci/css_coverage.sh CLASSES.json...   (written by tests/browser_smoke.js)
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^VERSION=//p' scripts/build_css.sh)
CLI=(${TAILWINDCSS:-npx --yes tailwindcss@$VERSION})
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 - "$WORK/seen.html" "$@" <<'PY'
import html, json, sys
names = sorted({c for f in sys.argv[2:] for c in json.load(open(f))})
with open(sys.argv[1], "w") as out:
    out.write('<div class="%s"></div>\n' % html.escape(" ".join(names)))
print(f"css coverage: {len(names)} distinct class names seen across the pages")
PY

cat > "$WORK/tailwind.config.js" <<JS
module.exports = { content: ['$WORK/seen.html'], theme: { extend: {} }, plugins: [] };
JS
printf '@tailwind components;\n@tailwind utilities;\n' > "$WORK/in.css"
"${CLI[@]}" -c "$WORK/tailwind.config.js" -i "$WORK/in.css" -o "$WORK/seen.css" 2>/dev/null

python3 - "$WORK/seen.css" web_dashboard/static/css/tailwind.css "$@" <<'PY'
import json, re, sys
def selectors(path):
    css = open(path).read()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = set()
    for m in re.finditer(r"\.((?:\\.|[A-Za-z0-9_-])+)", css):
        out.add(re.sub(r"\\(.)", r"\1", m.group(1)))
    return out
names = {c for f in sys.argv[3:] for c in json.load(open(f))}
# Only names a page carried: a compiled selector also names helpers such as `group`.
seen = selectors(sys.argv[1]) & names
built = selectors(sys.argv[2])
missing = sorted(seen - built)
if missing:
    print(f"::error::{len(missing)} class(es) on a page have no rule in the built CSS:")
    for c in missing[:40]:
        print("   ", c)
    print("Add the full class name literally to a scanned file (tailwind.config.js `content`),")
    print("never composed from parts, then run scripts/build_css.sh.")
    sys.exit(1)
print(f"css coverage: all {len(seen)} Tailwind classes on the pages have a rule")
PY
