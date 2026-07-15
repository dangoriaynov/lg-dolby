# Media Server — Operations Runbook

Operational reference for the `lg_dolby` media stack: how to reach it, how the
GPU is shared with the AI box, common diagnostics, and the write-up of the
2026-06-21 Jellyseerr outage.

---

## 1. Access & topology

```
your laptop ──ssh──► homelab host (danko@10.100.0.1)
                       │  Ubuntu 24.04, LXD, NVIDIA RTX 5070 Ti (driver on host)
                       ├── LXC "media-server"  (eth0 10.48.230.10, LAN 192.168.1.216)
                       │     └── Docker "media-stack"  (compose at /opt/media-stack)
                       │           jellyfin:8096  jellyseerr:5055  radarr  sonarr
                       │           prowlarr  qbittorrent  bazarr
                       └── LXC "llm"  (llama-server) ── owns the GPU
```

Get in and drive Docker inside the container:

```bash
ssh danko@10.100.0.1
lxc list                                   # all containers
lxc exec media-server -- docker ps         # media stack
lxc exec media-server -- bash -lc 'cd /opt/media-stack && docker compose ps'
```

- Repo (this folder) is the **source of truth**. It deploys to
  `/opt/media-stack/docker-compose.yml` inside the `media-server` LXC.
- Jellyfin is published to the LAN as `http://192.168.1.216:8096`
  (`JELLYFIN_PublishedServerUrl`, via an LXD `proxy-jellyfin` device →
  `127.0.0.1:8096`). Jellyseerr reaches Jellyfin at that same address.

---

## 2. GPU policy (important)

**The GPU is reserved for the `llm` container, not Jellyfin.**

- The host RTX 5070 Ti has **16 GB VRAM**; `llama-server` in the `llm` container
  already uses **~14.4 GB**, leaving only **~1.8 GB** free.
- Jellyfin therefore runs **without any GPU request** (`runtime: runc`,
  no `deploy.devices`, no `NVIDIA_*` env). It transcodes on **CPU**.
- This is fine in practice: the **LG 50/55UP78003LB (webOS 6)** hardware-decodes
  **H.264 and HEVC/H.265 up to 4K**, so Jellyfin **direct-plays** almost
  everything and never transcodes. CPU only kicks in for edge cases
  (e.g. AV1 source, subtitle burn-in, exotic audio).

Why not give Jellyfin the GPU?
1. **VRAM contention** — only ~1.8 GB free; a 4K transcode or several streams
   would fail with out-of-memory.
2. **Driver-version fragility** — a GPU-bound container breaks on every host
   NVIDIA driver bump that isn't mirrored in the container (see §4). Removing
   the GPU makes Jellyfin immune to that class of outage.

If you ever *do* need NVENC on Jellyfin, re-add to the `jellyfin` service in
`docker-compose.yml` and ensure the in-LXC driver matches the host (§4):

```yaml
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=all
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu, compute, video]
```

Check GPU usage at any time:

```bash
ssh danko@10.100.0.1 "nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader"
ssh danko@10.100.0.1 "nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv,noheader"
```

---

## 3. Common operations

```bash
# Status / health
lxc exec media-server -- docker ps -a
lxc exec media-server -- curl -s -o /dev/null -w 'jellyfin %{http_code}\n' http://127.0.0.1:8096/health
lxc exec media-server -- curl -s -o /dev/null -w 'jellyseerr %{http_code}\n' http://127.0.0.1:5055/api/v1/status

# Logs
lxc exec media-server -- docker logs --tail 100 jellyfin
lxc exec media-server -- docker logs --tail 100 jellyseerr

# Restart / recreate after editing compose
lxc exec media-server -- bash -lc 'cd /opt/media-stack && docker compose up -d jellyfin'

# Deploy a changed compose from this repo
lxc file push docker-compose.yml media-server/opt/media-stack/docker-compose.yml
lxc exec media-server -- bash -lc 'cd /opt/media-stack && docker compose up -d'
```

Jellyseerr config (incl. the Jellyfin URL/API key) lives at
`/app/config/settings.json` inside the `jellyseerr` container
(volume `${CONFIG_PATH}/jellyseerr`).

---

## 4. NVIDIA "Driver/library version mismatch" (the gotcha)

GPU passthrough into the LXC means the **kernel module comes from the host**,
but the **userspace driver libraries are installed via apt *inside* the LXC**.
If the host driver is upgraded and the container's libraries are not, NVML fails:

```
Failed to initialize NVML: Driver/library version mismatch
```

Any container that requests the GPU then fails to start (exit 128, error
"failed to generate CDI spec ... failed to initialize NVML").

**Detect:**

