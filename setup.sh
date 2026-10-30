#!/bin/bash
set -euo pipefail

# =============================================================================
# Media Server Setup Script — Run INSIDE the LXC container
#
# Installs Docker, creates directory structure, and launches the full stack:
#   Jellyfin (CPU), qBittorrent, Prowlarr, Radarr, Sonarr, Bazarr, Jellyseerr
#
# The GPU belongs to the `llm` container (docs/runbook.md §2), so the NVIDIA
# Container Toolkit is installed only when a GPU was passed in on purpose
# (host-setup.sh with WITH_GPU=1).
#
# Usage: sudo bash setup.sh
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SERVER_IP="192.168.1.216"

# --- Pre-flight checks ---
if [ "$EUID" -ne 0 ]; then
    err "Please run as root: sudo bash setup.sh"
fi

# The containers run as PUID/PGID. Run through `lxc exec` there is no SUDO_USER
# and $USER is root, which would hand every service (the torrent client among
# them) uid 0 — so fall back to a dedicated unprivileged account instead.
REAL_USER="${SUDO_USER:-${USER:-root}}"
if [ "$REAL_USER" = "root" ]; then
    REAL_USER="media"
    if ! id -u "$REAL_USER" &> /dev/null; then
        useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$REAL_USER"
        log "Created system user '$REAL_USER' for the containers"
    fi
fi
REAL_UID=$(id -u "$REAL_USER")
REAL_GID=$(id -g "$REAL_USER")

echo ""
echo "========================================"
echo " Media Server Setup (LXC + NVIDIA)"
echo " User: $REAL_USER (UID=$REAL_UID, GID=$REAL_GID)"
echo " IP:   $SERVER_IP"
echo "========================================"
echo ""

# --- Step 1: Is there a GPU on purpose? ---
echo "--- Step 1: Checking for a passed-through GPU ---"

HAS_GPU=false
if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    HAS_GPU=true
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    warn "NVIDIA GPU visible: $GPU_NAME — installing the container toolkit for it."
    warn "Policy is no GPU here (docs/runbook.md §2); Jellyfin still runs without it."
else
    log "No GPU in this container — Jellyfin transcodes on CPU (expected)."
fi

# --- Step 2: Install Docker ---
echo ""
echo "--- Step 2: Installing Docker ---"

if command -v docker &> /dev/null; then
    log "Docker already installed: $(docker --version)"
else
    apt-get update
    apt-get install -y ca-certificates curl gnupg

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Only a person who ran sudo gets the docker group (= root); never the
    # service account the containers run as.
    if [ -n "${SUDO_USER:-}" ]; then
        usermod -aG docker "$SUDO_USER" 2>/dev/null || true
    fi
    log "Docker installed."
fi

# Verify docker compose
if ! docker compose version &> /dev/null; then
    err "docker compose not available. Please install docker-compose-plugin."
fi
log "Docker Compose: $(docker compose version --short)"

if [ "$HAS_GPU" = true ]; then
    # --- Step 3: Install NVIDIA Container Toolkit ---
    echo ""
    echo "--- Step 3: Installing NVIDIA Container Toolkit ---"

    # dpkg -s, not `dpkg -l | grep -q`: under pipefail grep -q closing the pipe early
    # fails the pipeline exactly when the package is there.
    if dpkg -s nvidia-container-toolkit &> /dev/null; then
        log "NVIDIA Container Toolkit already installed"
    else
        # Add NVIDIA container toolkit repo
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
            gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

        curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

        apt-get update
        apt-get install -y nvidia-container-toolkit
        log "NVIDIA Container Toolkit installed"
    fi

    # Configure nvidia-container-runtime for LXC (no-cgroups required for unprivileged containers)
    NVIDIA_CONFIG="/etc/nvidia-container-runtime/config.toml"
    if [ -f "$NVIDIA_CONFIG" ]; then
        if grep -q "^no-cgroups" "$NVIDIA_CONFIG"; then
            sed -i 's/^no-cgroups.*/no-cgroups = true/' "$NVIDIA_CONFIG"
        elif grep -q "#no-cgroups" "$NVIDIA_CONFIG"; then
            sed -i 's/#no-cgroups.*/no-cgroups = true/' "$NVIDIA_CONFIG"
        else
            echo "no-cgroups = true" >> "$NVIDIA_CONFIG"
        fi
    else
        mkdir -p /etc/nvidia-container-runtime
        cat > "$NVIDIA_CONFIG" << 'TOML'
