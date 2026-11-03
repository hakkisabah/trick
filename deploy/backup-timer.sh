#!/bin/sh
# Install the daily memory backup as a systemd --user timer.
#
#   sh deploy/backup-timer.sh --remote https://host:8790 --fingerprint SHA256
#       writes the unit and the environment file; starts nothing
#
#   sh deploy/backup-timer.sh --remote ... --fingerprint ... --start
#       also enables the timer and runs the service once, so a broken setup is
#       discovered now rather than at three in the morning
#
# A backup that only happens when someone remembers it is not a backup, which is
# the whole reason this exists. The passphrase is not invented here and is not
# accepted on the command line: it belongs in a 0600 environment file, because an
# argument is visible in `ps` and in the shell history.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="$REPO/.venv/bin/python"
HOME_DIR="${TRICK_HOME:-$HOME/.local/share/trick}"
ENV_DIR="$HOME/.config/trick"
ENV_FILE="$ENV_DIR/backup.env"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="$UNIT_DIR/trick-backup.service"
TIMER="$UNIT_DIR/trick-backup.timer"
REMOTE=""
FINGERPRINT=""
AT="03:30"
START=0
DRY=0

for arg in "$@"; do
	case "$arg" in
	--remote=*) REMOTE=${arg#--remote=} ;;
	--fingerprint=*) FINGERPRINT=${arg#--fingerprint=} ;;
	--at=*) AT=${arg#--at=} ;;
	--start) START=1 ;;
	--dry-run) DRY=1 ;;
	-h | --help)
		sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "unknown argument: $arg" >&2
		echo "usage: sh deploy/backup-timer.sh --remote=https://host:8790 --fingerprint=SHA256 [--at=03:30] [--start]" >&2
		exit 2
		;;
	esac
done

if [ -z "$REMOTE" ] || [ -z "$FINGERPRINT" ]; then
	echo "backup-timer: --remote and --fingerprint are both required" >&2
	echo "the fingerprint is printed by deploy/vault-server.sh on the store" >&2
	exit 2
fi

case "$REMOTE" in
https://*) ;;
*)
	echo "backup-timer: --remote must start with https://, got '$REMOTE'" >&2
	exit 2
	;;
esac

if [ ! -x "$PY" ]; then
	echo "backup-timer: $PY not found; run step 1 of docs/INSTALL.md first" >&2
	exit 2
fi
if [ ! -f "$HOME_DIR/vault.token" ]; then
	echo "backup-timer: $HOME_DIR/vault.token is missing" >&2
	echo "write the store token there with mode 600 first (docs/REMOTE.md)" >&2
	exit 2
fi

mkdir -p "$ENV_DIR" "$UNIT_DIR"
chmod 700 "$ENV_DIR"

if [ ! -f "$ENV_FILE" ]; then
	umask 077
	{
		echo "# Trick backup environment, read by trick-backup.service."
		echo "# TRICK_BACKUP_PASSPHRASE is deliberately left empty: fill it in, or the"
		echo "# service refuses to run rather than write a backup nobody can open."
		echo "TRICK_BACKUP_PASSPHRASE="
		echo "TRICK_VAULT_URL=$REMOTE"
		echo "TRICK_VAULT_FINGERPRINT=$FINGERPRINT"
	} >"$ENV_FILE"
	chmod 600 "$ENV_FILE"
fi

cat >"$SERVICE" <<UNIT
[Unit]
Description=Trick memory backup: export the decision cache and push it

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=$PY -m trick backup run --home $HOME_DIR
UNIT

cat >"$TIMER" <<UNIT
[Unit]
Description=Trick memory backup, daily

[Timer]
OnCalendar=*-*-* $AT:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
UNIT

if [ "$DRY" -eq 1 ]; then
	echo "would write:"
	echo "  $SERVICE"
	echo "  $TIMER"
	echo "  $ENV_FILE"
	cat "$SERVICE"
	cat "$TIMER"
	exit 0
fi

systemctl --user daemon-reload

if ! grep -q '^TRICK_BACKUP_PASSPHRASE=.\+' "$ENV_FILE"; then
	cat >&2 <<MSG

backup-timer: units written, nothing enabled.

The passphrase is still empty in $ENV_FILE. Put it there (mode 600):

  printf 'TRICK_BACKUP_PASSPHRASE=%s\n' 'your passphrase' >> $ENV_FILE

then run this again with --start. The timer is not enabled yet on purpose: a
timer that fires with no passphrase would fail every night and still look
scheduled.

MSG
	exit 0
fi

systemctl --user enable trick-backup.timer >/dev/null

if [ "$START" -eq 1 ]; then
	systemctl --user start trick-backup.timer
	echo "running the service once, to prove the setup before trusting the schedule"
	if ! systemctl --user start --wait trick-backup.service; then
		echo "backup-timer: the first run failed; see: journalctl --user -u trick-backup.service" >&2
		exit 1
	fi
	echo "first backup ok"
fi

echo "installed:"
echo "  $SERVICE"
echo "  $TIMER"
echo "next: systemctl --user start trick-backup.timer"
