#!/bin/sh
# Set up the memory store on a fresh Ubuntu 22.04/24.04 instance.
#
# Run this ON the box, as root, with the Trick source tree already copied there:
#
#     sudo sh deploy/vault-server.sh /path/to/trick
#
# It is written for the cheapest useful instance (1 vCPU, 512 MB). What it does:
#
#   * a system user with no login shell,
#   * a virtualenv with only the package and its backup extra,
#   * a 0700 store root and one account, whose token is printed once,
#   * a self-signed certificate, valid ten years, and the fingerprint a client
#     must pin. A public CA is not needed: the client trusts a hash, not a name,
#     so there is no renewal to forget and no CA that can be tricked into
#     vouching for the wrong box,
#   * a hardened systemd unit (read-only filesystem except the store, no
#     capabilities, memory cap),
#   * a firewall rule for the one port, if ufw is active.
#
# It never prints the token to a file, and it never starts the service for you:
# the unit is enabled, and `systemctl start trick-vault` is your decision.
set -eu

SRC="${1:-}"
PORT="${TRICK_VAULT_PORT:-8790}"
STORE=/var/lib/trick-vault
ETC=/etc/trick-vault
APP=/opt/trick-vault
USER_NAME=trickvault

if [ -z "$SRC" ] || [ ! -f "$SRC/pyproject.toml" ]; then
  echo "usage: sudo sh deploy/vault-server.sh /path/to/trick" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 2
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home "$APP" --shell /usr/sbin/nologin "$USER_NAME"
fi

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip openssl >/dev/null

mkdir -p "$APP" "$ETC" "$STORE"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet "$SRC[backup]"

if [ ! -f "$ETC/tls/cert.pem" ]; then
  mkdir -p "$ETC/tls"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$ETC/tls/key.pem" -out "$ETC/tls/cert.pem" -days 3650 \
    -subj "/CN=trick-vault" >/dev/null 2>&1
  chmod 600 "$ETC/tls/key.pem"
fi

if [ ! -f "$STORE/accounts.json" ]; then
  "$APP/venv/bin/python" -m trick vault init --root "$STORE" --account owner >"$ETC/init.json"
  chmod 600 "$ETC/init.json"
  echo
  echo "the store token, shown once (give this to the client, nowhere else):"
  "$APP/venv/bin/python" - "$ETC/init.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["token"])
PY
  echo
fi

chown -R "$USER_NAME:$USER_NAME" "$STORE" "$APP"
chmod 700 "$STORE"

cat >/etc/systemd/system/trick-vault.service <<UNIT
[Unit]
Description=Trick memory store (encrypted backups, nothing readable)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m trick vault serve \\
    --root $STORE --host 0.0.0.0 --port $PORT --expose-public \\
    --cert $ETC/tls/cert.pem --key $ETC/tls/key.pem
Restart=on-failure
RestartSec=3

# The store reads nothing but its own directory and writes nothing but it either.
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
ReadWritePaths=$STORE
# 512 MB box: the store itself needs ~30 MB, so a cap here is a guard against a
# runaways, not a budget. It is deliberately well below the instance size.
MemoryMax=192M

[Install]
WantedBy=multi-user.target
UNIT

# The unit binds 0.0.0.0, which the CLI refuses by default for a reason: a store
# with no user accounts must be reachable only through the firewall. Keep the
# guard honest by allowing exactly one port.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null
fi

systemctl daemon-reload
systemctl enable trick-vault >/dev/null

FINGERPRINT=$(openssl x509 -in "$ETC/tls/cert.pem" -noout -fingerprint -sha256 \
  | sed 's/.*=//; s/://g' | tr 'A-Z' 'a-z')

cat <<DONE

store ready, not started.

  start          systemctl start trick-vault
  logs           journalctl -u trick-vault -f
  certificate    $ETC/tls/cert.pem
  fingerprint    $FINGERPRINT

on the client, once:

  printf '%s' 'THE-TOKEN-PRINTED-ABOVE' > ~/.local/share/trick/vault.token
  chmod 600 ~/.local/share/trick/vault.token

  .venv/bin/python -m trick backup export memory.trk
  .venv/bin/python -m trick backup push memory.trk \\
      --remote https://THIS-HOST:$PORT --fingerprint $FINGERPRINT

If the fingerprint does not match, the client refuses the store. That is the
point: the hash is the identity, so a different box on the same address cannot
impersonate this one.
DONE