[nvidia-container-cli]
no-cgroups = true
TOML
    fi
    log "NVIDIA Container Runtime configured (no-cgroups = true for LXC)"

    # Register nvidia runtime with Docker
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
    log "Docker configured with NVIDIA runtime"

    # --- Step 4: Verify Docker can see GPU ---
    echo ""
    echo "--- Step 4: Testing Docker GPU access ---"

    if docker run --rm --gpus all nvidia/cuda:12.6.1-base-ubuntu24.04 nvidia-smi &> /dev/null; then
        log "Docker GPU access verified!"
    else
        warn "Docker GPU test failed. Trying alternative CUDA image..."
        if docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all nvidia/cuda:12.6.1-base-ubuntu24.04 nvidia-smi; then
            log "Docker GPU access verified (via runtime flag)!"
        else
            warn "GPU test container failed. Jellyfin may still work — continuing setup."
            warn "You can debug later with: docker run --rm --gpus all nvidia/cuda:12.6.1-base-ubuntu24.04 nvidia-smi"
        fi
    fi
fi

# --- Step 5: Create directory structure ---
echo ""
echo "--- Step 5: Creating directory structure ---"

BASE="/opt/media-stack"

mkdir -p "$BASE/data/torrents/movies"
mkdir -p "$BASE/data/torrents/tv"
mkdir -p "$BASE/data/torrents/incomplete"
mkdir -p "$BASE/data/media/movies"
mkdir -p "$BASE/data/media/tv"
mkdir -p "$BASE/config/jellyfin/cache"
mkdir -p "$BASE/config/qbittorrent"
mkdir -p "$BASE/config/prowlarr"
mkdir -p "$BASE/config/radarr"
mkdir -p "$BASE/config/sonarr"
mkdir -p "$BASE/config/bazarr"
mkdir -p "$BASE/config/jellyseerr"

chown -R "$REAL_UID:$REAL_GID" "$BASE"
log "Directory structure created at $BASE"

# --- Step 6: Deploy configuration ---
echo ""
echo "--- Step 6: Deploying configuration ---"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Copy docker-compose.yml
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    cp "$SCRIPT_DIR/docker-compose.yml" "$BASE/docker-compose.yml"
    log "docker-compose.yml copied to $BASE/"
else
    err "docker-compose.yml not found in $SCRIPT_DIR"
fi

# Create .env
cat > "$BASE/.env" << EOF
PUID=$REAL_UID
PGID=$REAL_GID
TZ=Europe/Sofia
SERVER_IP=$SERVER_IP
DATA_PATH=/opt/media-stack/data
CONFIG_PATH=/opt/media-stack/config
EOF
log ".env created with PUID=$REAL_UID, PGID=$REAL_GID, TZ=Europe/Sofia"

# --- Step 7: Launch the stack ---
echo ""
echo "--- Step 7: Launching media stack ---"

cd "$BASE"
docker compose pull
docker compose up -d

echo ""
log "All services started!"
echo ""

# --- Summary ---
echo ""
echo "========================================"
echo " Setup Complete!"
echo "========================================"
echo ""
echo " Server IP: $SERVER_IP"
echo " Containers run as: $REAL_USER (UID=$REAL_UID)"
echo ""
echo " Service URLs:"
echo "   Jellyfin:    http://$SERVER_IP:8096    (media server — keep Hardware acceleration: None)"
echo "   Jellyseerr:  http://$SERVER_IP:5055    (browse & request)"
echo "   Radarr, Sonarr, Prowlarr, qBittorrent, Bazarr listen on 127.0.0.1 only;"
echo "   open them for setup through a temporary LXD proxy (README, Step 4)."
echo ""
echo " Next steps (follow the plan in order):"
echo "   1. qBittorrent: get temp password: docker logs qbittorrent 2>&1 | grep password"
echo "   2. Prowlarr:    add toloka.to indexer, connect to Radarr/Sonarr"
echo "   3. Radarr:      set root folder /data/media/movies, add qBittorrent"
echo "   4. Sonarr:      set root folder /data/media/tv, add qBittorrent"
echo "   5. Bazarr:      configure Ukrainian + English subtitle providers"
echo "   6. Jellyfin:    run setup wizard, add libraries; leave Hardware Acceleration"
echo "                   on None — there is no GPU here (docs/runbook.md §2)"
echo "   7. Jellyseerr:  connect to Jellyfin, Radarr, Sonarr"
echo "   8. LG TV:       install Jellyfin from LG Content Store, connect to http://$SERVER_IP:8096"
echo ""

# Remind about docker group
if [ -n "${SUDO_USER:-}" ] && ! groups "$SUDO_USER" 2>/dev/null | grep -q docker; then
    warn "Log out and back in (or run 'newgrp docker') for docker group to take effect."
fi