```bash
lxc exec media-server -- nvidia-smi                  # errors if mismatched
lxc exec media-server -- cat /proc/driver/nvidia/version   # kernel module ver (= host)
ssh danko@10.100.0.1 "cat /proc/driver/nvidia/version"     # host module ver
# Compare against in-LXC userspace:
lxc exec media-server -- dpkg -l | grep -i nvidia
```

**Fix — match the in-LXC userspace driver to the host's running module.**
Find the host version (`nvidia-smi` on host), then in the LXC:

```bash
lxc exec media-server -- bash -lc '
  apt-get update
  apt-get install -y nvidia-utils-<MAJ> libnvidia-compute-<MAJ> nvidia-kernel-common-<MAJ>
  apt-get autoremove -y          # removes the old <OLD> series
  nvidia-smi                     # must now report the host version cleanly
'
```

`libnvidia-ml.so.1` should then symlink to `libnvidia-ml.so.<host-version>`.

---

## 5. Incident — 2026-06-21: "Jellyseerr not working"

**Symptom.** Jellyseerr unusable. Its own API was healthy (`HTTP 200`), but logs
showed, every 5 min: `[Jellyfin API] ... read ECONNRESET`, and on login:
`[Auth] INVALID_URL ... hostname "http://undefined:undefinedundefined"`.

**Root cause.** The `jellyfin` container was `Exited (128)` since **2026-05-17**
with:

```
failed to ... initialize NVML: Driver/library version mismatch
```

The host driver had been upgraded to **580.159.03** (host kernel module = 580),
but the media-server LXC still had **570.211.01** userspace driver packages
(`nvidia-utils-570`, `libnvidia-compute-570`, …). Jellyfin requested the GPU via
CDI, CDI spec generation called NVML, NVML saw module 580 ≠ libs 570 → container
couldn't start. `restart: unless-stopped` retried and failed in a loop.

With Jellyfin down, Jellyseerr had no media backend:
- The LXD `proxy-jellyfin` device still listened on `:8096` but its backend was
  dead, so it **reset** connections → Jellyseerr saw `ECONNRESET` (not refused).
- Login (which authenticates against Jellyfin) failed → `INVALID_URL`.

**Fix applied.**
1. Upgraded the in-LXC NVIDIA userspace driver to match the host
   (`580.159.03-0ubuntu0.24.04.1`); removed the 570 packages. `nvidia-smi` in the
   LXC then worked. (This step is the general §4 recovery.)
2. **Decided to remove the GPU from Jellyfin entirely** — the GPU is needed for
   the `llm` container and the LG TV direct-plays anyway. Removed `runtime:
   nvidia`, the `NVIDIA_*` env, and the `deploy.devices` block from the
   `jellyfin` service in `docker-compose.yml`; recreated the container.

**Verification.**
- `jellyfin`: `running`, `runtime=runc`, `DeviceRequests=null`, `/health` → 200.
- `nvidia-smi`: only the `llm` processes present; Jellyfin not on the GPU.
- Jellyseerr → `http://192.168.1.216:8096/health` → `HTTP 200`; `ECONNRESET` gone.

**Takeaway.** Jellyfin no longer touches the GPU, so this class of outage cannot
recur from driver bumps. If the `llm` container ever shows the same NVML error
after a host driver upgrade, apply §4 to *its* LXC.

---

## 6. Downloads, quality & manual torrents (Radarr / Prowlarr / qBittorrent)

- **Network constraint:** the link to the server is ~**100 Mbit/s** (damaged
  cable). Keep releases modest — avoid Remux/4K (10–40 GB).
- **Radarr quality policy** (profile **"Any"**, used by the movies): allows up to
  **Bluray-1080p**, cutoff **Bluray-1080p**, upgrades on; **Remux-1080p / 2160p
  intentionally off**. Size caps already reject big files (global indexer max
  **14.6 GB**; per-movie quality-definition max ~**9.4 GB**). Language =
  *Original* (English) — Ukr/Eng dual-audio toloka rips pass because they include
  the English track.
- **Sonarr quality policy** (profile **"Any"**, used by the series): aligned with
  Radarr on 2026-06-21 — **cutoff = WEB 1080p group**, **upgrades on**, allowed up
  to **Bluray-1080p**, **all 2160p/Remux off**. Cutoff is the *WEB* 1080p tier
  (not Bluray) because TV releases are mostly web; chasing rare Bluray season-packs
  would re-download endlessly over the slow link. Bluray-1080p still ranks above
  the cutoff, so it's accepted if it appears.
- **Why Interactive Search shows red ❗ rows:** typical reasons — *"Existing file
  meets cutoff"* (already have an equal/better file), *"<quality> is not wanted in
  profile"*, *"Unknown Movie"* (collections / mini-movies / foreign-titled rips
  Radarr can't match), and size limits. Red = won't grab **automatically**.
