#!/usr/bin/env bash
# Sandbox bootstrappers prereq check (WSL / Linux / macOS).
# Verifies docker, docker-compose-v2, aws, az, gcloud, jq are available.
# Prints platform-appropriate install hints for anything missing.

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_supported_os

case "$(uname -s)" in
  Darwin) PLATFORM=darwin ;;
  *)      PLATFORM=linux ;;
esac

section "Checking prerequisites"

# Platform-appropriate install hints. Implemented as a function (rather than
# associative arrays) so this script parses on macOS' default bash 3.2.
install_hint() {
  local cmd="$1"
  if [[ "$PLATFORM" == "darwin" ]]; then
    case "$cmd" in
      docker)         echo "brew install --cask docker  # then launch Docker Desktop" ;;
      docker-compose) echo "(bundled with Docker Desktop on macOS)" ;;
      jq)             echo "brew install jq" ;;
      curl)           echo "(preinstalled on macOS)" ;;
      unzip)          echo "(preinstalled on macOS)" ;;
      aws)            echo "brew install awscli" ;;
      az)             echo "brew install azure-cli" ;;
      gcloud)         echo "brew install --cask google-cloud-sdk" ;;
      *)              echo "" ;;
    esac
  else
    case "$cmd" in
      docker)         echo "sudo apt-get install -y docker.io && sudo usermod -aG docker \$USER" ;;
      docker-compose) echo "sudo apt-get install -y docker-compose-v2" ;;
      jq)             echo "sudo apt-get install -y jq" ;;
      curl)           echo "sudo apt-get install -y curl" ;;
      unzip)          echo "sudo apt-get install -y unzip" ;;
      aws)            echo "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip && unzip -q /tmp/awscli.zip -d /tmp && sudo /tmp/aws/install" ;;
      az)             echo "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash" ;;
      gcloud)         echo "curl -fsSL https://sdk.cloud.google.com | bash && exec -l \$SHELL  # then: gcloud init" ;;
      *)              echo "" ;;
    esac
  fi
}

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

# Confirm the user can reach the docker daemon (else `docker ps` fails without sudo).
if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    warn "docker is installed but the current user can't reach the daemon."
    if [[ "$PLATFORM" == "darwin" ]]; then
      warn "    Ensure Docker Desktop is running (open -a Docker)."
    else
      warn "    Run: sudo usermod -aG docker \$USER && newgrp docker"
      warn "    (Or in WSL: ensure Docker Desktop's WSL integration is enabled for this distro.)"
    fi
  fi
fi

if (( ${#MISSING[@]} > 0 )); then
  section "Install missing prereqs"
  for m in "${MISSING[@]}"; do
    hint="$(install_hint "$m")"
    if [[ -n "$hint" ]]; then
      printf "  \033[0;33m%-15s\033[0m → %s\n" "$m" "$hint"
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
