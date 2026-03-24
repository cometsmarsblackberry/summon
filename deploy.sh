#!/bin/bash
# deploy.sh - One-command deployment for Summon
# Usage: ./deploy.sh [--bunny] [vps-user@vps-ip]

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Parse flags
CDN_PROVIDER=""
VPS_ARG=""
for arg in "$@"; do
    case "$arg" in
        --bunny) CDN_PROVIDER="bunny" ;;
        *) VPS_ARG="$arg" ;;
    esac
done

build_agent() {
    info "Building agent binary..."
    if ! command -v go &> /dev/null; then
        error "Go is required to build the agent. Install it from https://go.dev"
    fi
    (cd agent && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o ../static/tf2-agent .)
    info "Agent binary built: static/tf2-agent"
}

# CDN-specific Caddy config
if [ "$CDN_PROVIDER" = "bunny" ]; then
    info "Bunny CDN mode enabled"
    export CADDY_DOCKERFILE="Dockerfile.caddy.bunny"
    export CADDYFILE="./Caddyfile.bunny"
fi

# Check if deploying remotely or locally
if [ -n "$VPS_ARG" ]; then
    # Remote deployment
    VPS="$VPS_ARG"
    info "Deploying to $VPS..."
    SSH_CTL_PATH="/tmp/summon-ssh-%r@%h-%p"
    SSH_CTL_OPTS="-o ControlMaster=auto -o ControlPersist=300 -o ControlPath=$SSH_CTL_PATH"

    # Build Go agent binary (runs on game server instances, not in Docker)
    if [ "${FORCE_AGENT_BUILD:-0}" = "1" ]; then
        build_agent
    elif [ ! -f static/tf2-agent ]; then
        build_agent
    elif find agent -type f -newer static/tf2-agent -print -quit | read -r _; then
        build_agent
    else
        info "Agent binary up to date; skipping rebuild."
    fi

    # Sync files to VPS (excluding local-only files)
    info "Syncing files (showing progress)..."
    RSYNC_SSH_OPTS="-o ServerAliveInterval=30 -o ServerAliveCountMax=10 $SSH_CTL_OPTS"
    rsync -avz --progress --partial --timeout=120 -e "ssh $RSYNC_SSH_OPTS" \
        --exclude 'venv' --exclude '__pycache__' --exclude '/data' --exclude '.git' --exclude '.env' --exclude '.claude' \
        ./ "$VPS:/opt/summon/"
    
    # Run remote setup
    info "Setting up on VPS..."
    ssh $RSYNC_SSH_OPTS "$VPS" "cd /opt/summon && CDN_PROVIDER='$CDN_PROVIDER' bash -s" << 'REMOTE_SCRIPT'
        set -e

        # Configure CDN-specific Caddy build
        if [ "$CDN_PROVIDER" = "bunny" ]; then
            export CADDY_DOCKERFILE="Dockerfile.caddy.bunny"
            export CADDYFILE="./Caddyfile.bunny"
        fi

        # Install Docker if not present
        if ! command -v docker &> /dev/null; then
            echo "Installing Docker..."
            curl -fsSL https://get.docker.com | sh
            sudo systemctl enable docker
            sudo systemctl start docker
        fi
        
        # Install Docker Compose plugin if not present
        if ! docker compose version &> /dev/null; then
            echo "Installing Docker Compose..."
            sudo apt-get update
            sudo apt-get install -y docker-compose-plugin
        fi
        
        # Create data directory (UID 65532 = appuser inside container)
        mkdir -p data/logs
        chown -R 65532:65532 data
        
        # Check for .env
        if [ ! -f .env ]; then
            echo ""
            echo "⚠️  No .env file found!"
            echo "Create one with:"
            echo "  cp .env.example .env"
            echo "  nano .env  # Add your API keys"
            echo ""
        fi
        
        # Build and start (force recreate to apply config changes)
        echo "Building and starting..."
        docker compose -f docker-compose.prod.yml build
        docker compose -f docker-compose.prod.yml up -d --force-recreate

        # Clean up old images and build cache
        echo "Cleaning up old images..."
        docker image prune -f

        # Install CDN firewall if configured
        if [ "$CDN_PROVIDER" = "bunny" ]; then
            echo "Installing Bunny CDN firewall..."
            chmod +x scripts/bunny-firewall.sh
            cp scripts/bunny-firewall.service scripts/bunny-firewall.timer /etc/systemd/system/
            systemctl daemon-reload
            systemctl enable --now bunny-firewall.service bunny-firewall.timer
        fi

        echo ""
        echo "✅ Deployment complete!"
        echo "   Check logs: docker compose -f docker-compose.prod.yml logs -f"
REMOTE_SCRIPT

else
    # Local build/test
    info "Building Docker image locally..."
    docker build -t summon .
    info "To deploy, run: ./deploy.sh user@your-vps-ip"
fi