- **Force-grab a specific release:** Radarr → movie → *Interactive Search* → click
  the **download ⬇ arrow** on the row. It overrides the rejection, sends it to
  qBittorrent, and Radarr imports it.
- **Add a torrent fully manually:** download the `.torrent` from toloka.to (logged
  in) → qBittorrent UI `http://192.168.1.216:8085` → add (saves under
  `/data/torrents`). Radarr does not see manual adds, so import it:
  Radarr → **Wanted / Movie → Manual Import**, folder `/data/torrents`, pick the
  file, set **movie + quality**, Import with **mode Copy (= hardlink, keeps
  seeding)**. API: `GET /api/v3/manualimport?folder=/data/torrents&filterExistingFiles=true`
  then `POST /api/v3/command {name:ManualImport, importMode:copy, files:[{path, movieId, quality, languages}]}`.
- **2026-06-21:** profile "Any" was 720p-capped and excluded Bluray-1080p — raised
  to allow Bluray-1080p with cutoff 1080p. A manually-added
  `Despicable Me (2010) BDRip 1080p H.265 [Hurtom]` was matched to the movie via
  Manual Import (replaced the 720p; both torrents keep seeding). Note: **no
  "Minions / Посіпаки (2015)" file exists on the server** — that's a separate
  movie that was never added/downloaded.

---

## 7. Incident — 2026-07-15: "downloaded series has no play button"

**Symptom.** Better Call Saul S1 was imported into Sonarr (10 files in
`/data/media/tv/Better Call Saul`, 100%), but Jellyseerr kept showing the show
as *Requested* with no play button, and Sonarr's Activity → Queue showed 10
stuck entries (a 68 GB rutracker season pack) with a clock icon and no
progress.

**Root causes (two independent ones).**

1. **Sonarr had no download client configured** (`GET /api/v3/downloadclient`
   → `[]` — README step "add qBittorrent to Sonarr" was never done). A
   Jellyseerr request had triggered a season search; the grabbed 68 GB pack
   could never be sent anywhere, so it sat in the queue as
   `downloadClientUnavailable` forever.
2. **Jellyfin never scanned the imported files.** Its `Shows` library
   (`/data/media/tv`) was correct, but real-time monitoring (inotify) doesn't
   reliably propagate through the LXC/ro-bind mount, and no scheduled scan had
   run since the import — Jellyfin had 0 Better Call Saul items, so Jellyseerr
   (which mirrors Jellyfin availability) had nothing to link a play button to.

**Fix applied (2026-07-15).**

- Removed the 10 dead queue entries (`DELETE /api/v3/queue/{id}?removeFromClient=false&blocklist=false`).
- Removed a bogus Sonarr root folder `/data/torrents/Better Call Saul 1080p H265`
  (was accidentally created from the Add Series page; the only root folder is
  `/data/media/tv`).
- Added **qBittorrent as Sonarr's download client** (host `qbittorrent`, port
  `8085`, category `sonarr` — credentials same as Radarr's; readable from
  Radarr's DB: `DownloadClients.Settings` in `/config/radarr.db`). Health check
  is green.
- Triggered a Jellyfin library scan (`POST /Library/Refresh`) → series + 10
  episodes appeared; ran Jellyseerr jobs `jellyfin-recently-added-scan` and
  `availability-sync` → status flipped to *partially available* (S1 of 6), play
  button restored.
- **Prevention:** added a *Connect → Emby/Jellyfin* notification (implementation
  `MediaBrowser`, host `jellyfin:8096`, "Update Library" on) to **both Sonarr
  and Radarr**, so every future import/upgrade/rename pings Jellyfin to rescan
  immediately instead of waiting for a periodic scan.

**Useful keys/tricks discovered.**

- Jellyfin API key: Jellyseerr stores the one it uses in
  `/app/config/settings.json` (`docker exec jellyseerr grep -o 'apiKey[^,]*' /app/config/settings.json`);
  use it as `X-Emby-Token`.
- Jellyseerr's own API key is the first (base64-looking) `apiKey` in the same
  file; jobs API: `POST /api/v1/settings/jobs/<id>/run`.
- Jellyseerr media status codes: 1 unknown, 2 pending, 3 processing,
  4 partially available, 5 available. The play button appears at 4+.

**Takeaway.** "No play button" almost always means *Jellyfin doesn't have the
item*, not a Jellyseerr bug: check `ls /data/media/...` first, then whether
Jellyfin has scanned it. Stuck orange-clock queue rows with no Time Left mean
the download was never handed to a client — check System → Health and
Settings → Download Clients before blaming the tracker.
