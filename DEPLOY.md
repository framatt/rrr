# Docker deployment

This repo ships as one image containing:

- Flask (`app.py`) served by Gunicorn (1 worker, 16 threads).
- Node 22 + the WebTorrent bridge (`torrent_bridge/server.mjs`), started by the Flask app.
- Client-side Chromecast support through the Google Cast SDK in the browser.

The app no longer requires Docker host networking or LAN multicast for casting. A normal bridge
network plus the app and torrent ports is enough.

---

## How casting works now

The browser is the Cast sender:

1. The page loads the Google Cast SDK over HTTPS.
2. The Cast SDK discovers Cast devices on the browser's LAN.
3. The browser asks the TV (via the Default Media Receiver) to load the current stream URL.
4. The TV fetches the video directly from your HTTPS origin, or from `CAST_MEDIA_BASE`.
5. The browser pauses local playback and remains a remote control.

This is the same high-level model Netflix uses: the sender discovers and controls, the receiver plays,
and the TV fetches the media. The difference is that Netflix uses a custom receiver and its CDN; this app
uses the Default Media Receiver and your Flask server.

Why the pivot: `pychromecast` on the server depends on mDNS/L2 adjacency and a LAN-reachable media URL.
If the server moves to another VLAN/L3, server-side discovery and direct LAN casting break. Moving
discovery into the browser lets the server live anywhere the TV can reach over HTTPS.

TLS is required for the Cast SDK. Since you are handling SSL, the browser page will be a secure context;
the TV can fetch the media from the same HTTPS origin across L3.

The old server-side `pychromecast` path is still in the image as a fallback for LAN deployments, but it
is not used by default.

---

## 1. Requirements

- Linux Docker host with Compose v2.
- A valid HTTPS endpoint for the app (you handle TLS). The Cast SDK requires HTTPS or localhost.
  A self-signed certificate is not suitable for the TV's media fetch.
- Ports:
  - `8080` reachable by your reverse proxy only (do not expose it publicly).
  - `${TORRENT_PORT}` (default `51413`) TCP + UDP for torrent peers.
  - `443` public for the HTTPS front end.
- For casting: Chrome or Edge on a device on the **TV's LAN/Wi-Fi**. The browser does discovery, so it
  must be in the TV's L2 broadcast domain; mDNS does not cross VLANs without an mDNS reflector.
- The TV must be able to reach the media URL:
  - Normally the public HTTPS URL, which works across L3 if DNS/port forwarding/NAT hairpin or
    split-horizon DNS is set up.
  - Or a routed private URL set in `CAST_MEDIA_BASE`.
- `data/` must be on a local disk. SQLite WAL does not work correctly on NFS/SMB.

---

## 2. Push the files from this machine

```bash
git add Dockerfile .dockerignore docker-compose.yml .env.example .gitignore DEPLOY.md
git add static/app.js static/index.html static/cast-init.js app.py torrent_bridge/server.mjs
git commit -m "Add Docker deployment and client-side Cast"
git push origin main
```

Do not commit `.env` (it is ignored).

---

## 3. First-time setup on the server

```bash
git clone https://github.com/framatt/rrr.git
cd rrr
cp .env.example .env
nano .env
```

Set at least:

```dotenv
APP_USER=admin
APP_PASS=a-long-password

# Leave empty to use the page's HTTPS origin for the TV's media URL.
# Set only if the TV needs a different reachable URL.
CAST_MEDIA_BASE=

PUID=1000
PGID=1000
TZ=Europe/London
TORRENT_PORT=51413
```

If `APP_PASS` contains `#`, spaces, or `$`, wrap it in single quotes:

```dotenv
APP_PASS='a #long $password'
```

Find the UID/GID on the server:

```bash
id -u
id -g
```

Set `PUID`/`PGID` to those values (usually `1000`) and create the data directories:

```bash
mkdir -p data/cache data/state
sudo chown -R "$(id -u):$(id -g)" data
```

Optional: if you have a pre-built `catalog.json`, copy it to `data/catalog.json`. If it is absent,
the app fetches the live catalog on startup.

### Optional: migrate existing local data

Stop the app on the Windows machine first, then copy the profile and metadata cache:

