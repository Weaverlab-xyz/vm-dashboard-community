#!/usr/bin/env bash
# `terraform fmt -check` and `terraform validate` on every module under terraform/.
#
# Nothing ran Terraform's own checks before this. A misspelled argument, a reference to
# a variable that was renamed, or a resource attribute a provider dropped surfaced on
# somebody's `plan`. `validate` catches all three without credentials or state:
# `init -backend=false` downloads the providers, and validate checks every block against
# their schemas.
#
# Usage:
#   scripts/ci/terraform_validate.sh               # fmt + every module
#   scripts/ci/terraform_validate.sh DIR...        # validate just these (no fmt)
#
# TF_EXCLUDE="name other" leaves modules out of the every-module run, matched against the
# directory path relative to terraform/. CI uses it for terraform/workload_credentials,
# which needs a newer CLI than the image ships and is validated in its own step.
#
# Set TF_PLUGIN_CACHE_DIR to share provider downloads across modules (CI does); each
# module otherwise downloads its own copy of the same multi-hundred-megabyte providers.

set -uo pipefail
cd "$(dirname "$0")/../.."
export CHECKPOINT_DISABLE=1 TF_IN_AUTOMATION=1 TF_INPUT=0
if [ -n "${TF_PLUGIN_CACHE_DIR:-}" ]; then mkdir -p "$TF_PLUGIN_CACHE_DIR"; fi

terraform version

fail=0
if [ $# -gt 0 ]; then
  dirs=("$@")
else
  if ! terraform fmt -check -recursive -diff terraform/; then
    echo "::error::terraform fmt found unformatted files; run: terraform fmt -recursive terraform/"
    fail=1
  fi
  mapfile -t dirs < <(find terraform -name '*.tf' -not -path '*/.terraform/*' -printf '%h\n' | sort -u)
fi

checked=0
for d in "${dirs[@]}"; do
  rel=${d#terraform/}
  if [ $# -eq 0 ] && [[ " ${TF_EXCLUDE:-} " == *" $rel "* ]]; then
    echo "excluded by TF_EXCLUDE: $d"
    continue
  fi
  echo "::group::$d"
  checked=$((checked + 1))
  # -lockfile=readonly is not used: the modules commit no lock files, so init resolves the
  # newest provider that satisfies each constraint -- the same thing a user's init does.
  if ! out=$(terraform -chdir="$d" init -backend=false -no-color 2>&1); then
    echo "$out"
    echo "::endgroup::"
    echo "::error file=$d::terraform init failed in $d"
    fail=1
    continue
  fi
  if ! terraform -chdir="$d" validate -no-color; then
    echo "::endgroup::"
    echo "::error file=$d::terraform validate failed in $d"
    fail=1
    continue
  fi
  echo "::endgroup::"
done

if [ "$checked" -eq 0 ]; then
  echo "::error::no Terraform modules found; this check would pass without checking anything"
  exit 1
fi
echo "validated $checked module(s)"
exit $fail
