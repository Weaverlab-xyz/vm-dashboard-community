#!/usr/bin/env bash
# Install Ansible Galaxy collections for CI, surviving a Galaxy outage when it can.
#
#   scripts/ci/galaxy_collections.sh beyondtrust.secrets_safe beyondtrust.password_safe
#
# Galaxy answers the occasional 500 on a collection download, and ansible-galaxy does not
# retry one. On 2026-10-24 it failed ansible-syntax for over forty minutes on PRs that
# touch no playbook, through four tries over two minutes.
#
# So: upgrade from Galaxy (four tries), which keeps the check against the latest releases,
# the same as the runner images install. If every try fails and a full set of collections
# is already in $DEST -- restored by actions/cache from an earlier run on main -- check
# against those instead, with a warning naming what was used. With no cached set the job
# fails as it always did: there is nothing to check against.
#
# Writes fresh=true|false to $GITHUB_OUTPUT, so the workflow saves the cache only after a
# real download. Runs locally unchanged; GALAXY_RETRY_SECONDS=0 skips the waits.
set -uo pipefail

if [ $# -eq 0 ]; then
  echo "usage: $0 namespace.collection..." >&2
  exit 2
fi

DEST="${ANSIBLE_COLLECTIONS_DIR:-$HOME/.ansible/collections}"
STEP="${GALAXY_RETRY_SECONDS:-20}"
OUT="${GITHUB_OUTPUT:-/dev/null}"

cached=()
for c in "$@"; do
  [ -d "$DEST/ansible_collections/${c%%.*}/${c#*.}" ] && cached+=("$c")
done

for attempt in 1 2 3 4; do
  if ansible-galaxy collection install --upgrade -p "$DEST" "$@"; then
    echo "fresh=true" >> "$OUT"
    exit 0
  fi
  [ "$attempt" = 4 ] && break
  echo "::warning::Galaxy collection install failed (attempt $attempt of 4); retrying"
  sleep $((attempt * STEP))
done

if [ "${#cached[@]}" -eq $# ]; then
  echo "::warning::Galaxy failed all four tries; checking against the collections cached by an earlier run on main: ${cached[*]}"
  # One positional at most, so list the whole path: the log shows which versions ran.
  ansible-galaxy collection list -p "$DEST" || true
  echo "fresh=false" >> "$OUT"
  exit 0
fi

echo "::error::Galaxy failed all four tries and no cached copy holds every collection (cached: ${cached[*]:-none})"
exit 1
