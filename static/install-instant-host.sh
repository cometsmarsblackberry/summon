#!/usr/bin/env bash
set -euo pipefail
umask 077

SERVICE_USER="summon-agent"
STATE_DIR="/var/lib/summon-agent"
SERVICE_FILE="/etc/systemd/system/summon-host-agent.service"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run this installer as root (for example: curl ... | sudo bash)." >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this operating system." >&2
  exit 1
fi
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "26.04" ]]; then
  echo "Instant host v1 requires Ubuntu 26.04 LTS; found ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Instant host v1 requires AMD64." >&2
  exit 1
fi

SUMMON_URL="${SUMMON_URL:-}"
if [[ -z "$SUMMON_URL" ]]; then
  read -r -p "Summon URL (for example https://summon.example): " SUMMON_URL </dev/tty
fi
SUMMON_URL="${SUMMON_URL%/}"
if [[ "$SUMMON_URL" != https://* && "$SUMMON_URL" != http://localhost* && "$SUMMON_URL" != http://127.0.0.1* ]]; then
  echo "Summon URL must use HTTPS (HTTP is accepted only for local testing)." >&2
  exit 1
fi

ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN:-}"
if [[ -z "$ENROLLMENT_TOKEN" ]]; then
  read -r -s -p "Enrollment token: " ENROLLMENT_TOKEN </dev/tty
  echo >/dev/tty
fi
if [[ -z "$ENROLLMENT_TOKEN" ]]; then
  echo "Enrollment token is required." >&2
  exit 1
fi
# The copied command supplies this as an environment variable. Keep it out of
# apt, user-management, and other child-process environments until enrollment.
export -n ENROLLMENT_TOKEN

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl jq podman passt uidmap slirp4netns fuse-overlayfs dbus-user-session iproute2

if ! command -v pasta >/dev/null 2>&1; then
  echo "Podman rootless networking requires the pasta executable from the passt package." >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# System accounts do not consistently receive subordinate ID ranges on Ubuntu,
# but rootless Podman requires them. Allocate the next non-overlapping block.
if ! grep -q "^${SERVICE_USER}:" /etc/subuid; then
  LAST_SUBUID="$(awk -F: 'BEGIN { max=99999 } { end=$2+$3-1; if (end>max) max=end } END { print max }' /etc/subuid)"
  SUBUID_START="$(( ((LAST_SUBUID + 65536) / 65536) * 65536 ))"
  SUBUID_END="$(( SUBUID_START + 65535 ))"
  usermod --add-subuids "${SUBUID_START}-${SUBUID_END}" "$SERVICE_USER"
fi
if ! grep -q "^${SERVICE_USER}:" /etc/subgid; then
  LAST_SUBGID="$(awk -F: 'BEGIN { max=99999 } { end=$2+$3-1; if (end>max) max=end } END { print max }' /etc/subgid)"
  SUBGID_START="$(( ((LAST_SUBGID + 65536) / 65536) * 65536 ))"
  SUBGID_END="$(( SUBGID_START + 65535 ))"
  usermod --add-subgids "${SUBGID_START}-${SUBGID_END}" "$SERVICE_USER"
fi
SERVICE_UID="$(id -u "$SERVICE_USER")"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR" "$STATE_DIR/slots" "$STATE_DIR/bin"

REPORTED_HOSTNAME="$(hostname -s 2>/dev/null || true)"
REPORTED_HOSTNAME="$(printf '%s' "$REPORTED_HOSTNAME" | tr -cd '[:alnum:]._-')"
REPORTED_HOSTNAME="${REPORTED_HOSTNAME:0:63}"
ENROLL_RESPONSE="$(
  printf '{"token":"%s","hostname":"%s"}' "$ENROLLMENT_TOKEN" "$REPORTED_HOSTNAME" |
    curl --ipv4 --fail --silent --show-error \
      -H 'Content-Type: application/json' \
      --data-binary @- "$SUMMON_URL/internal/instant-hosts/enroll"
)"
unset ENROLLMENT_TOKEN

