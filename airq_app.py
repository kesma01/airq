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

# ── AQI helpers ───────────────────────────────────────────────────────────────

AQI_BP = [
    (0,    12.0,  "Good",               "#00c400", 0,   50),
    (12.1, 35.4,  "Moderate",           "#e8c300", 51,  100),
    (35.5, 55.4,  "Unhealthy for Some", "#ff7e00", 101, 150),
    (55.5, 150.4, "Unhealthy",          "#e00000", 151, 200),
    (150.5,250.4, "Very Unhealthy",     "#8f3f97", 201, 300),
    (250.5, 9999, "Hazardous",          "#7e0023", 301, 500),
]

def pm25_to_aqi(pm25):
    if pm25 is None or pm25 < 0:
        return {"aqi": None, "color": "#aaaaaa", "label": "No data"}
    for lo, hi, label, color, a_lo, a_hi in AQI_BP:
        if lo <= pm25 <= hi:
            return {"aqi": round(a_lo + (pm25-lo)/(hi-lo)*(a_hi-a_lo)),
                    "color": color, "label": label}
    return {"aqi": 500, "color": "#7e0023", "label": "Hazardous"}

def aqi_to_color(aqi):
    if aqi is None:
        return {"color": "#aaaaaa", "label": "No data"}
    for _, _, label, color, a_lo, a_hi in AQI_BP:
        if a_lo <= aqi <= a_hi:
            return {"color": color, "label": label}
    return {"color": "#7e0023", "label": "Hazardous"}

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
AQICN_LABELS = {
    "pm25":("PM2.5 (AQI idx)","AQI"),"pm10":("PM10 (AQI idx)","AQI"),
    "no2":("NO₂ (AQI idx)","AQI"),"o3":("O₃ (AQI idx)","AQI"),
    "so2":("SO₂ (AQI idx)","AQI"),"co":("CO (AQI idx)","AQI"),
    "t":("Temperature","°C"),"h":("Humidity","%"),"p":("Pressure","hPa"),
}

def _fetch_one_aqicn(uid):
    try:
        r=requests.get(AQICN_FEED.format(uid=uid,t=AQICN_TOKEN),timeout=10); r.raise_for_status()
        d=r.json().get("data",{})
        if not d or d=="Unknown station": return None
        aqi=d.get("aqi")
        try: aqi=int(aqi)
        except: return None
        geo=d.get("city",{}).get("geo",[None,None]); iaqi=d.get("iaqi",{})
        readings=[]
        for k,(lbl,unit) in AQICN_LABELS.items():
            v=iaqi.get(k,{}).get("v")
            if v is not None: readings.append({"type":lbl,"value":round(float(v),1),"unit":unit})
        ci=aqi_to_color(aqi)
        return {"uid":uid,"name":d.get("city",{}).get("name",str(uid)),
                "lat":float(geo[0]) if geo[0] else None,
                "lon":float(geo[1]) if geo[1] else None,
                "aqi":aqi,"color":ci["color"],"label":ci["label"],
                "dominant":d.get("dominentpol",""),"readings":readings}
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
    live    = primary + aqicn

    # Mark all live stations as fresh
    for s in live:
        s["stale"]     = False
        s["last_seen"] = None

    # Persist latest metadata for every live station
    upsert_station_meta(live)

    # Merge in stations that were seen recently but missing from this collection
    live_ids = {s["id"] for s in live}
    stale    = get_stale_stations(live_ids)

    all_st = live + stale
    ts     = datetime.datetime.utcnow()
    _set_snapshot(all_st, ts)
    _register(all_st)
    store_measurements(live)   # only store measurements for fresh data
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
