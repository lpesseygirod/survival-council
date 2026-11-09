#!/usr/bin/env bash
# From your Mac: copy the code to the server and run the installer there.
#   deploy/push.sh <server-ip> <dashboard-domain> [ssh-user]
# ssh-user: root on Hetzner (default), azureuser on Azure, ubuntu on AWS.
set -euo pipefail
HOST="${1:?usage: deploy/push.sh <server-ip> <dashboard-domain> [ssh-user]}"
DOMAIN="${2:?usage: deploy/push.sh <server-ip> <dashboard-domain> [ssh-user]}"
SSH_USER="${3:-root}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
rsync -az --delete --exclude '.venv' --exclude 'data' --exclude 'secrets' --exclude '.git' \
  --exclude '__pycache__' --exclude '.pytest_cache' ./ "$SSH_USER@$HOST:council-src/"
ssh -t "$SSH_USER@$HOST" "sudo bash ~/council-src/deploy/install.sh '$DOMAIN'"
