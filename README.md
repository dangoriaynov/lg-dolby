# Media Server Stack: Jellyfin + Arr Suite

Self-hosted media server running in an LXC container with NVIDIA GPU transcoding. Search, download, and stream movies/TV shows to an LG TV with Dolby 5.1/Atmos audio.

## What's Included

| Service | Purpose |
|---|---|
| **Jellyfin** | Media server — streams to your TV |
| **Jellyseerr** | Browse trending movies, search, and request downloads |
| **Radarr** | Movie automation — finds, downloads, organizes |
| **Sonarr** | TV show automation — same as Radarr for series |
| **Prowlarr** | Indexer manager — connects torrent trackers to Radarr/Sonarr |
| **qBittorrent** | Torrent client |
| **Bazarr** | Automatic subtitle downloads |

## Architecture

```
┌─ LAN ──────────────────────────────────────────────┐
│                                                     │
│  Ubuntu 24.04 Host (192.168.1.216)                  │
│  ├── NVIDIA RTX GPU (driver on host)                │
│  └── LXC container "media-server"                   │
│      ├── Docker                                     │
│      │   ├── Jellyfin     (:8096)  ◄── LG TV        │
│      │   ├── Jellyseerr   (:5055)  ◄── Phone/Laptop │
│      │   ├── Radarr       (:7878)  (internal)       │
│      │   ├── Sonarr       (:8989)  (internal)       │
│      │   ├── Prowlarr     (:9696)  (internal)       │
│      │   ├── qBittorrent  (:8085)  (internal)       │
│      │   └── Bazarr       (:6767)  (internal)       │
│      └── NVIDIA Container Toolkit (NVENC)           │
│                                                     │
│  LG TV ──HDMI eARC──► TCL Soundbar (Dolby Atmos)   │
└─────────────────────────────────────────────────────┘
```

## Hardware

- **TV:** LG 50UP78003LB (webOS 6, eARC, H265 hardware decode)
- **Soundbar:** TCL Q85H (7.1.4ch, Dolby Atmos, DTS:X via eARC)
- **Server:** Ubuntu 24.04 with NVIDIA GPU (NVENC transcoding)

## Prerequisites

- Ubuntu 24.04 host with LXD (snap)
- NVIDIA GPU with driver installed on the host
- `nvidia-smi` working on the host

## Deployment

### Step 1: Host setup (creates LXC container + GPU passthrough)

```bash
sudo bash host-setup.sh
```

### Step 2: Push files into the container

```bash
lxc file push docker-compose.yml media-server/root/
lxc file push setup.sh media-server/root/
```

### Step 3: Run setup inside the container

```bash
lxc exec media-server -- bash -c 'cd /root && bash setup.sh'
```

### Step 4: Open admin ports for initial configuration

These ports are only needed during setup. The admin services communicate internally via Docker network and don't need outside access for normal operation.

```bash
# Temporarily expose admin UIs for configuration
lxc config device add media-server proxy-radarr proxy listen=tcp:0.0.0.0:7878 connect=tcp:127.0.0.1:7878
lxc config device add media-server proxy-sonarr proxy listen=tcp:0.0.0.0:8989 connect=tcp:127.0.0.1:8989
lxc config device add media-server proxy-prowlarr proxy listen=tcp:0.0.0.0:9696 connect=tcp:127.0.0.1:9696
lxc config device add media-server proxy-qbittorrent proxy listen=tcp:0.0.0.0:8085 connect=tcp:127.0.0.1:8085
lxc config device add media-server proxy-bazarr proxy listen=tcp:0.0.0.0:6767 connect=tcp:127.0.0.1:6767
```

### Step 5: Configure services (in order)

1. **qBittorrent** (`http://192.168.1.216:8085`) — get temp password: `lxc exec media-server -- docker logs qbittorrent 2>&1 | grep password`, change it, set save path to `/data/torrents`
2. **Radarr** (`http://192.168.1.216:7878`) — add root folder `/data/media/movies`, add qBittorrent as download client (host: `qbittorrent`, port: `8085`, category: `radarr`), enable hardlinks
3. **Sonarr** (`http://192.168.1.216:8989`) — same as Radarr with root folder `/data/media/tv`, category: `sonarr`
4. **Prowlarr** (`http://192.168.1.216:9696`) — add toloka.to (or other trackers), connect to Radarr and Sonarr via Settings → Apps (use their API keys)
5. **Bazarr** (`http://192.168.1.216:6767`) — add subtitle languages, enable providers (OpenSubtitles.com), connect to Radarr/Sonarr
6. **Jellyfin** (`http://192.168.1.216:8096`) — run wizard, add libraries (`/data/media/movies`, `/data/media/tv`), enable **Nvidia NVENC** in Dashboard → Playback
7. **Jellyseerr** (`http://192.168.1.216:5055`) — sign in with Jellyfin (`http://jellyfin:8096`), add Radarr (`http://radarr:7878`) and Sonarr (`http://sonarr:8989`)

### Step 6: Close admin ports

After configuration is complete, remove the temporary proxy devices:

```bash
lxc config device remove media-server proxy-radarr
lxc config device remove media-server proxy-sonarr
lxc config device remove media-server proxy-prowlarr
lxc config device remove media-server proxy-qbittorrent
lxc config device remove media-server proxy-bazarr
```

## Daily Usage

1. **Search & request:** Open `http://192.168.1.216:5055` (Jellyseerr) on your phone or laptop, find a movie, click Request
2. **Wait:** Radarr picks the best H265 + Dolby release, qBittorrent downloads it, Bazarr adds subtitles — all automatic
3. **Watch:** Open Jellyfin on your LG TV, pick the movie, play — video direct-plays, audio passes through to the soundbar

## TV & Soundbar Setup

1. Connect soundbar to **HDMI 2 (eARC)** on the LG TV
2. TV Settings → Sound → Sound Out → **HDMI ARC**
3. TV Settings → Sound → Additional Settings → eARC Support → **On**
4. TV Settings → Sound → Additional Settings → Digital Sound Output → **Pass Through**
5. Install **Jellyfin** from LG Content Store, connect to `http://192.168.1.216:8096`
6. Jellyfin client settings: max audio channels **7.1**, prefer direct play

## Ports

### Permanently exposed (via LXD proxy devices)

| Port | Service | Reason |
|---|---|---|
| 8096/tcp | Jellyfin | TV connects here |
| 5055/tcp | Jellyseerr | Browse & request UI |
| 6881/tcp+udp | qBittorrent | Torrent peer traffic |

### Internal only (Docker network)

| Port | Service |
|---|---|
| 7878 | Radarr |
| 8989 | Sonarr |
| 9696 | Prowlarr |
| 8085 | qBittorrent Web UI |
| 6767 | Bazarr |

## Files

| File | Where to run | Purpose |
|---|---|---|
| `host-setup.sh` | Ubuntu host | Creates LXC container, GPU passthrough, port forwarding |
| `setup.sh` | Inside container | Installs Docker, NVIDIA toolkit, launches stack |
| `docker-compose.yml` | Inside container | Defines all 7 services |

## Autostart

Everything starts automatically after a host reboot:

```bash
# Ensure LXC autostart (run on host)
lxc config set media-server boot.autostart true
```

Docker services have `restart: unless-stopped` and start with Docker daemon inside the container.
