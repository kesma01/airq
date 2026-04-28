# 🌬️ AIRQ — Slovenia Air Quality Map

A real-time air quality map for Slovenia, aggregating data from five public and crowdsourced sources into a single interactive map with 24-hour history charts.

**Live at → [airq.kesma.wtf](https://airq.kesma.wtf)**

![Slovenia Air Quality Map](https://airq.kesma.wtf/static/preview.png)

---

## Features

- **Real-time data** refreshed every 15 minutes from five sources
- **AQI colour-coded markers** (Good → Hazardous) with zoom-adaptive sizing
- **Interactive station cards** — tap any marker to see all readings; click a parameter to switch the 24-hour sparkline chart
- **24-hour history** stored locally in SQLite, with a CAMS model fallback for PM2.5 when no local data exists yet
- **Stale station handling** — stations that temporarily go offline are kept on the map with a dashed marker and a "last seen" timestamp for up to 7 days
- **Dark / light theme** and **Slovenian / English** UI toggle, both persisted in localStorage
- **Mobile-first layout** — full-screen map on desktop, bottom-sheet panel on mobile, iOS Safari tested

## Data Sources

| Source | Coverage | Auth |
|--------|----------|------|
| [ARSO](https://www.arso.gov.si/) | Official Slovenian reference monitors (PM2.5, PM10, NO₂, O₃, SO₂, CO) | None |
| [Sensor.Community](https://sensor.community/) | Crowdsourced low-cost sensors across Slovenia | None |
| [OpenSenseMap](https://opensensemap.org/) | Citizen science sensor network | None |
| [PurpleAir](https://www.purpleair.com/) | Dual-channel Plantower sensors, Slovenia bbox | API key |
| [AQICN](https://aqicn.org/) | Multi-pollutant AQI including cross-border AT/HR stations | API key |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3 / Flask |
| Storage | SQLite (tall schema: `bucket × station × param`) |
| Map | Leaflet.js + CartoDB tiles (light & dark) |
| Charts | Chart.js 4 |
| Server | Nginx → Flask on any Ubuntu VPS |
| TLS | Let's Encrypt via Certbot |

## Project Structure

```
airq_app.py          # Flask app — data collectors, SQLite, API routes
templates/
  index.html         # Single-page HTML shell
static/
  css/style.css      # Responsive layout, theming, marker & panel styles
  js/map.js          # Leaflet map, markers, panel, Chart.js sparklines
requirements.txt     # Python dependencies
tokens.txt.example   # API key template (copy to tokens.txt and fill in)
```

## Running Locally

```bash
# 1. Clone
git clone git@github.com:kesma01/airq.git
cd airq

# 2. Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. API keys
cp tokens.txt.example tokens.txt
# Edit tokens.txt and add your keys (AQICN and PurpleAir are optional)

# 4. Run
python3 airq_app.py
# Open http://localhost:8060
```

The background collector runs immediately on startup and then every 15 minutes aligned to UTC boundaries. History accumulates in `airq_data.db` (created automatically).

## Deploying

Any Ubuntu 22.04/24.04 VPS with root SSH access works. Point your domain at the server IP before running Certbot.

### 1 — Deploy the app

SSH into the server and run the following once:

```bash
# Install system packages
apt-get update && apt-get install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx

# Copy the app
mkdir -p /opt/airq
cd /opt/airq
git clone https://github.com/kesma01/airq.git .

# API keys
cp tokens.txt.example tokens.txt
nano tokens.txt   # fill in your keys

# Python environment
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

### 2 — systemd service

Create `/etc/systemd/system/airq.service`:

```ini
[Unit]
Description=AIRQ Air Quality Map
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/airq
ExecStart=/opt/airq/venv/bin/python3 airq_app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
chown -R www-data:www-data /opt/airq
systemctl daemon-reload
systemctl enable --now airq
systemctl status airq
```

### 3 — Nginx reverse proxy

Create `/etc/nginx/sites-available/airq`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name your.domain.com;

    # Serve static files directly — bypass Flask for speed
    location /static/ {
        alias /opt/airq/static/;
        add_header Cache-Control "public, max-age=120, must-revalidate";
    }

    # API must never be cached
    location /api/ {
        proxy_pass http://127.0.0.1:8060;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        add_header Cache-Control "no-store";
    }

    # HTML — no-store so Cloudflare never serves a stale page
    location / {
        proxy_pass http://127.0.0.1:8060;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }
}
```

```bash
ln -s /etc/nginx/sites-available/airq /etc/nginx/sites-enabled/airq
nginx -t && systemctl reload nginx
```

### 4 — TLS with Let's Encrypt

```bash
certbot --nginx -d your.domain.com --non-interactive --agree-tos -m you@example.com
systemctl reload nginx
```

Certbot auto-renews via a systemd timer — no cron job needed.

### Updating

```bash
cd /opt/airq
git pull
systemctl restart airq
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/stations` | All stations with current AQI, readings, stale flag |
| `GET /api/history/<id>?param=PM2.5` | 24-hour 15-min buckets for any param |
| `GET /api/status` | Collector timestamp and next-run countdown |

## AQI Scale

| Colour | AQI | PM2.5 (µg/m³) |
|--------|-----|---------------|
| 🟢 Good | 0–50 | 0–12 |
| 🟡 Moderate | 51–100 | 12.1–35.4 |
| 🟠 Unhealthy for Sensitive Groups | 101–150 | 35.5–55.4 |
| 🔴 Unhealthy | 151–200 | 55.5–150.4 |
| 🟣 Very Unhealthy | 201–300 | 150.5–250.4 |
| 🟤 Hazardous | 301–500 | > 250.5 |

## License

MIT — free to use, fork, and adapt. Attribution appreciated.
