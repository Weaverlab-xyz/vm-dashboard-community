#!/usr/bin/env bash
# `ansible-playbook --syntax-check` on every playbook the repository ships.
#
# tests/test_playbook_samples.py reads the playbooks as text and checks the conventions
# the dashboard relies on. It cannot tell whether Ansible will load them: a misspelled
# module, an argument on the wrong line, a `when:` at play level, a removed collection.
# Those surfaced on a live run against someone's lab. This asks Ansible itself, which
# resolves every module and action name against the installed collections.
#
# Which files: every .yml/.yaml under the directories below. A playbook (or a task file)
# is a YAML LIST; a file whose top level is a mapping is not Ansible (compose files,
# agent config examples) and is reported, not checked. A list that is not a play, such as a
# task file somebody adds, fails the syntax check, and the fix is to move it under a
# tasks/ directory that the glob below excludes, so the exclusion is visible in review.
#
# Needs the `ansible` package (which bundles ansible.windows, community.*, vyos.vyos,
# microsoft.ad, kubernetes.core, ...) plus the BeyondTrust collections the runner images
# install from Galaxy; see the `ansible-syntax` job in .github/workflows/tests.yml.
#
# Usage:
#   scripts/ci/ansible_syntax.sh             # everything
#   scripts/ci/ansible_syntax.sh FILE...     # just these

set -uo pipefail
cd "$(dirname "$0")/../.."

# Ansible refuses non-blocking stdio, which some CI log pipes and terminals hand it.
exec </dev/null

if [ $# -gt 0 ]; then
  files=("$@")
else
  mapfile -t files < <(find examples/playbooks web_dashboard/services/builtin_playbooks \
      examples/remote-agent \( -name '*.yml' -o -name '*.yaml' \) \
      -not -path '*/tasks/*' -not -path '*/node_modules/*' | sort)
fi

# Split by shape: a top-level list goes to Ansible, a top-level mapping is not a playbook.
# A file that is not valid YAML at all fails here.
mapfile -t classified < <(python3 - "${files[@]}" <<'EOF'
import sys, yaml
for f in sys.argv[1:]:
    try:
        with open(f, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        print(f"bad\t{f}\t{str(e).splitlines()[0]}")
        continue
    print(("play" if isinstance(doc, list) else "skip") + "\t" + f)
EOF
)

fail=0
checked=0
for line in "${classified[@]}"; do
  IFS=$'\t' read -r kind f why <<<"$line"
  case "$kind" in
    bad)
      echo "::error file=$f::not valid YAML: $why"; fail=1 ;;
    skip)
      echo "not a playbook (top level is a mapping): $f" ;;
    play)
      checked=$((checked + 1))
      if ! msg=$(ANSIBLE_NOCOLOR=1 ansible-playbook --syntax-check -i localhost, "$f" 2>&1); then
        echo "::error file=$f::ansible-playbook --syntax-check failed"
        echo "$msg" | sed 's/^/    /'
        fail=1
      fi ;;
  esac
done

if [ "$checked" -eq 0 ]; then
  echo "::error::no playbooks found; this check would pass without checking anything"
  exit 1
fi
echo "syntax-checked $checked playbook(s)"
exit $fail