HOST_ID="$(printf '%s' "$ENROLL_RESPONSE" | jq -er '.host_id')"
CREDENTIAL="$(printf '%s' "$ENROLL_RESPONSE" | jq -er '.credential')"
WEBSOCKET_URL="$(printf '%s' "$ENROLL_RESPONSE" | jq -er '.websocket_url')"
AGENT_URL="$(printf '%s' "$ENROLL_RESPONSE" | jq -er '.agent_url')"
AGENT_SHA256="$(printf '%s' "$ENROLL_RESPONSE" | jq -er '.agent_sha256')"

install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$STATE_DIR/credential"
printf '%s' "$CREDENTIAL" >"$STATE_DIR/credential"

PORT_PREFLIGHT_OK=true
mapfile -t REQUIRED_PORTS < <(
  printf '%s' "$ENROLL_RESPONSE" |
    jq -r '.slots[] | .game_port, .tv_port' |
    sort -n -u
)
for port in "${REQUIRED_PORTS[@]}"; do
  if ss -H -lnu "sport = :$port" | grep -q .; then
    echo "Preflight: port $port is already in use; the host will remain unavailable." >&2
    PORT_PREFLIGHT_OK=false
  fi
done

curl --fail --location --retry 5 --retry-connrefused \
  --output "$STATE_DIR/bin/tf2-agent.download" "$AGENT_URL"
if ! printf '%s  %s\n' "$AGENT_SHA256" "$STATE_DIR/bin/tf2-agent.download" | sha256sum --check --status; then
  DOWNLOADED_SHA256="$(sha256sum "$STATE_DIR/bin/tf2-agent.download" | awk '{print $1}')"
  echo "Agent download failed integrity verification." >&2
  echo "  Expected SHA-256:   $AGENT_SHA256" >&2
  echo "  Downloaded SHA-256: $DOWNLOADED_SHA256" >&2
  echo "  Downloaded file:    $STATE_DIR/bin/tf2-agent.download" >&2
  echo "The agent service was not created. A stale proxy/CDN cache or a corrupted download may be responsible." >&2
  echo "Purge the cached agent, request a new enrollment token, and run this installer again." >&2
  exit 1
fi
chmod 0755 "$STATE_DIR/bin/tf2-agent.download"
chown "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR/bin/tf2-agent.download"
mv "$STATE_DIR/bin/tf2-agent.download" "$STATE_DIR/bin/tf2-agent"

loginctl enable-linger "$SERVICE_USER" || true
mkdir -p "/run/user/$SERVICE_UID"
chown "$SERVICE_USER:$SERVICE_USER" "/run/user/$SERVICE_UID"
chmod 0700 "/run/user/$SERVICE_UID"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Summon persistent Instant host agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$STATE_DIR
Environment=AGENT_MODE=host
Environment=BACKEND_URL=$WEBSOCKET_URL
Environment=HOST_ID=$HOST_ID
Environment=CREDENTIAL_FILE=$STATE_DIR/credential
Environment=HOST_STATE_DIR=$STATE_DIR
Environment=XDG_RUNTIME_DIR=/run/user/$SERVICE_UID
ExecStartPre=+/usr/bin/install -d -m 0700 -o $SERVICE_USER -g $SERVICE_USER /run/user/$SERVICE_UID
ExecStart=$STATE_DIR/bin/tf2-agent
Restart=always
RestartSec=5
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now summon-host-agent.service

echo
echo "Summon Instant host $HOST_ID enrolled. The host remains disabled until an admin enables it."
echo "The installer did not change your firewall. Required connectivity:"
echo "  outbound: HTTPS/WSS (TCP 443) to $SUMMON_URL"
if printf '%s' "$ENROLL_RESPONSE" | jq -e '.slots | length > 0' >/dev/null 2>&1; then
  printf '%s' "$ENROLL_RESPONSE" | jq -r '.slots[] | "  inbound: UDP \(.game_port) and UDP \(.tv_port)  (ufw: sudo ufw allow \(.game_port)/udp; sudo ufw allow \(.tv_port)/udp)"'
else
  echo "  inbound: each game and SourceTV UDP port shown in the Summon admin panel"
fi
echo
echo "Check status with: systemctl status summon-host-agent.service"
if [[ "$PORT_PREFLIGHT_OK" != true ]]; then
  echo "Resolve the port conflicts above, then restart summon-host-agent.service." >&2
  exit 1
fi