```bash
scp state/userdata.json  server:~/rrr/data/state/userdata.json
scp state/downloads.json server:~/rrr/data/state/downloads.json
scp .app_cache/metadata.sqlite3 server:~/rrr/data/cache/metadata.sqlite3
scp catalog.json server:~/rrr/data/catalog.json   # only if you have one
```

On the server, re-apply ownership:

```bash
sudo chown -R "$(id -u):$(id -g)" data
```

---

## 4. Build and start

```bash
docker compose up -d --build
docker compose logs -f --tail=100
```

Verify:

```bash
curl -s http://127.0.0.1:8080/api/health
# with APP_USER/APP_PASS set:
curl -s -u admin:your-password http://127.0.0.1:8080/api/health
```

Check the container and network mode:

```bash
docker compose ps
docker inspect rrr --format '{{.HostConfig.NetworkMode}}'   # should print: bridge
```

The app is now HTTP on port 8080. TLS is terminated by your proxy.

---

## 5. HTTPS / reverse proxy

You are handling SSL. The app just needs to be reached over HTTP on `8080` by the proxy, and the public
HTTPS URL must route media requests back to the app.

- Upstream: `http://<server-ip>:8080` (or `http://app:8080` if the proxy shares the Compose network, or
  `http://host.docker.internal:8080` with `extra_hosts: ["host.docker.internal:host-gateway"]`).
- Serve the app at the root of a dedicated hostname (`https://media.example.com/`), not a URL subpath.
  The frontend and API use absolute paths.
- Use a valid certificate for the public hostname.
- Do **not** put proxy-level authentication (e.g. an NPM Access List) in front of the media endpoints.
  The TV fetches tokenized URLs and cannot answer a proxy login prompt. The app's `APP_USER`/`APP_PASS`
  is fine because `/api/video`, `/api/torrents/stream`, `/api/downloads/stream`, and `/api/image` accept
  media tokens and bypass basic auth.
- Disable response buffering for video, and use long timeouts.
- Preserve the `Host` header so the app's same-origin checks work for POSTs.

Example advanced proxy config:

```nginx
client_max_body_size 50m;

proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
send_timeout 3600s;

proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

The browser page and the TV media fetch can use the same public HTTPS origin. The TV does not use port
8080 directly, so `8080` should stay private to the proxy/server.

---

## 6. Cast checklist

1. Open the app over **HTTPS** in **Chrome or Edge** on a device connected to the TV's LAN/Wi-Fi.
2. Start playback (the Cast button is in the player).
3. Click **Cast**. The browser opens the native Cast device picker.
4. Pick the TV. The browser pauses local playback; the TV starts fetching the stream.

Requirements and behavior:

- **Secure context:** the Cast SDK requires HTTPS (or localhost). Use the HTTPS URL, not the plain LAN
  HTTP URL.
- **Chromium browser:** the web Cast SDK is supported in Chrome/Edge/Opera. Safari and Firefox cannot
  use client-side Cast; they fall back to the server-side path, which only works if the container is on
  the TV's L2 and uses host networking.
- **Browser in the TV's L2 broadcast domain:** discovery is local mDNS. Disable AP/client isolation.
  Guest Wi-Fi often blocks it. If the browser is on another VLAN, add an mDNS reflector or move it onto
  the TV's network.
- **TV can reach the media URL:** by default the URL is the page's HTTPS origin. If the TV is on a
  different VLAN/L3, the public HTTPS endpoint must route back to the proxy (port forwarding, NAT
  hairpin, or split-horizon DNS). Alternatively, set `CAST_MEDIA_BASE` to a URL the TV can route to.
- **Valid TLS:** the TV will reject a self-signed certificate for the media fetch.
- **No proxy auth on media:** use token URLs; do not put an Access List in front of them.
- **Codecs:** the Default Media Receiver handles MP4/H.264/AAC best. MKV/AVI, HEVC, and DTS may be
  rejected. There is no transcoding in this app.
- **Torrents:** the TV pulls from Flask, Flask pulls from the WebTorrent bridge. The bridge needs outbound
  internet; incoming peers use port `51413` (published by Compose).

### Cross-L3 notes

The server can be on another VLAN/L3. The browser must still be in the TV's L2 broadcast domain for
discovery, and the TV must have a route to the media URL.

- If the TV uses the public HTTPS domain and both are behind the same NAT, enable NAT hairpin/loopback or
  add a local DNS override so the domain resolves to the server's private address.
- If you prefer a private media URL, set `CAST_MEDIA_BASE=http://<server-private-ip>:8080` (or an HTTPS
  URL with a matching certificate) and allow that port between the TV's VLAN and the server's VLAN.
