#!/bin/bash
# deploy.sh - One-command deployment for Summon
# Usage: ./deploy.sh [--bunny] [--ref <git-ref>] [vps-user@vps-ip]

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
DEPLOY_REF=""
while [ $# -gt 0 ]; do
    case "$1" in
        --bunny) CDN_PROVIDER="bunny" ;;
        --ref) DEPLOY_REF="$2"; shift ;;
        *) VPS_ARG="$1" ;;
    esac
    shift
done

build_agent() {
    local src_dir="$1"
    info "Building agent binary..."
    if ! command -v go &> /dev/null; then
        error "Go is required to build the agent. Install it from https://go.dev"
    fi
    (cd "$src_dir/agent" && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o ../static/tf2-agent .)
    info "Agent binary built: $src_dir/static/tf2-agent"
}

generate_version_file() {
    local dir="$1"
    local ref="${2:-HEAD}"
    cat > "$dir/.version" <<EOF
commit=$(git rev-parse --short "$ref")
describe=$(git describe --tags --always --dirty 2>/dev/null || git rev-parse --short "$ref")
date=$(date -Iseconds)
EOF
    info "Version: $(grep '^describe=' "$dir/.version" | cut -d= -f2)"
}

# CDN-specific Caddy config
if [ "$CDN_PROVIDER" = "bunny" ]; then
    info "Bunny CDN mode enabled"
    export CADDY_DOCKERFILE="Dockerfile.caddy.bunny"
    export CADDYFILE="./Caddyfile.bunny"
fi

# Set up source directory — either a worktree for --ref or the repo root
REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_DIR="$REPO_ROOT"
WORKTREE_DIR=""

if [ -n "$DEPLOY_REF" ]; then
    # Validate the ref exists
    if ! git rev-parse --verify "$DEPLOY_REF" &>/dev/null; then
        error "Git ref '$DEPLOY_REF' not found. Use a branch, tag, or commit hash."
    fi
    WORKTREE_DIR=$(mktemp -d "/tmp/summon-deploy-XXXXXX")
    SRC_DIR="$WORKTREE_DIR"
    info "Deploying ref '$DEPLOY_REF' via worktree at $WORKTREE_DIR"
    git worktree add --detach "$WORKTREE_DIR" "$DEPLOY_REF"
    # Clean up worktree on exit
    trap 'info "Cleaning up worktree..."; git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"' EXIT
fi

# Check if deploying remotely or locally
if [ -n "$VPS_ARG" ]; then
    # Remote deployment
    VPS="$VPS_ARG"
    info "Deploying to $VPS..."
    SSH_CTL_PATH="/tmp/summon-ssh-%r@%h-%p"
    SSH_CTL_OPTS="-o ControlMaster=auto -o ControlPersist=300 -o ControlPath=$SSH_CTL_PATH"

    # Build Go agent binary (runs on game server instances, not in Docker)
    if [ -n "$DEPLOY_REF" ]; then
        # Always build when deploying a specific ref
        build_agent "$SRC_DIR"
    elif [ "${FORCE_AGENT_BUILD:-0}" = "1" ]; then
        build_agent "$SRC_DIR"
    elif [ ! -f "$SRC_DIR/static/tf2-agent" ]; then
        build_agent "$SRC_DIR"
    elif find "$SRC_DIR/agent" -type f -newer "$SRC_DIR/static/tf2-agent" -print -quit | read -r _; then
        build_agent "$SRC_DIR"
    else
        info "Agent binary up to date; skipping rebuild."
    fi

    # Generate .version file
    generate_version_file "$SRC_DIR" "${DEPLOY_REF:-HEAD}"

    # Sync files to VPS (excluding local-only files)
    info "Syncing files (showing progress)..."
    RSYNC_SSH_OPTS="-o ServerAliveInterval=30 -o ServerAliveCountMax=10 $SSH_CTL_OPTS"
    rsync -avz --progress --partial --timeout=120 -e "ssh $RSYNC_SSH_OPTS" \
        --exclude 'venv' --exclude '__pycache__' --exclude '/data' --exclude '.git' --exclude '.env' --exclude '.claude' \
        "$SRC_DIR/" "$VPS:/opt/summon/"

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
    generate_version_file "$SRC_DIR" "${DEPLOY_REF:-HEAD}"
    info "Building Docker image locally..."
    docker build -t summon "$SRC_DIR"
    info "To deploy, run: ./deploy.sh user@your-vps-ip"
fi
