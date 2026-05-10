#!/usr/bin/env bash
# Sandbox bootstrappers prereq check (WSL / Linux).
# Verifies docker, docker-compose-v2, aws, az, gcloud, jq are available.
# Prints apt-install hints for anything missing.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_wsl

section "Checking prerequisites"

declare -A APT_HINTS=(
  [docker]="sudo apt-get install -y docker.io && sudo usermod -aG docker \$USER"
  [docker-compose]="sudo apt-get install -y docker-compose-v2"
  [jq]="sudo apt-get install -y jq"
  [curl]="sudo apt-get install -y curl"
  [unzip]="sudo apt-get install -y unzip"
)

# AWS CLI v2: not in apt; vendor URL.
declare -A SPECIAL_HINTS=(
  [aws]="curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip && unzip -q /tmp/awscli.zip -d /tmp && sudo /tmp/aws/install"
  [az]="curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
  [gcloud]="curl -fsSL https://sdk.cloud.google.com | bash && exec -l \$SHELL  # then: gcloud init"
)

CHECKS=(docker jq curl unzip aws az gcloud)
DOCKER_COMPOSE_OK=0
MISSING=()

for cmd in "${CHECKS[@]}"; do
  if command -v "$cmd" >/dev/null 2>&1; then
    case "$cmd" in
      aws)    ok "aws    — $(aws --version 2>&1 | head -n1)";;
      az)     ok "az     — $(az --version 2>&1 | head -n1)";;
      gcloud) ok "gcloud — $(gcloud --version 2>&1 | head -n1)";;
      docker) ok "docker — $(docker --version)";;
      jq)     ok "jq     — $(jq --version)";;
      *)      ok "$cmd";;
    esac
  else
    MISSING+=("$cmd")
    warn "$cmd is not installed."
  fi
done

# docker-compose v2 is `docker compose` (subcommand of docker), not a separate binary.
if docker compose version >/dev/null 2>&1; then
  ok "docker compose — $(docker compose version --short 2>/dev/null || echo 'v2')"
  DOCKER_COMPOSE_OK=1
else
  warn "docker-compose v2 not available (run \`docker compose version\`)."
  MISSING+=("docker-compose")
fi

# Confirm the user is in the docker group (else `docker ps` fails without sudo).
if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    warn "docker is installed but the current user can't reach the daemon. Run:"
    warn "    sudo usermod -aG docker \$USER && newgrp docker"
    warn "(Or in WSL: ensure Docker Desktop's WSL integration is enabled for this distro.)"
  fi
fi

if (( ${#MISSING[@]} > 0 )); then
  section "Install missing prereqs"
  for m in "${MISSING[@]}"; do
    if [[ -n "${APT_HINTS[$m]:-}" ]]; then
      printf "  \033[0;33m%-15s\033[0m → %s\n" "$m" "${APT_HINTS[$m]}"
    elif [[ -n "${SPECIAL_HINTS[$m]:-}" ]]; then
      printf "  \033[0;33m%-15s\033[0m → %s\n" "$m" "${SPECIAL_HINTS[$m]}"
    else
      printf "  \033[0;33m%-15s\033[0m → (no install hint; consult docs)\n" "$m"
    fi
  done
  printf "\n"
  exit 1
fi

section "All prerequisites satisfied"
ok "Ready to run setup-aws.sh / setup-azure.sh / setup-gcp.sh"

cat <<'EOF'

Next steps — authenticate each CLI you plan to use:

  AWS:    aws configure                       (or: aws sso login)
  Azure:  az login
  GCP:    gcloud auth login && gcloud auth application-default login

Then:

  ./scripts/sandbox/Linux/setup-aws.sh
  ./scripts/sandbox/Linux/setup-azure.sh
  ./scripts/sandbox/Linux/setup-gcp.sh

To tear it all down:

  ./scripts/sandbox/Linux/rollback.sh --cloud all

EOF