- If the browser is not on the TV's LAN, client-side discovery cannot work. The server-side fallback can
  only help if the server itself is on the TV's LAN and the container uses host networking.
- If there is no route at all between the TV and the server, casting cannot work without a relay or
  moving them onto a common network.

The page URL and the media URL do not have to be identical. `CAST_MEDIA_BASE` controls what the TV
fetches; leave it empty to reuse the browser's HTTPS origin.

---

## 7. Updates

```bash
cd rrr
git pull
docker compose up -d --build
```

Data in `./data` is preserved. `docker compose down` also preserves it; only `down -v` would remove named
volumes (there are none here).

---

## 8. Backup and restore

Backup:

```bash
sudo tar czf "rrr-data-$(date +%F).tgz" data
```

Restore:

```bash
docker compose down
sudo tar xzf rrr-data-YYYY-MM-DD.tgz
sudo chown -R "$(id -u):$(id -g)" data
docker compose up -d
```

`data/state/userdata.json` is the watchlist/progress library. `data/cache/` holds the metadata database,
image cache, live lists, torrent data, and the torrent bridge log. `data/cache/torrents/` can grow large
when background downloads are used; `data/cache/images/` is a poster cache.

---

## 9. Troubleshooting

### Cast button shows no devices

- Is the page loaded over **HTTPS**? The Cast SDK is unavailable on plain HTTP.
- Are you using **Chrome/Edge**? Safari/Firefox cannot use the web Cast SDK.
- Is the browser on the same LAN/Wi-Fi as the TV? Check AP/client isolation and guest-network rules.
- Open the browser console. The Cast SDK loads from `https://www.gstatic.com`; the app's CSP allows it,
  including the Google Cast extension script IDs used by the SDK. If the network blocks gstatic, the
  SDK never initializes and the app falls back to the server menu.
- On desktop Chrome, device discovery can depend on Chrome's Cast integration or the Google Cast
  extension. If the console reports "No cast extension found", try Chrome on Android, or install the
  official Google Cast extension. The app's CSP already permits the extension IDs the SDK requests.
- If you are testing the server-side fallback, the container must use host networking and be on the TV's
  L2. That is not the default deployment.

### The TV starts but the video fails

- Check `CAST_MEDIA_BASE` (if set) or the page HTTPS origin. The TV must be able to open the media URL.
- Test reachability from another device on the TV's network:
  `curl -I https://media.example.com/api/cast/config` (a `401` still proves reachability when auth is on).
- Confirm the certificate is valid and publicly trusted (not self-signed).
- Confirm the proxy has no login/Access List on `/api/video`, `/api/torrents/stream`, or
  `/api/downloads/stream`.
- Check the proxy is not buffering and its read timeout is long enough.
- Try an MP4/H.264/AAC title. MKV/HEVC/DTS may be rejected by the TV.

### NAT hairpin / cross-VLAN media failures

- A TV on the LAN resolving a public domain may fail if the router does not support NAT loopback.
- Add a split-horizon DNS entry (public domain → server private IP) or set `CAST_MEDIA_BASE` to a
  reachable private URL.
- Ensure inter-VLAN routing/firewall allows the TV to reach the chosen media port.

### Proxy returns 502

Test the upstream from the proxy container/network:

```bash
curl -I http://192.168.1.50:8080/api/cast/config
```

A `401` means the connection is fine. If it cannot connect, fix the upstream host/port or use
`host.docker.internal` with `host-gateway` as described above.

### Torrent features fail

Check the health payload:

```bash
curl -s -u admin:your-password http://127.0.0.1:8080/api/health
```

`torrents.installed`, `torrents.available`, and `torrents.node` should be true/present. Check
`data/cache/torrent-bridge.log`. Confirm `51413` (TCP and UDP) is not already used on the host; change
`TORRENT_PORT` in `.env` if it is.

### Permission errors under `data/`

The container runs as `${PUID}:${PGID}`. Fix ownership:

```bash
sudo chown -R 1000:1000 data
```

### App is up but the catalog is empty

If `data/catalog.json` is absent, the app fetches the live catalog from the configured upstream. Check
outbound internet access from the container and `docker compose logs`.
