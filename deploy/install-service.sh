#!/bin/sh
# Install the Trick bridge as a systemd --user service.
#
#	writes the unit and reloads systemd; starts nothing
#	sh deploy/install-service.sh --start
#	writes, enables and starts it now
#
# Everything machine-specific (repository path, python path, docker bridge
# address) is resolved here instead of being hard-coded into a unit file that
# would be wrong on the next machine.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/trick-bridge.service"
PORT=8765
START=0
DRY=0

for arg in "$@"; do
	case "$arg" in
	--start) START=1 ;;
	--dry-run) DRY=1 ;;
	--port=*) PORT=${arg#--port=} ;;
	-h | --help)
		sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "unknown argument: $arg" >&2
		exit 2
		;;
	esac
done

case "$PORT" in
'' | *[!0-9]*)
	echo "install-service: --port must be a number, got '$PORT'" >&2
	exit 2
	;;
esac

if [ ! -x "$PY" ]; then
	echo "install-service: $PY not found" >&2
	echo "run step 1 of docs/INSTALL.md first:" >&2
	echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
	exit 2
fi

# A container cannot reach the host loopback, so when Docker is present bind the
# docker bridge address. The bridge itself refuses a wildcard bind.
BIND=127.0.0.1
EXPOSE=""
if command -v ip >/dev/null 2>&1; then
	ADDR=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
	if [ -n "${ADDR:-}" ]; then
		BIND=$ADDR
		EXPOSE="--expose-network"
	fi
fi

if [ "$DRY" -eq 1 ]; then
	UNIT=/dev/stdout
else
	mkdir -p "$UNIT_DIR"
fi
cat >"$UNIT" <<UNITEOF
[Unit]
Description=Trick core service (unix socket + HTTP bridge)
Documentation=file:$REPO/docs/INSTALL.md
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
Environment=PYTHONPATH=$REPO/src
ExecStart=$PY -m trick serve --socket $HOME/.local/share/trick/run/trick.sock --http-host $BIND $EXPOSE --http-port $PORT
Restart=on-failure
RestartSec=2
# The service has no accounts: it must never be reachable from the LAN.
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
UNITEOF

if [ "$DRY" -eq 1 ]; then
	exit 0
fi

systemctl --user daemon-reload

echo "wrote $UNIT"
echo "bind: $BIND:$PORT $EXPOSE"
if [ "$START" -eq 1 ]; then
	systemctl --user enable --now trick-bridge.service
	sleep 1
	systemctl --user --no-pager status trick-bridge.service || true
	echo "token file: $HOME/.local/share/trick/bridge.token"
else
	echo "not started. to start:"
	echo "  systemctl --user enable --now trick-bridge.service"
fi
