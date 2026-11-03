#!/bin/sh
# Rent, wait for, or destroy the one box that holds sealed archives.
#
# WHAT THIS IS FOR
#   The remote memory store needs 512 MB of RAM, 20 GB of disk and a TLS port.
#   It runs no model, so Lightsail's cheapest plan ($5/month) is the correct
#   size and a bigger one is waste. This script is the repetitive part of that:
#   create the instance, allocate a static IP, attach it, open exactly one port
#   and give the box its first-boot script.
#
# WHAT THIS IS NOT
#   It is not billing. AWS has no metering or invoicing API for resale, so an
#   invoice is the payment provider's job and the price table is docs/PLANS.md.
#   The automatable half of the commercial layer - "this account is now on the
#   backup_plus plan" - is the signed webhook in docs/PLANS.md, not AWS.
#
# USAGE
#   sh deploy/lightsail.sh plan              # print the exact commands, run none
#   sh deploy/lightsail.sh status
#   TRICK_LIGHTSAIL_CONFIRM=yes sh deploy/lightsail.sh create
#   TRICK_LIGHTSAIL_CONFIRM=yes sh deploy/lightsail.sh destroy
#
# ENVIRONMENT
#   TRICK_LIGHTSAIL_NAME     instance name            (default trick-vault)
#   TRICK_LIGHTSAIL_REGION   AWS region                (default eu-central-1)
#   TRICK_LIGHTSAIL_PLAN     Lightsail bundle id       (default nano_3_0)
#   TRICK_LIGHTSAIL_SOURCE   Trick checkout to install (default: this repo)
#   TRICK_LIGHTSAIL_SSH_CIDR who may reach port 22     (default: no one)
#
# IAM
#   The identity needs exactly these actions, on these resources:
#
#     lightsail:CreateInstances        arn:aws:lightsail:*:*:Instance/*
#     lightsail:GetInstance            arn:aws:lightsail:*:*:Instance/*
#     lightsail:DeleteInstance         arn:aws:lightsail:*:*:Instance/*
#     lightsail:AllocateStaticIp       arn:aws:lightsail:*:*:StaticIp/*
#     lightsail:AttachStaticIp         arn:aws:lightsail:*:*:StaticIp/*
#     lightsail:ReleaseStaticIp        arn:aws:lightsail:*:*:StaticIp/*
#     lightsail:GetStaticIp            arn:aws:lightsail:*:*:StaticIp/*
#     lightsail:OpenInstancePublicPorts arn:aws:lightsail:*:*:Instance/*
#     lightsail:GetInstancePortStates  arn:aws:lightsail:*:*:Instance/*
#     lightsail:PutInstancePublicPorts arn:aws:lightsail:*:*:Instance/*
#
#   The publish-only credentials in ~/.config/hermes-secrets/aws-yayin.credentials
#   are deliberately NOT enough: Lightsail answers AccessDeniedException. That is
#   the correct outcome, not a bug to work around - a publishing key should not
#   be able to rent servers.
set -eu

NAME="${TRICK_LIGHTSAIL_NAME:-trick-vault}"
REGION="${TRICK_LIGHTSAIL_REGION:-eu-central-1}"
BUNDLE="${TRICK_LIGHTSAIL_PLAN:-nano_3_0}"
PORT="${TRICK_VAULT_PORT:-8790}"
SSH_CIDR="${TRICK_LIGHTSAIL_SSH_CIDR:-}"
BLUEPRINT="ubuntu_24_04"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${TRICK_LIGHTSAIL_SOURCE:-$HERE}"
IP_NAME="$NAME-ip"

ACTION="${1:-plan}"

if ! command -v aws >/dev/null 2>&1; then
  echo "lightsail: the aws CLI is not installed; nothing was changed" >&2
  exit 2
fi

aws_() { aws lightsail "$@" --region "$REGION"; }

