#!/bin/bash
set -euo pipefail

# =============================================================================
# HOST Setup Script — Run on the Ubuntu 24.04 HOST (homelab, 192.168.1.216)
#
# This script:
#   1. Verifies NVIDIA driver on the host
#   2. Creates LXC container "media-server" on existing LXD bridge
#   3. Configures GPU passthrough + Docker nesting
#   4. Adds LXD proxy devices so services are reachable at 192.168.1.216:<port>
#   5. Installs nvidia-utils inside container (must match host driver)
#
# Usage: sudo bash host-setup.sh
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

CONTAINER_NAME="media-server"
HOST_IP="192.168.1.216"

if [ "$EUID" -ne 0 ]; then
    err "Please run as root: sudo bash host-setup.sh"
fi

echo ""
echo "========================================================"
echo " HOST Setup: LXC Container + NVIDIA GPU Passthrough"
echo " Container: $CONTAINER_NAME"
echo " Host NIC:  enp11s0 ($HOST_IP)"
echo " Network:   default LXD bridge + proxy devices"
echo "========================================================"
echo ""

# =============================================================
# Step 1: Verify NVIDIA Driver on HOST
# =============================================================
echo "--- Step 1: Verifying NVIDIA driver on host ---"

if ! command -v nvidia-smi &> /dev/null; then
    err "nvidia-smi not found. Install the NVIDIA driver first:
    sudo apt install ubuntu-drivers-common
    sudo ubuntu-drivers install --gpgpu
    sudo reboot"
fi

if ! nvidia-smi &> /dev/null; then
    err "nvidia-smi failed. The driver may need a reboot: sudo reboot"
fi

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
HOST_DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
HOST_DRIVER_MAJOR=${HOST_DRIVER_VERSION%%.*}
log "NVIDIA driver OK: $GPU_NAME (driver $HOST_DRIVER_VERSION)"

if [ ! -e /dev/nvidia0 ]; then
    err "/dev/nvidia0 not found. Driver may not be loaded. Try: sudo reboot"
fi

# =============================================================
# Step 2: Create the LXC container
# =============================================================
echo ""
echo "--- Step 2: Creating LXC container '$CONTAINER_NAME' ---"

if lxc info "$CONTAINER_NAME" &> /dev/null 2>&1; then
    warn "Container '$CONTAINER_NAME' already exists. Skipping creation."
else
    lxc launch ubuntu:24.04 "$CONTAINER_NAME"
    log "Container '$CONTAINER_NAME' created with Ubuntu 24.04"
    echo "Waiting for container to start..."
    sleep 5
fi

# =============================================================
# Step 3: Configure container — nesting, security, GPU
# =============================================================
echo ""
echo "--- Step 3: Configuring container ---"

# Stop container for config changes
lxc stop "$CONTAINER_NAME" --force 2>/dev/null || true
sleep 2

# Enable nesting (required for Docker inside LXC)
lxc config set "$CONTAINER_NAME" security.nesting true
log "Nesting enabled (required for Docker)"

# Enable syscall interception (required for Docker overlayfs in unprivileged containers)
lxc config set "$CONTAINER_NAME" security.syscalls.intercept.mknod true
lxc config set "$CONTAINER_NAME" security.syscalls.intercept.setxattr true
log "Syscall interception enabled (mknod + setxattr)"

# Relax AppArmor (fixes Docker permission issues in Ubuntu 24.04 LXC)
lxc config set "$CONTAINER_NAME" raw.lxc "lxc.apparmor.profile=unconfined"
log "AppArmor set to unconfined (fixes Docker in Ubuntu 24.04)"

# Pass through NVIDIA GPU
lxc config device add "$CONTAINER_NAME" gpu gpu 2>/dev/null && \
    log "NVIDIA GPU device added" || \
    warn "GPU device may already exist (OK if re-running)"

# =============================================================
# Step 4: Add proxy devices (port forwarding from host to container)
# =============================================================
echo ""
echo "--- Step 4: Setting up port forwarding (LXD proxy devices) ---"

# Only expose what's needed from outside:
#   - Jellyfin (8096/tcp)  — TV connects here to play media
#   - Jellyseerr (5055/tcp) — browse & request UI (phone/laptop)
#   - Torrent traffic (6881/tcp+udp) — peers need this for downloads
# All other services (Radarr, Sonarr, Prowlarr, qBittorrent, Bazarr)
# communicate internally via Docker network — no external access needed.

declare -A PORTS=(
    ["jellyfin"]="8096"
    ["jellyseerr"]="5055"
    ["qbt-torrent"]="6881"
)

