#!/usr/bin/env bash
# Survival Council: server install / update. Run as root on Ubuntu 24.04.
#   bash install.sh your.domain.example
# Safe to run again after pushing new code (it updates and restarts).
set -euo pipefail
DOMAIN="${1:?usage: install.sh <domain for the dashboard>}"
APP=/home/council/app
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
say() { printf '\n== %s\n' "$*"; }

say "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip bubblewrap acl sudo nftables procps rsync \
  unattended-upgrades ufw debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null || true

say "Users"
id council >/dev/null 2>&1 || useradd -m -s /bin/bash council
id council-ai >/dev/null 2>&1 || useradd -M -d /nonexistent -s /usr/sbin/nologin council-ai
chmod 700 /home/council

say "Code"
if [ "$SRC" != "$APP" ]; then
  mkdir -p "$APP"
  rsync -a --delete --exclude data --exclude secrets --exclude .venv "$SRC/" "$APP/"
fi
mkdir -p "$APP/data" "$APP/secrets"
chown -R council:council /home/council
chmod 700 "$APP/secrets"

say "Python environment for the game"
sudo -u council -H bash -c "cd $APP && ( [ -x .venv/bin/python ] || python3 -m venv .venv ) \
  && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e ."
"$APP/.venv/bin/python" -m playwright install-deps chromium >/dev/null
sudo -u council -H "$APP/.venv/bin/python" -m playwright install chromium >/dev/null

say "Sandbox for the AI's own code"
mkdir -p /opt/council-ai
[ -x /opt/council-ai/venv/bin/python ] || python3 -m venv /opt/council-ai/venv
/opt/council-ai/venv/bin/pip install -q --upgrade pip numpy pandas scipy requests httpx
rm -rf /opt/council-ai/sdk && cp -r "$APP/survival/sdk" /opt/council-ai/sdk
chmod -R a+rX /opt/council-ai
mkdir -p /srv/council-code/paper /srv/council-code/live
chown -R council-ai:council /srv/council-code
chmod 2770 /srv/council-code /srv/council-code/paper /srv/council-code/live
setfacl -R -m u:council:rwX,u:council-ai:rwX /srv/council-code
setfacl -R -d -m u:council:rwX,u:council-ai:rwX,g:council:rwX /srv/council-code
install -m 440 "$APP/deploy/sudoers-council" /etc/sudoers.d/council
visudo -cf /etc/sudoers.d/council >/dev/null
if [ -d /etc/apparmor.d ]; then
  install -m 644 "$APP/deploy/apparmor-bwrap" /etc/apparmor.d/bwrap
  apparmor_parser -r /etc/apparmor.d/bwrap 2>/dev/null || true
fi
mkdir -p /etc/council && install -m 644 "$APP/deploy/sandbox.nft" /etc/council/sandbox.nft

say "Services"
install -m 644 "$APP/deploy/council-netfilter.service" "$APP/deploy/council.service" \
  "$APP/deploy/council-dashboard.service" /etc/systemd/system/
sed "s/__DOMAIN__/$DOMAIN/" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
cat > /usr/local/bin/council <<'SH'
#!/usr/bin/env bash
# Lea's command on the server: council pause | resume | status | check | use paper|live ...
exec sudo -u council -H env COUNCIL_CODE_ROOT=/srv/council-code COUNCIL_SDK_DIR=/opt/council-ai/sdk \
  /home/council/app/council "$@"
SH
chmod 755 /usr/local/bin/council
systemctl daemon-reload
systemctl enable -q council-netfilter council council-dashboard caddy
systemctl restart council-netfilter
systemctl restart council-dashboard council caddy

say "Firewall (this machine; also set Hetzner's Cloud Firewall)"
{ ufw --force default deny incoming && ufw --force default allow outgoing \
  && ufw allow 22/tcp && ufw allow 443/tcp && ufw --force enable; } >/dev/null \
  || echo "WARNING: ufw could not be enabled here; rely on Hetzner's Cloud Firewall"

say "SSH: keys only"
if [ -d /etc/ssh/sshd_config.d ]; then
  cat > /etc/ssh/sshd_config.d/10-council.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHD
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
fi

say "Done"
[ -f "$APP/secrets/dashboard.json" ] || echo "Next: set the dashboard password with:  council dashboard-password"
echo "Then open https://$DOMAIN"
