#!/usr/bin/env bash
# From your Mac: copy the code to the server and run the installer there.
#   deploy/push.sh <server-ip> <dashboard-domain>
set -euo pipefail
HOST="${1:?usage: deploy/push.sh <server-ip> <dashboard-domain>}"
DOMAIN="${2:?usage: deploy/push.sh <server-ip> <dashboard-domain>}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
rsync -az --delete --exclude '.venv' --exclude 'data' --exclude 'secrets' --exclude '.git' \
  --exclude '__pycache__' --exclude '.pytest_cache' ./ "root@$HOST:/root/council-src/"
ssh "root@$HOST" "bash /root/council-src/deploy/install.sh '$DOMAIN'"
