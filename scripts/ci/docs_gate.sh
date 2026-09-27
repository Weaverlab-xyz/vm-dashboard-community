#!/usr/bin/env bash
# Docs gate: a change that adds or alters user-facing surface must touch the docs, or
# say in a commit why it does not need to.
#
# tests/test_docs_coverage.py asks the precise question for surfaces it can enumerate
# (page routes, nav links, feature flags, schedulable request models). This asks the
# coarse one for everything else — a new button, a changed default, a new API field —
# because those are exactly what a September audit found undocumented: scheduling shipped
# across a dozen commits, and the two that widened it most touched no doc at all.
#
# The branch TRIGGERS the gate when, relative to the base, it
#   * changes web_dashboard/templates/, web_dashboard/api/, web_dashboard/models/,
#     web_dashboard/main.py or web_dashboard/services/feature_flags.py, or
#   * carries a `feat:` / `feat(scope):` commit.
# It PASSES when it also changes docs/, README.md, CONTRIBUTING.md (where a change to how
# contributors work is documented) or a runners/**/README.md, or when any commit on it
# carries a trailer line
#
#     Docs: none — <reason>
#
# (an ASCII hyphen works too). The reason is required: "none" alone is refused, so the
# opt-out is always a sentence a reviewer can disagree with.
#
# Usage:
#   scripts/ci/docs_gate.sh [BASE]     # BASE defaults to origin/main
#
# Needs full history for BASE and HEAD (actions/checkout with fetch-depth: 0).

set -euo pipefail

BASE="${1:-origin/main}"

if ! git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null; then
  echo "::error::docs gate: base '$BASE' is not a known commit — fetch it first" \
       "(git fetch origin <branch>) or pass the right ref."
  exit 2
fi

# Three dots: what this branch changed since it left BASE, not what BASE gained since.
changed="$(git diff --name-only "$BASE"...HEAD)"
# Two dots, no merges: the branch's own commits. Merges from BASE bring BASE's commits,
# which are already on BASE and were gated there.
commits="$(git log --no-merges --format='%h %s' "$BASE"..HEAD)"
messages="$(git log --no-merges --format='%B' "$BASE"..HEAD)"

surface="$(printf '%s\n' "$changed" | grep -E \
  '^web_dashboard/(templates/|api/|models/|main\.py$|services/feature_flags\.py$)' || true)"
feats="$(printf '%s\n' "$commits" | grep -E '^[0-9a-f]+ feat(\([^)]*\))?!?:' || true)"

if [ -z "$surface" ] && [ -z "$feats" ]; then
  echo "docs gate: no user-facing surface changed — nothing to check."
  exit 0
fi

docs="$(printf '%s\n' "$changed" | grep -E '^(docs/.*\.md|README\.md|CONTRIBUTING\.md|runners/.*README\.md)$' || true)"
if [ -n "$docs" ]; then
  echo "docs gate: passed — docs changed alongside the code:"
  printf '  %s\n' $docs
  exit 0
fi

optout="$(printf '%s\n' "$messages" | grep -E '^Docs: *none *(—|–|-) *[^ ]' || true)"
if [ -n "$optout" ]; then
  echo "docs gate: passed — opted out by commit trailer:"
  printf '%s\n' "$optout" | sed 's/^/  /'
  exit 0
fi

echo "::error::docs gate: this change touches user-facing surface but no docs."
if [ -n "$feats" ]; then
  echo "feat commits:"
  printf '%s\n' "$feats" | sed 's/^/  /'
fi
if [ -n "$surface" ]; then
  echo "user-facing files changed:"
  printf '%s\n' "$surface" | sed 's/^/  /'
fi
cat <<'EOF'

Update the page under docs/ that describes this (docs/README.md indexes them), or, if
the change really has nothing for a reader — a refactor, a fix that restores documented
behaviour — add this trailer to any commit on the branch:

    Docs: none — <one sentence on why>

See CONTRIBUTING.md, "Documentation".
EOF
exit 1
