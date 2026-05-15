"""
Air Quality Map for Slovenia
Data sources:
  - ARSO          (official Slovenian Environment Agency XML, no auth)
  - Sensor.Community (crowdsourced SDS011/PMS, no auth)
  - OpenSenseMap  (citizen science SDS011/PMS, no auth)
  - PurpleAir     (Plantower PMS5003A dual-channel, API key)
  - AQICN         (multi-pollutant AQI + cross-border AT/HR stations, API key)
History: SQLite DB, collector runs every 15 min in background.
Run: source venv/bin/activate && python3 airq_app.py
"""

import os
import json
import time
import threading
import sqlite3
import xml.etree.ElementTree as ET
import datetime
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ── Tokens ────────────────────────────────────────────────────────────────────

def _load_tokens():
    tokens = {}
    path = os.path.join(os.path.dirname(__file__), "tokens.txt")
    try:
        with open(path) as f:
            for line in f:
                if "," in line:
                    domain, key = line.strip().split(",", 1)
                    tokens[domain.strip()] = key.strip()
    except Exception as e:
        print(f"[tokens] {e}")
    return tokens

_T = _load_tokens()
AQICN_TOKEN   = _T.get("aqicn.org", "")
PURPLEAIR_KEY = _T.get("purpleair.com", "")
OPENAQ_KEY    = _T.get("openaq.org", "")

# ── SQLite DB ─────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(__file__), "airq_data.db")

@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with _db() as conn:
        # Migrate from old wide schema (pm25/pm10/aqi columns) to tall schema (param/value)
        try:
            conn.execute("SELECT param FROM measurements LIMIT 1")
        except sqlite3.OperationalError:
            print("[DB] migrating to tall schema (param/value)")
            conn.execute("DROP TABLE IF EXISTS measurements")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                bucket     TEXT NOT NULL,
                station_id TEXT NOT NULL,
                source     TEXT,
                param      TEXT NOT NULL,
                value      REAL NOT NULL,
                PRIMARY KEY (bucket, station_id, param)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sid_param
            ON measurements (station_id, param, bucket DESC)
        """)
        # Persistent registry of every station ever seen — used to keep stale
        # stations on the map when their source API is temporarily unavailable.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS station_meta (
                station_id   TEXT PRIMARY KEY,
                source       TEXT,
                name         TEXT,
                lat          REAL,
                lon          REAL,
                sensor_type  TEXT,
                vendor       TEXT,
                last_seen    TEXT,
                last_aqi     INTEGER,
                last_color   TEXT,
                last_label   TEXT,
                last_readings TEXT
            )
        """)

def _bucket_now():
    """Current UTC time rounded down to 15-min boundary → 'YYYY-MM-DDTHH:MM'."""
    now = datetime.datetime.utcnow()
    return now.replace(minute=(now.minute // 15) * 15,
                       second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

def store_measurements(stations):
    """Store every numeric reading from every station into the tall table."""
    bucket = _bucket_now()
    with _db() as conn:
        for s in stations:
            sid = s["id"]
            src = s.get("source")
            # Store all readings (any param with a numeric value)
            for r in s.get("readings", []):
                param = r.get("type", "").strip()
                val   = r.get("value")
                if not param or val is None:
                    continue
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO measurements "
                        "(bucket, station_id, source, param, value) "
                        "VALUES (?,?,?,?,?)",
                        (bucket, sid, src, param, float(val))
                    )
                except Exception as e:
                    print(f"[DB] store {sid}/{param}: {e}")
            # Also persist AQI as its own param when available
            if s.get("aqi") is not None:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO measurements "
                        "(bucket, station_id, source, param, value) "
                        "VALUES (?,?,?,?,?)",
                        (bucket, sid, src, "AQI", float(s["aqi"]))
                    )
                except Exception as e:
                    print(f"[DB] store {sid}/AQI: {e}")
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)
                  ).strftime("%Y-%m-%dT%H:%M")
        conn.execute("DELETE FROM measurements WHERE bucket < ?", (cutoff,))

def upsert_station_meta(stations):
    """Persist each station's latest state so stale stations can still be shown."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _db() as conn:
        for s in stations:
            if not s.get("lat") or not s.get("lon"):
                continue
            conn.execute("""
                INSERT OR REPLACE INTO station_meta
                    (station_id, source, name, lat, lon, sensor_type, vendor,
                     last_seen, last_aqi, last_color, last_label, last_readings)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                s["id"], s.get("source"), s.get("name"),
                s["lat"], s["lon"],
                s.get("sensor_type"), s.get("vendor"),
                now,
                s.get("aqi"), s.get("color"), s.get("label"),
                json.dumps(s.get("readings", [])),
            ))