for svc in "${!PORTS[@]}"; do
    port="${PORTS[$svc]}"

    # Remove existing proxy if present (idempotent)
    lxc config device remove "$CONTAINER_NAME" "proxy-${svc}" 2>/dev/null || true

    lxc config device add "$CONTAINER_NAME" "proxy-${svc}" proxy \
        listen="tcp:0.0.0.0:${port}" \
        connect="tcp:127.0.0.1:${port}"

    log "Port $port/tcp forwarded ($svc)"
done

# Also forward qBittorrent torrent port as UDP
lxc config device remove "$CONTAINER_NAME" "proxy-qbt-torrent-udp" 2>/dev/null || true
lxc config device add "$CONTAINER_NAME" "proxy-qbt-torrent-udp" proxy \
    listen="udp:0.0.0.0:6881" \
    connect="udp:127.0.0.1:6881"
log "Port 6881/udp forwarded (qbt-torrent-udp)"

# =============================================================
# Step 5: Start container
# =============================================================
echo ""
echo "--- Step 5: Starting container ---"

lxc start "$CONTAINER_NAME"
echo "Waiting for container to boot..."
sleep 8
log "Container started"

# Show container IP
CONTAINER_INTERNAL_IP=$(lxc list "$CONTAINER_NAME" --format csv -c 4 | head -1 | cut -d' ' -f1)
log "Container internal IP: $CONTAINER_INTERNAL_IP (on LXD bridge)"
log "Services will be accessible at $HOST_IP via proxy devices"

# =============================================================
# Step 6: Install NVIDIA utilities inside container
# =============================================================
echo ""
echo "--- Step 6: Installing NVIDIA utilities inside container ---"

log "Host driver version: $HOST_DRIVER_VERSION (major: $HOST_DRIVER_MAJOR)"

lxc exec "$CONTAINER_NAME" -- apt-get update -qq

# Try exact major version first, then common fallbacks
INSTALLED=false
for ver in "$HOST_DRIVER_MAJOR" 570 565 560 555 550 545; do
    if lxc exec "$CONTAINER_NAME" -- apt-get install -y -qq "nvidia-utils-${ver}" 2>/dev/null; then
        log "nvidia-utils-${ver} installed inside container"
        INSTALLED=true
        break
    fi
done

if [ "$INSTALLED" = false ]; then
    warn "Could not auto-install nvidia-utils matching host driver $HOST_DRIVER_VERSION"
    warn "Manually install inside container:"
    warn "  lxc exec $CONTAINER_NAME -- apt install nvidia-utils-$HOST_DRIVER_MAJOR"
fi

# =============================================================
# Step 7: Verify GPU inside container
# =============================================================
echo ""
echo "--- Step 7: Verifying GPU access inside container ---"

echo ""
if lxc exec "$CONTAINER_NAME" -- nvidia-smi; then
    echo ""
    log "GPU is accessible inside the container!"
else
    echo ""
    warn "nvidia-smi failed inside container."
    warn "This often means nvidia-utils version mismatch."
    warn "Check host version: nvidia-smi"
    warn "Then inside container: apt list --installed 2>/dev/null | grep nvidia-utils"
    warn "Install matching version: lxc exec $CONTAINER_NAME -- apt install nvidia-utils-$HOST_DRIVER_MAJOR"
fi

# =============================================================
# Done
# =============================================================
echo ""
echo "========================================================"
echo " HOST Setup Complete!"
echo "========================================================"
echo ""
echo " Container: $CONTAINER_NAME"
echo " GPU:       $GPU_NAME (NVENC)"
echo " Network:   LXD bridge + proxy (3 ports forwarded)"
echo ""
echo " Exposed on $HOST_IP:"
echo "   Jellyfin:    http://$HOST_IP:8096  (TV connects here)"
echo "   Jellyseerr:  http://$HOST_IP:5055  (browse & request movies)"
echo "   Torrent:     $HOST_IP:6881         (peer traffic)"
echo ""
echo " Internal only (no outside access):"
echo "   Radarr, Sonarr, Prowlarr, qBittorrent, Bazarr"
echo ""
echo " Next step — copy setup files into the container and run:"
echo ""
echo "   lxc file push docker-compose.yml ${CONTAINER_NAME}/root/"
echo "   lxc file push setup.sh ${CONTAINER_NAME}/root/"
echo "   lxc exec ${CONTAINER_NAME} -- bash -c 'cd /root && bash setup.sh'"
echo ""
echo " Or enter the container interactively:"
echo ""
echo "   lxc exec ${CONTAINER_NAME} -- bash"
echo "   cd /root && bash setup.sh"
echo ""
