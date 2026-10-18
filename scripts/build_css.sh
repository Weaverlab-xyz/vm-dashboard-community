#!/usr/bin/env bash
# Build web_dashboard/static/css/tailwind.css, the app's only Tailwind stylesheet.
#
# The file is committed: the image ships it as-is, so building the dashboard needs no
# Node and no network. The cost is that a new class name in a template, script or Python
# file has no rule until this is re-run -- which is what --check is for. CI runs it, and
# fails when the committed file differs from a fresh build.
#
# Usage:
#   scripts/build_css.sh           # rebuild the committed file
#   scripts/build_css.sh --check   # build to a temp file; exit 1 if the committed one is stale
#
# Needs Node. TAILWINDCSS overrides the CLI (a path to an installed tailwindcss binary);
# otherwise it runs the pinned version through npx. To bump Tailwind, change the version
# below, re-run, and commit both.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=3.4.17
OUT=web_dashboard/static/css/tailwind.css
CLI=(${TAILWINDCSS:-npx --yes tailwindcss@$VERSION})

build() {
  "${CLI[@]}" -c tailwind.config.js -i web_dashboard/static/css/tailwind.input.css \
    -o "$1" --minify
}

if [ "${1:-}" = "--check" ]; then
  tmp="$(mktemp --suffix=.css)"
  trap 'rm -f "$tmp"' EXIT
  build "$tmp"
  if ! cmp -s "$tmp" "$OUT"; then
    echo "::error file=$OUT::$OUT is stale: a class name was added or removed without a rebuild."
    echo "Run scripts/build_css.sh and commit the result."
    exit 1
  fi
  echo "$OUT is up to date."
else
  build "$OUT"
  echo "Built $OUT ($(wc -c <"$OUT") bytes)."
fi
