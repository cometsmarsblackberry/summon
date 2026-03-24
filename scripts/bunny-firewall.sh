#!/bin/bash
# bunny-firewall.sh - Restrict inbound HTTP/HTTPS access to Docker-published ports
# to Bunny CDN edge servers only.
# Updates iptables DOCKER-USER chain with current Bunny edge IPs.
# Designed to run via systemd timer.

set -euo pipefail

BUNNY_API="https://bunnycdn.com/api/system/edgeserverlist"
CHAIN="DOCKER-USER"
COMMENT="bunny-cdn"
CACHE="/var/cache/bunny-firewall-ips.json"

log() { echo "[$(date -Iseconds)] $1"; }

# Fetch current edge server list
log "Fetching Bunny CDN edge server list..."
if ! ips_json=$(curl -sf --max-time 30 "$BUNNY_API"); then
    log "ERROR: Failed to fetch Bunny IP list"
    exit 1
fi

# Parse JSON array into newline-separated IPs
ips=$(echo "$ips_json" | tr -d '[]" \n' | tr ',' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')

count=$(echo "$ips" | wc -l)
if [ "$count" -lt 10 ]; then
    log "ERROR: Only got $count IPs — refusing to apply (likely bad response)"
    exit 1
fi

# Discover Docker bridge interfaces. Matching on egress interface keeps the
# rules scoped to inbound traffic headed to containers, without breaking
# outbound HTTPS from the containers themselves.
mapfile -t docker_ifaces < <(
    ip -o link show \
    | awk -F': ' '{print $2}' \
    | grep -E '^(docker0|br-)'
)

if [ "${#docker_ifaces[@]}" -eq 0 ]; then
    log "ERROR: No Docker bridge interfaces found"
    exit 1
fi

log "Applying $count Bunny CDN IPs to $CHAIN for interfaces: ${docker_ifaces[*]}"

# Remove old bunny rules from DOCKER-USER
while true; do
    num=$(
        iptables -L "$CHAIN" -n --line-numbers \
        | awk -v comment="$COMMENT" '$0 ~ comment { print $1; exit }'
    )
    [ -n "$num" ] || break
    iptables -D "$CHAIN" "$num"
done

# Build new rules: insert ACCEPT for each Bunny IP on ports 80/443,
# then append DROP for all other inbound traffic on those ports headed to
# Docker bridge interfaces.
# Insert in reverse order so they end up in the correct sequence.
# The DROP rules go at the end of the chain (before the final RETURN).

# First, add the DROP rules at the end (before RETURN)
for iface in "${docker_ifaces[@]}"; do
    iptables -I "$CHAIN" -o "$iface" -p tcp --dport 443 -j DROP -m comment --comment "$COMMENT"
    iptables -I "$CHAIN" -o "$iface" -p tcp --dport 80 -j DROP -m comment --comment "$COMMENT"
done

# Then insert all ACCEPT rules before the DROPs (insert at position 1 pushes others down)
for iface in "${docker_ifaces[@]}"; do
    echo "$ips" | while read -r ip; do
        iptables -I "$CHAIN" -s "$ip" -o "$iface" -p tcp -m multiport --dports 80,443 -j ACCEPT -m comment --comment "$COMMENT"
    done
done

# Save cache for debugging/inspection; rules are always reconciled on each run
# so script changes and stale iptables state cannot get stuck behind a cache hit.
echo "$ips_json" > "$CACHE"

log "Done — $count Bunny CDN IPs allowed on ports 80,443 to Docker bridges; other inbound traffic blocked"