print_plan() {
  cat <<EOF
region      $REGION
instance    $NAME  (blueprint $BLUEPRINT, bundle $BUNDLE)
static ip   $IP_NAME
open ports  8790/tcp${SSH_CIDR:+ and 22/tcp from $SSH_CIDR}
source tree $SOURCE

commands that would run, in this order:

  aws lightsail create-instances \\
      --region $REGION --instance-names $NAME --blueprint-id $BLUEPRINT \\
      --bundle-id $BUNDLE --availability-zone ${REGION}a \\
      --user-data file://<rendered cloud-init>

  aws lightsail allocate-static-ip --region $REGION --static-ip-name $IP_NAME
  aws lightsail attach-static-ip --region $REGION --static-ip-name $IP_NAME --instance-name $NAME
  aws lightsail put-instance-public-ports --region $REGION --instance-name $NAME \\
      --port-infos fromPort=8790,toPort=8790,protocol=tcp${SSH_CIDR:+,fromPort=22,toPort=22,protocol=tcp,cidrs=$SSH_CIDR}

nothing was run. add TRICK_LIGHTSAIL_CONFIRM=yes to actually create it.
EOF
}

require_confirm() {
  if [ "${TRICK_LIGHTSAIL_CONFIRM:-}" != "yes" ]; then
    echo "lightsail: refusing to $ACTION without TRICK_LIGHTSAIL_CONFIRM=yes (this spends money)" >&2
    exit 2
  fi
}

render_cloud_init() {
  # The box installs the store from the source tree it is given. A first boot
  # that fails must not look like a working server: the unit is enabled, and the
  # script's own output is left in /var/log/trick-vault-install.log.
  cat <<EOF
#cloud-config
package_update: true
packages: [git, python3, python3-venv]
write_files:
  - path: /root/install-trick.sh
    permissions: '0700'
    content: |
      #!/bin/sh
      set -eu
      git clone --depth 1 https://github.com/hakkisabah/trick /opt/trick-src
      sh /opt/trick-src/deploy/vault-server.sh /opt/trick-src > /var/log/trick-vault-install.log 2>&1
runcmd:
  - [ sh, /root/install-trick.sh ]
EOF
}

case "$ACTION" in
  plan)
    print_plan
    ;;
  status)
    aws_ get-instance --instance-name "$NAME" \
      --query 'instance.{name:name,state:state.name,ip:publicIpAddress,blueprint:blueprintId,bundle:bundleId}' \
      --output json
    ;;
  create)
    require_confirm
    if aws_ get-instance --instance-name "$NAME" >/dev/null 2>&1; then
      echo "lightsail: $NAME already exists; run 'status' or 'destroy' first" >&2
      exit 1
    fi
    TMPDIR_CI="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_CI"' EXIT
    render_cloud_init > "$TMPDIR_CI/cloud-init.yaml"
    aws_ create-instances \
      --instance-names "$NAME" \
      --blueprint-id "$BLUEPRINT" \
      --bundle-id "$BUNDLE" \
      --availability-zone "${REGION}a" \
      --user-data "file://$TMPDIR_CI/cloud-init.yaml" >/dev/null
    aws_ allocate-static-ip --static-ip-name "$IP_NAME" >/dev/null 2>&1 || true
    aws_ attach-static-ip --static-ip-name "$IP_NAME" --instance-name "$NAME" >/dev/null
    if [ -n "$SSH_CIDR" ]; then
      aws_ put-instance-public-ports --instance-name "$NAME" --port-infos \
        "fromPort=$PORT,toPort=$PORT,protocol=tcp" \
        "fromPort=22,toPort=22,protocol=tcp,cidrs=$SSH_CIDR" >/dev/null
    else
      aws_ put-instance-public-ports --instance-name "$NAME" --port-infos \
        "fromPort=$PORT,toPort=$PORT,protocol=tcp" >/dev/null
    fi
    echo "lightsail: $NAME created; waiting for it to run"
    for _ in $(seq 1 60); do
      STATE="$(aws_ get-instance --instance-name "$NAME" --query 'instance.state.name' --output text)"
      [ "$STATE" = "running" ] && break
      sleep 5
    done
    IP="$(aws_ get-static-ip --static-ip-name "$IP_NAME" --query 'staticIp.ipAddress' --output text)"
    echo "lightsail: running at $IP"
    echo "next: read the token on the box, then point the client at"
    echo "      https://$IP:$PORT  with the fingerprint printed by the installer"
    ;;
  destroy)
    require_confirm
    aws_ delete-instance --instance-name "$NAME" >/dev/null 2>&1 || true
    aws_ release-static-ip --static-ip-name "$IP_NAME" >/dev/null 2>&1 || true
    echo "lightsail: $NAME and $IP_NAME are gone; the archives on it are gone too"
    ;;
  *)
    echo "usage: sh deploy/lightsail.sh [plan|status|create|destroy]" >&2
    exit 2
    ;;
esac