def get_stale_stations(live_ids: set) -> list:
    """Return stations seen in the last 7 days that are not in the live set."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=7)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _db() as conn:
        if live_ids:
            ph = ",".join("?" * len(live_ids))
            rows = conn.execute(
                f"SELECT * FROM station_meta "
                f"WHERE last_seen >= ? AND station_id NOT IN ({ph})",
                [cutoff] + list(live_ids)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM station_meta WHERE last_seen >= ?", [cutoff]
            ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id":          r["station_id"],
            "source":      r["source"],
            "name":        r["name"],
            "lat":         r["lat"],
            "lon":         r["lon"],
            "sensor_type": r["sensor_type"],
            "vendor":      r["vendor"],
            "aqi":         r["last_aqi"],
            "color":       r["last_color"],
            "label":       r["last_label"],
            "readings":    json.loads(r["last_readings"] or "[]"),
            "pm25": None, "pm10": None,
            "stale":       True,
            "last_seen":   r["last_seen"],
        })
    return out

def get_station_history(station_id, param="PM2.5", hours=24):
    """Return hours of 15-min buckets for the given param; v=null where no data."""
    now   = datetime.datetime.utcnow()
    start = now - datetime.timedelta(hours=hours)
    cur   = start.replace(minute=(start.minute // 15) * 15,
                          second=0, microsecond=0)
    buckets = []
    while cur <= now:
        buckets.append(cur.strftime("%Y-%m-%dT%H:%M"))
        cur += datetime.timedelta(minutes=15)

    if not buckets:
        return []

    with _db() as conn:
        rows = conn.execute(
            "SELECT bucket, value FROM measurements "
            "WHERE station_id=? AND param=? AND bucket>=? ORDER BY bucket",
            (station_id, param, buckets[0])
        ).fetchall()

    data = {r["bucket"]: r["value"] for r in rows}
    return [{"t": b + ":00Z", "v": data.get(b)} for b in buckets]

# ── In-memory latest snapshot (served by /api/stations) ──────────────────────

_snapshot: dict = {"stations": [], "collected_at": None, "total": 0}
_snapshot_lock = threading.RLock()

def _set_snapshot(stations, ts):
    with _snapshot_lock:
        _snapshot["stations"]     = stations
        _snapshot["collected_at"] = ts
        _snapshot["total"]        = len(stations)

def _get_snapshot():
    with _snapshot_lock:
        return dict(_snapshot)

# ── Station lat/lon registry (for sparkline fallback) ────────────────────────

_registry: dict = {}
_registry_lock  = threading.Lock()

def _register(stations):
    with _registry_lock:
        for s in stations:
            if s.get("lat") is not None and s.get("lon") is not None:
                _registry[s["id"]] = {"lat": s["lat"], "lon": s["lon"]}

def _lookup(sid):
    with _registry_lock:
        return _registry.get(sid)

# ── EU Air Quality Index (EAQI) ───────────────────────────────────────────────
# Breakpoints (µg/m³) → level 1-6.  Upper bound is exclusive; level 6 = above all.
# PM2.5 / PM10 use 24-hour running mean; gases use the latest hourly value.

EAQI_BP = {
    "PM2.5": [(0, 10, 1), (10, 20, 2), (20, 25, 3), (25, 50, 4), (50, 75, 5)],
    "PM10":  [(0, 20, 1), (20, 40, 2), (40, 50, 3), (50, 100, 4), (100, 150, 5)],
    "O₃":   [(0, 50, 1), (50, 100, 2), (100, 130, 3), (130, 240, 4), (240, 380, 5)],
    "NO₂":  [(0, 40, 1), (40, 90, 2), (90, 120, 3), (120, 230, 4), (230, 340, 5)],
    "SO₂":  [(0, 100, 1), (100, 200, 2), (200, 350, 3), (350, 500, 4), (500, 750, 5)],
}

EAQI_META = {
    1: ("#009966", "Very Good"),
    2: ("#33CC33", "Good"),
    3: ("#F0D800", "Medium"),
    4: ("#FF9900", "Poor"),
    5: ("#CC3300", "Very Poor"),
    6: ("#820000", "Extremely Poor"),
}

def _eaqi_level(param, value):
    """Return EAQI level 1-6 for a concentration value, or None if no data."""
    if value is None or value < 0:
        return None
    for lo, hi, level in EAQI_BP.get(param, []):
        if lo <= value < hi:
            return level
    return 6  # above all breakpoints

def _eaqi_qi(level):
    """Return {aqi, color, label} dict for an EAQI level (or None → no data)."""
    if level is None:
        return {"aqi": None, "color": "#aaaaaa", "label": "No data"}
    color, label = EAQI_META.get(level, ("#820000", "Extremely Poor"))
    return {"aqi": level, "color": color, "label": label}

def pm25_to_aqi(pm25):
    """EAQI from a single PM2.5 reading (used during live collection before 24h history exists)."""
    return _eaqi_qi(_eaqi_level("PM2.5", pm25))

def aqi_to_color(raw_aqi):
    """Approximate EAQI level from a US-scale AQI value (used for AQICN stations)."""
    if raw_aqi is None:
        return {"aqi": None, "color": "#aaaaaa", "label": "No data"}
    if raw_aqi <= 33:  level = 1
    elif raw_aqi <= 66:  level = 2
    elif raw_aqi <= 100: level = 3
    elif raw_aqi <= 150: level = 4
    elif raw_aqi <= 200: level = 5
    else:                level = 6
    color, label = EAQI_META[level]
    return {"aqi": level, "color": color, "label": label}

def apply_eaqi(stations):
    """
    Recalculate EAQI for every station using proper averaging windows:
      - PM2.5 and PM10 → 24-hour running mean from the DB
      - O₃ / NO₂ / SO₂  → latest reading (hourly value)
    Falls back to the current reading when the DB has no history yet.
    Updates aqi / color / label in-place on each station dict.
    """
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)
              ).strftime("%Y-%m-%dT%H:%M")

    # Single query: 24h mean for PM2.5 and PM10 across all stations
    with _db() as conn:
        rows = conn.execute(
            "SELECT station_id, param, AVG(value) AS avg_val "
            "FROM measurements "
            "WHERE param IN ('PM2.5','PM10') AND bucket >= ? "
            "GROUP BY station_id, param",
            (cutoff,)
        ).fetchall()

    pm_means = {}           # (station_id, param) → float
    for r in rows:
        pm_means[(r["station_id"], r["param"])] = r["avg_val"]

    for s in stations:
        sid      = s["id"]
        readings = {r["type"]: r["value"] for r in s.get("readings", [])}
        levels   = []

        # PM2.5 — 24h mean, fall back to latest reading
        pm25 = pm_means.get((sid, "PM2.5")) or readings.get("PM2.5")
        lv = _eaqi_level("PM2.5", pm25)
        if lv: levels.append(lv)

        # PM10 — 24h mean, fall back to latest reading
        pm10 = pm_means.get((sid, "PM10")) or readings.get("PM10")
        lv = _eaqi_level("PM10", pm10)
        if lv: levels.append(lv)

        # Gases — latest hourly reading
        for param in ("O₃", "NO₂", "SO₂"):
            lv = _eaqi_level(param, readings.get(param))
            if lv: levels.append(lv)

        if not levels:
            continue    # no concentration data — keep existing color (e.g. AQICN)

        qi = _eaqi_qi(max(levels))
        s["aqi"]   = qi["aqi"]
        s["color"] = qi["color"]
        s["label"] = qi["label"]

def _sf(val):
    if val is None: return None
    try: return float(str(val).strip().lstrip("<").strip())
    except: return None

def _near(a, b, thr=0.05):
    try:
        return abs(a["lat"]-b["lat"]) < thr and abs(a["lon"]-b["lon"]) < thr
    except (KeyError, TypeError):
        return False

# ── ARSO ──────────────────────────────────────────────────────────────────────

ARSO_URL    = "https://www.arso.gov.si/xml/zrak/ones_zrak_urni_podatki_zadnji.xml"
ARSO_PARAMS = {
    "pm2.5":  ("PM2.5",   "µg/m³"),
    "pm10":   ("PM10",    "µg/m³"),
    "no2":    ("NO₂",     "µg/m³"),
    "o3":     ("O₃",      "µg/m³"),
    "so2":    ("SO₂",     "µg/m³"),
    "co":     ("CO",      "mg/m³"),
    "benzen": ("Benzene", "µg/m³"),
}

def _fetch_arso():
    try:
        r = requests.get(ARSO_URL, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[ARSO] {e}"); return []
    out = []
    for p in root.findall("postaja"):
        lat = _sf(p.get("wgs84_sirina"))
        lon = _sf(p.get("wgs84_dolzina"))
        if not lat or not lon: continue
        readings, pm25 = [], None
        for tag, (lbl, unit) in ARSO_PARAMS.items():
            v = _sf(p.findtext(tag))
            if v is not None:
                readings.append({"type": lbl, "value": round(v,2), "unit": unit})
                if tag == "pm2.5": pm25 = v
        qi = pm25_to_aqi(pm25)
        out.append({
            "id": f"arso_{p.get('sifra','')}", "source": "ARSO",
            "name": p.findtext("merilno_mesto","").strip(),
            "lat": lat, "lon": lon, "pm25": pm25, "pm10": _sf(p.findtext("pm10")),
            "aqi": qi["aqi"], "color": qi["color"], "label": qi["label"],
            "sensor_type": "Reference monitor",
            "vendor": "ARSO (Agencija RS za okolje)",
            "altitude_m": _sf(p.get("nadm_visina")), "readings": readings,
        })
    return out

# ── Sensor.Community ──────────────────────────────────────────────────────────

SC_URL = "https://data.sensor.community/airrohr/v1/filter/country=SI"

def _fetch_sc():
    try:
        r = requests.get(SC_URL, headers={"User-Agent":"airq-si/1.0"}, timeout=20)
        r.raise_for_status()
        return _parse_sc(r.json())
    except Exception as e:
        print(f"[SC] {e}"); return []

SC_VTYPE_LABEL = {
    "P2": ("PM2.5",    "µg/m³"),
    "P1": ("PM10",     "µg/m³"),
    "temperature":     ("Temperature", "°C"),
    "humidity":        ("Humidity",    "%"),
    "pressure":        ("Pressure",    "hPa"),
    "pressure_at_sealevel": ("Pressure (sea)", "hPa"),
}

def _parse_sc(raw):
    """
    Group API entries by location.id so each physical SC box → one station.
    The top-level entry `id` is a per-submission batch ID that changes every poll;
    location.id is stable.  Using location.id gives consistent station IDs across
    collections, which is required for history to accumulate correctly.
    """
    # Bucket all entries by location id
    from collections import defaultdict
    by_loc = defaultdict(list)
    for entry in raw:
        lid = entry.get("location", {}).get("id")
        if lid:
            by_loc[lid].append(entry)

    out = []
    for loc_id, entries in by_loc.items():
        loc = entries[0].get("location", {})
        lat = _sf(str(loc.get("latitude", "")))
        lon = _sf(str(loc.get("longitude", "")))
        if not lat or not lon:
            continue

        pm25 = pm10 = None
        readings  = []
        stypes    = set()
        seen_type = set()   # avoid duplicate reading labels from multiple sensors

        for entry in entries:
            st = entry.get("sensor", {}).get("sensor_type", {}).get("name", "")
            if st:
                stypes.add(st)
            for sv in entry.get("sensordatavalues", []):
                vt = sv.get("value_type", "")
                v  = _sf(str(sv.get("value", "")))
                if v is None:
                    continue
                label, unit = SC_VTYPE_LABEL.get(vt, (vt, ""))
                if label in seen_type:
                    continue        # prefer first sensor's reading for this type
                seen_type.add(label)
                readings.append({"type": label, "value": round(v, 1), "unit": unit})
                if vt == "P2":  pm25 = v
                elif vt == "P1": pm10 = v

        qi = pm25_to_aqi(pm25)
        out.append({
            "id":          f"sc_{loc_id}",   # stable location ID
            "source":      "Sensor.Community",
            "name":        f"SC {loc_id}",
            "lat": lat, "lon": lon,
            "pm25": pm25, "pm10": pm10,
            "aqi": qi["aqi"], "color": qi["color"], "label": qi["label"],
            "sensor_type": ", ".join(sorted(stypes)) or "Unknown",
            "vendor":      "Sensor.Community (crowdsourced)",
            "readings":    readings,
        })
    return out

# ── OpenSenseMap ──────────────────────────────────────────────────────────────

OSM_BBOX = "https://api.opensensemap.org/boxes?bbox=13.38,45.42,16.61,46.87"
OSM_BOX  = "https://api.opensensemap.org/boxes/{id}"

def _parse_osm_box(b):
    coords = (b.get("currentLocation") or {}).get("coordinates",[])
    if len(coords)<2: return None
    lon, lat = float(coords[0]), float(coords[1])
    pm25=pm10=None; readings=[]; stypes=set()
    for s in b.get("sensors",[]):
        title=s.get("title",""); stype=s.get("sensorType","")
        if stype: stypes.add(stype)
        last=s.get("lastMeasurement") or {}
        v=_sf(str(last.get("value","")))
        if v is None: continue
        readings.append({"type":title,"value":round(v,2),"unit":s.get("unit","µg/m³")})
        if "PM2.5" in title or "PM 2.5" in title: pm25=v
        elif "PM10" in title: pm10=v
    qi=pm25_to_aqi(pm25)
    return {"id":f"osm_{b.get('_id','')}", "source":"OpenSenseMap",
            "name":b.get("name",b.get("_id","")), "lat":lat,"lon":lon,
            "pm25":pm25,"pm10":pm10,
            "aqi":qi["aqi"],"color":qi["color"],"label":qi["label"],
            "sensor_type":", ".join(stypes) or "Unknown",
            "vendor":"OpenSenseMap (citizen science)","readings":readings}

def _fetch_one_osm(bid):
    try:
        r=requests.get(OSM_BOX.format(id=bid),timeout=10); r.raise_for_status()
        return _parse_osm_box(r.json())
    except Exception as e:
        print(f"[OSM] {bid}: {e}"); return None

def _fetch_osm():
    try:
        r=requests.get(OSM_BBOX,timeout=15); r.raise_for_status(); boxes=r.json()
    except Exception as e:
        print(f"[OSM] bbox: {e}"); return []
    cutoff=time.time()-48*3600; active=[]
    for b in boxes:
        ts=b.get("lastMeasurementAt","")
        if not ts: continue
        try:
            t=datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
            if t.timestamp()<cutoff: continue
        except: continue
        if any("PM" in s.get("title","") for s in b.get("sensors",[])): active.append(b["_id"])
    out=[]
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed({ex.submit(_fetch_one_osm,bid):bid for bid in active}):
            r=fut.result()
            if r: out.append(r)
    return out

# ── PurpleAir ─────────────────────────────────────────────────────────────────

PA_URL    = "https://api.purpleair.com/v1/sensors"
PA_FIELDS = "sensor_index,name,latitude,longitude,location_type,pm2.5_atm,pm10.0_atm,humidity,temperature"
PA_BBOX   = {"nwlng":13.0,"nwlat":47.0,"selng":17.0,"selat":45.4}

def _fetch_purpleair():
    if not PURPLEAIR_KEY: return []
    try:
        r=requests.get(PA_URL,headers={"X-API-Key":PURPLEAIR_KEY},
                       params={**PA_BBOX,"fields":PA_FIELDS},timeout=15)
        r.raise_for_status(); d=r.json()
    except Exception as e:
        print(f"[PurpleAir] {e}"); return []
    fields=d.get("fields",[]); out=[]
    for row in d.get("data",[]):
        s=dict(zip(fields,row))
        lat=_sf(str(s.get("latitude",""))); lon=_sf(str(s.get("longitude","")))
        if not lat or not lon: continue
        if s.get("location_type")!=0: continue
        if not (45.4<=lat<=46.9 and 13.3<=lon<=16.7): continue
        pm25=_sf(str(s.get("pm2.5_atm","")))
        pm10=_sf(str(s.get("pm10.0_atm","")))
        qi=pm25_to_aqi(pm25); readings=[]
        if pm25 is not None: readings.append({"type":"PM2.5","value":round(pm25,1),"unit":"µg/m³"})
        if pm10 is not None: readings.append({"type":"PM10","value":round(pm10,1),"unit":"µg/m³"})
        hum=_sf(str(s.get("humidity",""))); tmp=_sf(str(s.get("temperature","")))
        if hum is not None: readings.append({"type":"Humidity","value":round(hum,1),"unit":"%"})
        if tmp is not None: readings.append({"type":"Temperature","value":round((tmp-32)*5/9,1),"unit":"°C"})
        idx=s.get("sensor_index","")
        out.append({
            "id":f"pa_{idx}","source":"PurpleAir","name":s.get("name",f"PA {idx}"),
            "lat":lat,"lon":lon,"pm25":pm25,"pm10":pm10,
            "aqi":qi["aqi"],"color":qi["color"],"label":qi["label"],
            "sensor_type":"Plantower PMS5003A","vendor":"PurpleAir (crowdsourced)",
            "readings":readings,
        })
    return out

# ── AQICN ─────────────────────────────────────────────────────────────────────

AQICN_BOUNDS = "https://api.waqi.info/map/bounds/?latlng=45.42,13.38,46.87,16.61&token={t}"
AQICN_FEED   = "https://api.waqi.info/feed/@{uid}/?token={t}"

# US EPA AQI sub-index → concentration reverse lookup tables
# AQICN's iaqi values are US AQI sub-indices, NOT µg/m³ concentrations.
# We reverse the EPA formula to get µg/m³ so EAQI breakpoints can be applied.
_US_AQI_PM25 = [   # (aqi_lo, aqi_hi, conc_lo, conc_hi)  — µg/m³
    (0,   50,  0.0,   12.0),
    (51,  100, 12.1,  35.4),
    (101, 150, 35.5,  55.4),
    (151, 200, 55.5,  150.4),
    (201, 300, 150.5, 250.4),
    (301, 400, 250.5, 350.4),
    (401, 500, 350.5, 500.4),
]
_US_AQI_PM10 = [   # µg/m³
    (0,   50,  0,   54),
    (51,  100, 55,  154),
    (101, 150, 155, 254),
    (151, 200, 255, 354),
    (201, 300, 355, 424),
    (301, 400, 425, 504),
    (401, 500, 505, 604),
]

def _us_aqi_to_conc(aqi_val, bp_table):
    """Reverse US EPA AQI sub-index → pollutant concentration (µg/m³)."""
    if aqi_val is None:
        return None
    for a_lo, a_hi, c_lo, c_hi in bp_table:
        if a_lo <= aqi_val <= a_hi:
            return round(c_lo + (aqi_val - a_lo) / (a_hi - a_lo) * (c_hi - c_lo), 1)
    return None

# Gas readings are kept as AQI sub-index values (unit conversion ppb→µg/m³
# requires temperature/pressure and is not worth the complexity for display).
AQICN_GAS_LABELS = {
    "no2": ("NO₂", "AQI idx"),
    "o3":  ("O₃",  "AQI idx"),
    "so2": ("SO₂", "AQI idx"),
    "co":  ("CO",  "AQI idx"),
    "t":   ("Temperature", "°C"),
    "h":   ("Humidity",    "%"),
    "p":   ("Pressure",    "hPa"),
}

def _fetch_one_aqicn(uid):
    try:
        r = requests.get(AQICN_FEED.format(uid=uid, t=AQICN_TOKEN), timeout=10)
        r.raise_for_status()
        d = r.json().get("data", {})
        if not d or d == "Unknown station": return None
        aqi = d.get("aqi")
        try: aqi = int(aqi)
        except: return None
        geo  = d.get("city", {}).get("geo", [None, None])
        iaqi = d.get("iaqi", {})
        readings = []

        # PM2.5 — reverse US AQI sub-index → µg/m³ so EAQI can be applied
        pm25_conc = _us_aqi_to_conc(iaqi.get("pm25", {}).get("v"), _US_AQI_PM25)
        if pm25_conc is not None:
            readings.append({"type": "PM2.5", "value": pm25_conc, "unit": "µg/m³"})

        # PM10 — same reverse conversion
        pm10_conc = _us_aqi_to_conc(iaqi.get("pm10", {}).get("v"), _US_AQI_PM10)
        if pm10_conc is not None:
            readings.append({"type": "PM10", "value": pm10_conc, "unit": "µg/m³"})

        # Gas pollutants + met — keep as AQI index values (displayed in card)
        for k, (lbl, unit) in AQICN_GAS_LABELS.items():
            v = iaqi.get(k, {}).get("v")
            if v is not None:
                readings.append({"type": lbl, "value": round(float(v), 1), "unit": unit})

        ci = aqi_to_color(aqi)
        return {"uid": uid, "name": d.get("city", {}).get("name", str(uid)),
                "lat":  float(geo[0]) if geo[0] else None,
                "lon":  float(geo[1]) if geo[1] else None,
                "aqi": aqi, "color": ci["color"], "label": ci["label"],
                "dominant": d.get("dominentpol", ""), "readings": readings}
    except Exception as e:
        print(f"[AQICN] {uid}: {e}"); return None

# Cache the last successful AQICN new-stations list so a transient API error
# doesn't wipe those stations from the map.
_aqicn_cache: list  = []
_aqicn_cache_time: float = 0.0   # epoch seconds of last *fresh* fetch
_aqicn_cache_lock = threading.Lock()

AQICN_CACHE_TTL = 2 * 3600   # seconds — after this, cached stations become stale

def _apply_aqicn(primary):
    """Upgrade existing stations' AQI with AQICN multi-pollutant values.
    Returns list of truly new cross-border stations only.
    On API failure falls back to the last successful result for up to
    AQICN_CACHE_TTL seconds; after that returns [] so those stations
    fall out of the live set and are shown as stale markers."""
    global _aqicn_cache, _aqicn_cache_time
    if not AQICN_TOKEN: return []

    # Try up to 2 times before giving up
    raw = None
    for attempt in range(2):
        try:
            r = requests.get(AQICN_BOUNDS.format(t=AQICN_TOKEN), timeout=15)
            r.raise_for_status()
            raw = r.json().get("data", [])
            break
        except Exception as e:
            print(f"[AQICN] bounds attempt {attempt+1}: {e}")
            if attempt == 0:
                time.sleep(3)   # brief pause before retry

    if raw is None:
        # Both attempts failed
        with _aqicn_cache_lock:
            cached    = list(_aqicn_cache)
            cache_age = time.time() - _aqicn_cache_time if _aqicn_cache_time else float("inf")

        age_min = int(cache_age // 60) if cache_age < 1e9 else None

        if cache_age <= AQICN_CACHE_TTL:
            print(f"[AQICN] using cached {len(cached)} stations "
                  f"(age {age_min} min)")
            return cached
        else:
            label = f"{age_min} min old" if age_min is not None else "never fetched"
            print(f"[AQICN] cache expired ({label}) — "
                  "AQICN stations will appear as stale")
            return []

    valid=[]
    for s in raw:
        try: aqi=int(s.get("aqi","-"))
        except: continue
        lat=_sf(str(s.get("lat",""))); lon=_sf(str(s.get("lon","")))
        if lat and lon: valid.append({"uid":s["uid"],"lat":lat,"lon":lon,"aqi":aqi})

    new_uids=[]
    for aq in valid:
        matched=next((st for st in primary if _near(st,aq,0.05)),None)
        if matched:
            c=aqi_to_color(aq["aqi"])
            matched.update({"aqi":aq["aqi"],"color":c["color"],"label":c["label"],
                             "aqi_source":"AQICN"})
        else:
            new_uids.append(aq["uid"])

    new_stations=[]
    if new_uids:
        with ThreadPoolExecutor(max_workers=10) as ex:
            for fut in as_completed({ex.submit(_fetch_one_aqicn,uid):uid for uid in new_uids}):
                d=fut.result()
                if d and d["lat"] and d["lon"]:
                    new_stations.append({
                        "id":f"aqicn_{d['uid']}","source":"AQICN","name":d["name"],
                        "lat":d["lat"],"lon":d["lon"],"pm25":None,"pm10":None,
                        "aqi":d["aqi"],"color":d["color"],"label":d["label"],
                        "sensor_type":"Reference monitor",
                        "vendor":f"AQICN · dominant: {d['dominant']}",
                        "readings":d["readings"],
                    })

    # Update cache with the fresh result and record when we got it
    with _aqicn_cache_lock:
        _aqicn_cache      = list(new_stations)
        _aqicn_cache_time = time.time()

    return new_stations

# ── OpenAQ ────────────────────────────────────────────────────────────────────
# OpenAQ v3 API — adds Slovenian stations not already covered by ARSO.
# Deduplication against primary sources is done by proximity (0.05°).

OPENAQ_BBOX_URL   = ("https://api.openaq.org/v3/locations"
                     "?bbox=13.38,45.42,16.61,46.87&limit=200")
OPENAQ_LATEST_URL = "https://api.openaq.org/v3/locations/{id}/latest"

# OpenAQ parameter names → internal label + unit
OPENAQ_PARAM_MAP = {
    "pm25":             ("PM2.5",       "µg/m³"),
    "pm10":             ("PM10",        "µg/m³"),
    "pm1":              ("PM1",         "µg/m³"),
    "no2":              ("NO₂",         "µg/m³"),
    "o3":               ("O₃",          "µg/m³"),
    "so2":              ("SO₂",         "µg/m³"),
    "co":               ("CO",          "µg/m³"),
    "no":               ("NO",          "µg/m³"),
    "relativehumidity": ("Humidity",    "%"),
    "temperature":      ("Temperature", "°C"),
}
OPENAQ_SKIP_PARAMS = {"um003"}   # particle count — not a mass concentration

def _fetch_one_openaq(loc, sensor_map):
    """Fetch and parse the latest measurements for one OpenAQ location."""
    lid = loc["id"]
    try:
        r = requests.get(
            OPENAQ_LATEST_URL.format(id=lid),
            headers={"X-API-Key": OPENAQ_KEY},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        print(f"[OpenAQ] {lid}: {e}")
        return None

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    readings = []
    pm25 = pm10 = None

    for entry in results:
        val = entry.get("value")
        if val is None:
            continue
        # Skip measurements older than 3 hours (hourly-reporting stations)
        ts_str = (entry.get("datetime") or {}).get("utc", "")
        try:
            ts  = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = (now_utc - ts).total_seconds()
            if age > 10800:
                continue
        except Exception:
            pass

        param_raw = sensor_map.get(entry["sensorsId"])
        if not param_raw or param_raw in OPENAQ_SKIP_PARAMS:
            continue
        if param_raw not in OPENAQ_PARAM_MAP:
            continue

        label, unit = OPENAQ_PARAM_MAP[param_raw]
        readings.append({"type": label, "value": round(float(val), 2), "unit": unit})
        if param_raw == "pm25":
            pm25 = float(val)
        elif param_raw == "pm10":
            pm10 = float(val)

    if not readings:
        return None

    coords = loc["coordinates"]
    qi = pm25_to_aqi(pm25)
    instruments = {i["name"] for i in loc.get("instruments", [])} - {""}
    provider    = loc.get("provider", {}).get("name", "Unknown")
    return {
        "id":          f"oaq_{lid}",
        "source":      "OpenAQ",
        "name":        loc["name"],
        "lat":         coords["latitude"],
        "lon":         coords["longitude"],
        "pm25": pm25, "pm10": pm10,
        "aqi":   qi["aqi"], "color": qi["color"], "label": qi["label"],
        "sensor_type": ", ".join(sorted(instruments)) if instruments else "Sensor",
        "vendor":      f"OpenAQ · {provider}",
        "readings":    readings,
    }

def _fetch_openaq(primary):
    """
    Fetch active Slovenian stations from OpenAQ that are not already in the
    primary set (ARSO / Sensor.Community / OpenSenseMap / PurpleAir).
    Only SI-country stations last-seen within 48 h are considered.
    """
    if not OPENAQ_KEY:
        return []
    try:
        r = requests.get(
            OPENAQ_BBOX_URL,
            headers={"X-API-Key": OPENAQ_KEY},
            timeout=15,
        )
        r.raise_for_status()
        locations = r.json().get("results", [])
    except Exception as e:
        print(f"[OpenAQ] bbox: {e}")
        return []

    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=48))

    # Filter to active SI stations and build per-location sensor maps
    candidates = []
    for loc in locations:
        if loc.get("country", {}).get("code") != "SI":
            continue
        ts_str = (loc.get("datetimeLast") or {}).get("utc", "")
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            continue
        sensor_map = {s["id"]: s["parameter"]["name"]
                      for s in loc.get("sensors", [])}
        candidates.append((loc, sensor_map))

    # Drop any location close to an already-known primary station
    new_locs = []
    for loc, sm in candidates:
        c = loc["coordinates"]
        fake = {"lat": c["latitude"], "lon": c["longitude"]}
        if not any(_near(st, fake, 0.05) for st in primary):
            new_locs.append((loc, sm))

    if not new_locs:
        return []

    print(f"[OpenAQ] fetching latest for {len(new_locs)} new locations…")
    out = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_openaq, loc, sm): loc
                   for loc, sm in new_locs}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                out.append(result)

    print(f"[OpenAQ] {len(out)} new stations")
    return out

# ── Collector ─────────────────────────────────────────────────────────────────

def _run_collection():
    """Fetch all sources, update snapshot + registry, store to DB."""
    print("[collector] collecting…")
    t0=time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        fa=ex.submit(_fetch_arso); fsc=ex.submit(_fetch_sc)
        fosm=ex.submit(_fetch_osm); fpa=ex.submit(_fetch_purpleair)
        arso=fa.result(); sc=fsc.result(); osm=fosm.result(); pa=fpa.result()
    primary = arso + sc + osm + pa
    aqicn   = _apply_aqicn(primary)
    openaq  = _fetch_openaq(primary)
    live    = primary + aqicn + openaq

    # Mark all live stations as fresh
    for s in live:
        s["stale"]     = False
        s["last_seen"] = None

    store_measurements(live)        # persist to DB first so 24h means are current
    apply_eaqi(live)               # recalculate colours using 24h rolling means

    # Persist latest metadata for every live station (with updated EAQI colours)
    upsert_station_meta(live)

    # Merge in stations that were seen recently but missing from this collection
    live_ids = {s["id"] for s in live}
    stale    = get_stale_stations(live_ids)

    all_st = live + stale
    ts     = datetime.datetime.utcnow()
    _set_snapshot(all_st, ts)
    _register(all_st)
    print(f"[collector] done — {len(live)} live + {len(stale)} stale "
          f"= {len(all_st)} stations in {time.time()-t0:.1f}s")

def _collector_loop():
    """Run immediately, then at every 15-min UTC boundary."""
    _run_collection()
    while True:
        now  = datetime.datetime.utcnow()
        mins = now.minute % 15
        secs = now.second
        wait = (15 - mins) * 60 - secs
        print(f"[collector] next in {wait}s")
        time.sleep(wait)
        _run_collection()

# ── CAMS model grid ───────────────────────────────────────────────────────────
# Slovenia bbox: lat 45.25–46.75, lon 13.25–16.75, 0.25° grid → 7×15 = 105 pts
_CAMS_LATS = [round(45.25 + i * 0.25, 2) for i in range(7)]
_CAMS_LONS = [round(13.25 + i * 0.25, 2) for i in range(15)]
_cams_grid_cache: dict = {"data": None, "ts": 0.0}
_cams_grid_lock = threading.Lock()
CAMS_GRID_TTL = 3600  # refresh once per hour

def _fetch_cams_point(lat, lon):
    url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=pm2_5,pm10,nitrogen_dioxide,ozone"
        "&forecast_days=1&past_days=0&timezone=UTC"
    )
    try:
        r = requests.get(url, timeout=10)
        d = r.json()
        times = d["hourly"]["time"]
        now_h = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:00")
        idx = next((i for i, t in enumerate(times) if t >= now_h), len(times) - 1)
        vals = {
            "PM2.5": (d["hourly"].get("pm2_5")     or [None] * len(times))[idx],
            "PM10":  (d["hourly"].get("pm10")       or [None] * len(times))[idx],
            "NO₂":   (d["hourly"].get("nitrogen_dioxide") or [None] * len(times))[idx],
            "O₃":    (d["hourly"].get("ozone")      or [None] * len(times))[idx],
        }
        levels = [lv for lv in (_eaqi_level(p, v) for p, v in vals.items()) if lv]
        level  = max(levels) if levels else None
        qi     = _eaqi_qi(level)
        return {
            "lat": lat, "lon": lon,
            "level": level, "color": qi["color"],
            "pm25": vals["PM2.5"], "pm10": vals["PM10"],
            "no2":  vals["NO₂"],  "o3":   vals["O₃"],
        }
    except Exception as e:
        return {"lat": lat, "lon": lon, "level": None, "color": "#aaaaaa"}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/stations")
def stations():
    snap = _get_snapshot()
    # If collector hasn't run yet, run synchronously once
    if not snap["stations"]:
        _run_collection()
        snap = _get_snapshot()
    ts = snap["collected_at"]
    return jsonify({
        "stations":    snap["stations"],
        "collected_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
        "total":       snap["total"],
    })

@app.route("/api/cams")
def cams_grid():
    with _cams_grid_lock:
        if _cams_grid_cache["data"] and time.time() - _cams_grid_cache["ts"] < CAMS_GRID_TTL:
            return jsonify(_cams_grid_cache["data"])

    coords = [(lat, lon) for lat in _CAMS_LATS for lon in _CAMS_LONS]
    points = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_fetch_cams_point, lat, lon): (lat, lon) for lat, lon in coords}
        for f in as_completed(futs):
            points.append(f.result())

    result = {
        "points":     points,
        "cell_deg":   0.25,
        "fetched_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _cams_grid_lock:
        _cams_grid_cache["data"] = result
        _cams_grid_cache["ts"]   = time.time()
    return jsonify(result)

@app.route("/api/history/<station_id>")
def history(station_id):
    param = request.args.get("param", "PM2.5")
    pts   = get_station_history(station_id, param=param, hours=24)
    has_data = any(p["v"] is not None for p in pts)
    return jsonify({"points": pts, "has_data": has_data, "param": param})

@app.route("/api/status")
def status():
    snap = _get_snapshot()
    ts   = snap["collected_at"]
    now  = datetime.datetime.utcnow()
    mins = now.minute % 15; secs = now.second
    next_in = (15 - mins) * 60 - secs
    return jsonify({
        "collected_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
        "total":        snap["total"],
        "next_in_seconds": next_in,
    })

# ── Startup ───────────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    threading.Thread(target=_collector_loop, daemon=True).start()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=8060, debug=debug)
