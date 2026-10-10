#!/usr/bin/env bash
# gitleaks: fail when a commit adds something shaped like a credential.
#
# Scans git history, not just the working tree, because a secret committed and then
# deleted is still in the repository for anyone who clones it.
#
# Findings that were reviewed and are not credentials (test fixtures, documentation
# placeholders) are listed by fingerprint in .gitleaksignore, which says how they were
# checked. A REAL secret is never added there: rotate it first, then remove it from history.
#
# Usage:
#   scripts/ci/gitleaks.sh                      # all of history
#   scripts/ci/gitleaks.sh origin/main..HEAD    # just these commits (what a PR runs)

set -euo pipefail
cd "$(dirname "$0")/../.."

args=(git --no-banner --redact --verbose)
if [ $# -gt 0 ]; then
  args+=(--log-opts="$1")
fi
gitleaks "${args[@]}" .
