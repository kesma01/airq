"use strict";

// ── Language strings ──────────────────────────────────────────────────────────
const LANG = {
  sl: {
    title:        "Kakovost zraka v Sloveniji",
    staleNotice:  "Postaja trenutno ni dosegljiva",
    lastSeen:     "Zadnji podatki",
    loading:      "Nalaganje postaj…",
    good:         "Dobro", moderate: "Zmerno", unhealthyS: "Nezdravo·Ob",
    unhealthy:    "Nezdravo", veryUnhealthy: "Zelo nezdravo",
    hazardous:    "Nevarno", noData: "Ni podatkov",
    source:       "Vir", sensorType: "Tip senzorja", vendor: "Ponudnik",
    history:      "zadnjih 24 ur",
    histModel:    "(CAMS model)",
    noHistory:    "Za to postajo še ni zbranih zgodovinskih podatkov.",
    lastUpdated:  "Zadnja posodobitev",
    ago:          "min nazaj",
    stationsCount: "postaj",
    darkMode:     "🌙 Temno",
    lightMode:    "☀️ Svetlo",
    langToggle:   "EN",
    eaqiTitle:    "EU Indeks kakovosti zraka (µg/m³)",
    eaqiNote:     "PM₁₀ in PM₂.₅ temeljita na 24-urnem drsečem povprečju",
    eaqiLevels:   ["Zelo dobro","Dobro","Srednje","Slabo","Zelo slabo","Izjemno slabo"],
    eaqiPollutants: ["O₃","NO₂","SO₂","PM₁₀","PM₂.₅"],
    legVeryGood: "Zelo dobro", legGood: "Dobro", legMedium: "Srednje",
    legPoor: "Slabo", legVeryPoor: "Zelo slabo", legExtremelyPoor: "Izjemno slabo",
    camsBtn:      "🌫️ Model",
    camsLoading:  "Nalaganje modela…",
    camsLabel:    "CAMS model · trenutna ura",
  },
  en: {
    title:        "Slovenia Air Quality",
    staleNotice:  "Station currently unavailable",
    lastSeen:     "Last data",
    loading:      "Loading stations…",
    good:         "Good", moderate: "Moderate", unhealthyS: "Unhealthy·S",
    unhealthy:    "Unhealthy", veryUnhealthy: "Very Unhealthy",
    hazardous:    "Hazardous", noData: "No data",
    source:       "Source", sensorType: "Sensor type", vendor: "Vendor / Provider",
    history:      "past 24 h",
    histModel:    "(CAMS model)",
    noHistory:    "No historical data collected yet for this sensor.",
    lastUpdated:  "Last updated",
    ago:          "min ago",
    stationsCount: "stations",
    darkMode:     "🌙 Dark",
    lightMode:    "☀️ Light",
    langToggle:   "SL",
    eaqiTitle:    "EU Air Quality Index (µg/m³)",
    eaqiNote:     "PM₁₀ and PM₂.₅ based on 24-hour running mean",
    eaqiLevels:   ["Very Good","Good","Medium","Poor","Very Poor","Extremely Poor"],
    eaqiPollutants: ["O₃","NO₂","SO₂","PM₁₀","PM₂.₅"],
    legVeryGood: "Very Good", legGood: "Good", legMedium: "Medium",
    legPoor: "Poor", legVeryPoor: "Very Poor", legExtremelyPoor: "Extremely Poor",
    camsBtn:      "🌫️ Model",
    camsLoading:  "Loading model…",
    camsLabel:    "CAMS model · current hour",
  },
};

// ── EU Air Quality Index reference table ──────────────────────────────────────
const EAQI_LEVELS = [
  { color: "#009966", textColor: "#fff" },
  { color: "#33CC33", textColor: "#fff" },
  { color: "#F0D800", textColor: "#333" },
  { color: "#FF9900", textColor: "#fff" },
  { color: "#CC3300", textColor: "#fff" },
  { color: "#820000", textColor: "#fff" },
];

// Concentration ranges per pollutant per level [µg/m³]
const EAQI_RANGES = [
  ["0–50",  "50–100", "100–130", "130–240", "240–380", "380–800"],  // O₃
  ["0–40",  "40–90",  "90–120",  "120–230", "230–340", "340–1000"], // NO₂
  ["0–100", "100–200","200–350", "350–500", "500–750", "750–1250"], // SO₂
  ["0–20",  "20–40",  "40–50",   "50–100",  "100–150", "150–1200"], // PM₁₀
  ["0–10",  "10–20",  "20–25",   "25–50",   "50–75",   "75–800"],   // PM₂.₅
];

function buildEaqiTable() {
  const levels     = t("eaqiLevels");
  const pollutants = t("eaqiPollutants");

  // Level header cells
  const headerCells = EAQI_LEVELS.map((lv, i) => `
    <th style="background:${lv.color};color:${lv.textColor}">
      <span class="eaqi-num">${i + 1}</span>
      <span class="eaqi-lbl">${levels[i]}</span>
    </th>`).join("");

  // Pollutant rows
  const rows = EAQI_RANGES.map((ranges, pi) => {
    const cells = ranges.map((val, li) => {
      const lv = EAQI_LEVELS[li];
      return `<td style="background:${lv.color};color:${lv.textColor}">${val}</td>`;
    }).join("");
    return `<tr><td class="eaqi-poll">${pollutants[pi]}</td>${cells}</tr>`;
  }).join("");

  return `
    <div class="p-eaqi">
      <div class="p-chart-title" style="margin-top:14px">${t("eaqiTitle")}</div>
      <div class="eaqi-scroll">
        <table class="eaqi-table">
          <thead><tr><th></th>${headerCells}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="eaqi-note">${t("eaqiNote")}</div>
    </div>`;
}

// ── Persisted preferences ─────────────────────────────────────────────────────
let lang  = localStorage.getItem("airq_lang")  || "sl";
let theme = localStorage.getItem("airq_theme") || "light";

function t(key) { return (LANG[lang] || LANG.sl)[key] || key; }

function applyTheme() {
  document.documentElement.setAttribute("data-theme", theme);
  document.getElementById("btn-theme").textContent =
    theme === "dark" ? t("lightMode") : t("darkMode");
  // Swap map tile layer
  if (tileLayer) {
    map.removeLayer(tileLayer);
    tileLayer = L.tileLayer(
      theme === "dark"
        ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      { attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
        subdomains: "abcd", maxZoom: 19 }
    ).addTo(map);
  }
}

function applyLang() {
  document.getElementById("title").textContent     = t("title");
  document.getElementById("loading-text").textContent = t("loading");
  document.getElementById("btn-lang").textContent  = t("langToggle");
  document.getElementById("btn-theme").textContent =
    theme === "dark" ? t("lightMode") : t("darkMode");
  // Legend labels
  const keys = ["legVeryGood","legGood","legMedium","legPoor","legVeryPoor","legExtremelyPoor","noData"];
  document.querySelectorAll(".leg").forEach((el, i) => {
    el.lastChild.textContent = " " + t(keys[i]);
  });
}

// ── Map init ──────────────────────────────────────────────────────────────────
const map = L.map("map", { center: [46.15, 14.99], zoom: 9, zoomControl: true });
let tileLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  { attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
    subdomains: "abcd", maxZoom: 19 }
).addTo(map);

// ── State ─────────────────────────────────────────────────────────────────────
let allStations  = [];
let activeChart  = null;
let collectedAt  = null;
let camsLayer    = null;   // custom canvas L.Layer
let camsVisible  = false;
let sloveniaGeoJSON = null; // cached boundary

// ── Cluster layer ─────────────────────────────────────────────────────────────
let markerLayer = L.markerClusterGroup({
  maxClusterRadius: 50,          // px — tighter clustering
  showCoverageOnHover: false,    // skip the blue polygon
  zoomToBoundsOnClick: true,
  spiderfyOnMaxZoom: true,
  iconCreateFunction(cluster) {
    const children = cluster.getAllChildMarkers();

    // Worst (highest) AQI determines the cluster colour
    let worstAqi   = -1;
    let worstColor = "#aaaaaa";
    for (const m of children) {
      const s = m._station;
      if (s && s.aqi != null && s.aqi > worstAqi) {
        worstAqi   = s.aqi;
        worstColor = s.color;
      }
    }

    const count  = children.length;
    const label  = worstAqi >= 0 ? worstAqi : "?";
    const size   = 36;
    return L.divIcon({
      className: "",
      html: `<div class="cluster-marker" style="
        width:${size}px;height:${size}px;
        background:${worstColor};">
        <span class="cluster-aqi">${label}</span>
        <span class="cluster-count">${count}</span>
      </div>`,
      iconSize:   [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  },
}).addTo(map);

// ── Helpers ───────────────────────────────────────────────────────────────────
function timeAgo(isoStr) {
  if (!isoStr) return "–";
  const diff = Math.round((Date.now() - new Date(isoStr).getTime()) / 60000);
  if (diff < 60)  return `${diff} min`;
  if (diff < 1440) return `${Math.round(diff / 60)} h`;
  return `${Math.round(diff / 1440)} d`;
}

// ── Markers ───────────────────────────────────────────────────────────────────
function makeIcon(station, zoom) {
  const color  = station.color || "#aaaaaa";
  const aqi    = station.aqi;
  const small  = zoom < 10;
  const size   = small ? 16 : 26;
  const noData = aqi == null;
  const stale  = station.stale === true;
  const classes = ["aqi-marker", noData ? "no-data" : "", stale ? "stale" : ""]
    .filter(Boolean).join(" ");
  return L.divIcon({
    className: "",
    html: `<div class="${classes}" style="
      width:${size}px;height:${size}px;background:${color};
      font-size:${small ? "0" : "0.66rem"}">${small ? "" : (aqi ?? "?")}</div>`,
    iconSize: [size, size],
    iconAnchor: [size/2, size/2],
  });
}

function renderMarkers() {
  markerLayer.clearLayers();
  const zoom = map.getZoom();
  allStations.forEach(s => {
    const m = L.marker([s.lat, s.lon], { icon: makeIcon(s, zoom), title: s.name });
    m._station = s;   // used by iconCreateFunction for cluster colour
    m.on("click", e => { L.DomEvent.stopPropagation(e); openPanel(s); });
    markerLayer.addLayer(m);
  });
}

// Redraw individual marker icons on zoom so AQI value appears when zoomed in.
map.on("zoomend", renderMarkers);

// Close panel when clicking on the map (outside panel)
map.on("click", () => closePanel());

// ── Panel ─────────────────────────────────────────────────────────────────────
function openPanel(station) {
  const panel   = document.getElementById("panel");
  const content = document.getElementById("panel-content");
  panel.classList.remove("hidden");

  const color = station.color || "#aaaaaa";
  const aqi   = station.aqi ?? "–";

  // Build reading buttons (each is a clickable button that switches the chart)
  let readingsHtml = "";
  (station.readings || []).forEach(r => {
    const safeParam = (r.type  || "").replace(/"/g, "&quot;");
    const safeUnit  = (r.unit  || "").replace(/"/g, "&quot;");
    readingsHtml += `
      <button class="p-reading" data-param="${safeParam}" data-unit="${safeUnit}">
        <div class="p-reading-label">${r.type}</div>
        <div class="p-reading-value">${r.value} <small style="font-size:0.6rem;color:var(--subtext)">${r.unit||""}</small></div>
      </button>`;
  });
  if (!readingsHtml)
    readingsHtml = `<div class="p-reading" style="grid-column:1/-1">
      <div class="p-reading-label">${t("noData")}</div></div>`;

  // Localize EAQI level label using the numeric level (1-6) → eaqiLevels array
  const eaqiLevels = t("eaqiLevels");
  const levelLabel = (station.aqi >= 1 && station.aqi <= 6)
    ? eaqiLevels[station.aqi - 1]
    : t("noData");

  const aqiSrc = station.aqi_source
    ? `<br><small style="font-size:0.65rem">${station.aqi_source}</small>` : "";
  const staleBanner = station.stale
    ? `<div class="p-stale-banner">
         ⏱ ${t("staleNotice")} &nbsp;·&nbsp; ${t("lastSeen")}: ${timeAgo(station.last_seen)}
       </div>` : "";

  content.innerHTML = `
    <div class="p-source">${t("source")}: ${station.source}</div>
    <div class="p-name">${station.name}</div>
    <div class="p-aqi-badge" style="background:${color}">
      <span class="p-aqi-value">${aqi}</span>
      <span>${levelLabel}</span>
    </div>${aqiSrc}
    ${staleBanner}
    <div class="p-readings">${readingsHtml}</div>
    <div class="p-meta">
      <strong>${t("sensorType")}:</strong> ${station.sensor_type || "–"}<br>
      <strong>${t("vendor")}:</strong> ${station.vendor || "–"}
    </div>
    <div class="p-chart-title">–</div>
    <div class="p-chart-wrap" id="chart-wrap">
      <canvas id="sparkline"></canvas>
    </div>
    <div id="chart-status" class="p-no-history" style="display:none"></div>
    ${buildEaqiTable()}
  `;

  // Wire reading buttons → chart
  content.querySelectorAll("button.p-reading").forEach(btn => {
    btn.addEventListener("click", () => {
      content.querySelectorAll("button.p-reading").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const param = btn.dataset.param;
      const unit  = btn.dataset.unit;
      const titleEl = content.querySelector(".p-chart-title");
      if (titleEl) titleEl.textContent = `${param} – ${t("history")}`;
      loadHistory(station, param, unit);
    });
  });

  // Auto-select best starting param: prefer PM2.5, else first button
  const firstBtn =
    content.querySelector('button.p-reading[data-param="PM2.5"]') ||
    content.querySelector('button.p-reading[data-param^="PM2.5"]') ||
    content.querySelector("button.p-reading");
  if (firstBtn) {
    firstBtn.click();
  } else {
    loadHistory(station, "PM2.5", "µg/m³");
  }
}

function closePanel() {
  document.getElementById("panel").classList.add("hidden");
  if (activeChart) { activeChart.destroy(); activeChart = null; }
}

// ── History / sparkline ───────────────────────────────────────────────────────
async function loadHistory(station, param, unit) {
  param = param || "PM2.5";
  unit  = unit  || "µg/m³";

  if (activeChart) { activeChart.destroy(); activeChart = null; }

  // Reset chart area to visible in case a previous load hid it
  const wrap   = document.getElementById("chart-wrap");
  const status = document.getElementById("chart-status");
  const canvas = document.getElementById("sparkline");
  if (wrap)   { wrap.style.display = ""; }
  if (status) { status.style.display = "none"; status.textContent = ""; }

  let points  = [];
  let isModel = false;

  try {
    const res  = await fetch(`/api/history/${encodeURIComponent(station.id)}?param=${encodeURIComponent(param)}`);
    const data = await res.json();

    if (data.has_data) {
      points = data.points;
    } else if (param === "PM2.5") {
      // CAMS model fallback only for PM2.5 (the only param Open-Meteo exposes)
      isModel = true;
      const r = await fetch(
        `https://air-quality-api.open-meteo.com/v1/air-quality` +
        `?latitude=${station.lat}&longitude=${station.lon}` +
        `&hourly=pm2_5&past_days=1&forecast_days=0&timezone=Europe%2FZagreb`
      );
      const d = await r.json();
      const times = d.hourly?.time || [];
      const pm25  = d.hourly?.pm2_5 || [];
      points = times.map((ts, i) => ({ t: ts + ":00Z", v: pm25[i] ?? null }));
    }
  } catch (e) {
    console.error("History fetch failed", e);
  }

  // Update chart title (button click already set it, but refresh model note here)
  const titleEl = document.querySelector(".p-chart-title");
  if (titleEl) {
    titleEl.innerHTML = `${param} – ${t("history")}` +
      (isModel ? ` <small style="color:var(--subtext);font-size:0.64rem">${t("histModel")}</small>` : "");
  }

  if (!points || points.every(p => p.v === null)) {
    if (wrap)   wrap.style.display = "none";
    if (status) { status.style.display = ""; status.textContent = t("noHistory"); }
    return;
  }

  const color   = station.color || "#4c6ef5";
  const isDark  = document.documentElement.getAttribute("data-theme") === "dark";
  const gridCol = isDark ? "#222" : "#e8eaf0";
  const tickCol = isDark ? "#666" : "#999";

  const labels = points.map(p => {
    const d = new Date(p.t);
    return d.toLocaleTimeString("sl-SI", { hour: "2-digit", minute: "2-digit" });
  });

  if (activeChart) activeChart.destroy();
  activeChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data:            points.map(p => p.v),
        borderColor:     color,
        backgroundColor: color + "28",
        borderWidth:     2,
        pointRadius:     0,
        fill:            true,
        tension:         0.3,
        spanGaps:        false,   // gaps for missing data
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ctx.parsed.y !== null
              ? `${ctx.parsed.y}${unit ? " " + unit : ""}`
              : t("noData"),
          },
        },
      },
      scales: {
        x: {
          ticks: { color: tickCol, font: { size: 9 }, maxTicksLimit: 6, maxRotation: 0 },
          grid:  { color: gridCol },
        },
        y: {
          ticks: { color: tickCol, font: { size: 9 } },
          grid:  { color: gridCol },
          title: { display: !!unit, text: unit, color: tickCol, font: { size: 8 } },
        },
      },
    },
  });
}

// ── Status bar ────────────────────────────────────────────────────────────────
function updateStatusBar() {
  if (!collectedAt) return;
  const diffMin = Math.round((Date.now() - new Date(collectedAt).getTime()) / 60000);
  const el = document.getElementById("statusbar");
  if (el)
    el.innerHTML = `<span>${allStations.length}</span> ${t("stationsCount")} · ${t("lastUpdated")}: <span>${diffMin} ${t("ago")}</span>`;
}
setInterval(updateStatusBar, 30000);

// ── Load stations ─────────────────────────────────────────────────────────────
async function loadStations() {
  try {
    const res  = await fetch("/api/stations");
    const data = await res.json();
    allStations  = data.stations || [];
    collectedAt  = data.collected_at;
    renderMarkers();
    updateStatusBar();
  } catch (e) {
    console.error("Stations fetch failed", e);
  } finally {
    document.getElementById("loading").classList.add("done");
  }
}

// ── Toggles ───────────────────────────────────────────────────────────────────
function toggleTheme() {
  theme = theme === "light" ? "dark" : "light";
  localStorage.setItem("airq_theme", theme);
  applyTheme();
  // Rebuild chart if open (colors need to update)
  const panel = document.getElementById("panel");
  if (!panel.classList.contains("hidden")) {
    const s = allStations.find(st =>
      document.querySelector(".p-name")?.textContent === st.name);
    if (s) {
      const activeBtn = document.querySelector("button.p-reading.active");
      const param = activeBtn?.dataset.param || "PM2.5";
      const unit  = activeBtn?.dataset.unit  || "µg/m³";
      loadHistory(s, param, unit);
    }
  }
}

function toggleLang() {
  lang = lang === "sl" ? "en" : "sl";
  localStorage.setItem("airq_lang", lang);
  applyLang();
  updateStatusBar();
}

// ── CAMS model layer ──────────────────────────────────────────────────────────

// Custom canvas layer that clips CAMS grid cells to Slovenia's boundary
const CAMSLayer = L.Layer.extend({
  initialize(data) { this._data = data; },

  onAdd(map) {
    this._map = map;
    this._canvas = document.createElement("canvas");
    Object.assign(this._canvas.style, {
      position: "absolute", top: "0", left: "0",
      pointerEvents: "none",
    });
    // Insert below markers but above tiles (overlayPane z-index ~400)
    map.getPane("overlayPane").appendChild(this._canvas);
    map.on("moveend zoomend resize", this._draw, this);
    this._draw();
  },

  onRemove(map) {
    this._canvas.remove();
    map.off("moveend zoomend resize", this._draw, this);
  },

  _draw() {
    const map    = this._map;
    const size   = map.getSize();
    const canvas = this._canvas;
    canvas.width  = size.x;
    canvas.height = size.y;
    canvas.style.width  = size.x + "px";
    canvas.style.height = size.y + "px";

    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, size.x, size.y);

    // ── Clip to Slovenia polygon ──────────────────────────────────────────
    if (sloveniaGeoJSON) {
      const geom = sloveniaGeoJSON.features[0].geometry;
      const rings = geom.type === "MultiPolygon"
        ? geom.coordinates.flat(1)
        : geom.coordinates;
      ctx.beginPath();
      for (const ring of rings) {
        ring.forEach(([lng, lat], i) => {
          const p = map.latLngToContainerPoint([lat, lng]);
          i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y);
        });
        ctx.closePath();
      }
      ctx.clip();
    }

    // ── Draw grid cells ───────────────────────────────────────────────────
    const half = this._data.cell_deg / 2;
    ctx.globalAlpha = 0.55;
    for (const pt of this._data.points) {
      const sw = map.latLngToContainerPoint([pt.lat - half, pt.lon - half]);
      const ne = map.latLngToContainerPoint([pt.lat + half, pt.lon + half]);
      ctx.fillStyle = (pt.level !== null && pt.color) ? pt.color : "#cccccc";
      ctx.fillRect(ne.x, ne.y, sw.x - ne.x, sw.y - ne.y);
    }
    ctx.globalAlpha = 1;

    // ── Slovenia border outline ───────────────────────────────────────────
    if (sloveniaGeoJSON) {
      const geom = sloveniaGeoJSON.features[0].geometry;
      const rings = geom.type === "MultiPolygon"
        ? geom.coordinates.flat(1)
        : geom.coordinates;
      ctx.beginPath();
      for (const ring of rings) {
        ring.forEach(([lng, lat], i) => {
          const p = map.latLngToContainerPoint([lat, lng]);
          i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y);
        });
        ctx.closePath();
      }
      ctx.strokeStyle = "rgba(0,0,0,0.5)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  },
});

async function toggleCams() {
  const btn = document.getElementById("btn-cams");
  if (camsVisible) {
    if (camsLayer) { camsLayer.remove(); camsLayer = null; }
    camsVisible = false;
    btn.classList.remove("active");
    // Remove legend note
    const note = document.getElementById("cams-note");
    if (note) note.remove();
    return;
  }

  // Show loading state
  btn.disabled = true;
  btn.textContent = t("camsLoading");

  try {
    // Load Slovenia boundary if not yet cached
    if (!sloveniaGeoJSON) {
      const r = await fetch("/static/slovenia.geojson");
      sloveniaGeoJSON = await r.json();
    }
    // Fetch CAMS grid
    const r    = await fetch("/api/cams");
    const data = await r.json();

    camsLayer   = new CAMSLayer(data).addTo(map);
    camsVisible = true;
    btn.classList.add("active");

    // Small note below statusbar
    let note = document.getElementById("cams-note");
    if (!note) {
      note = document.createElement("div");
      note.id = "cams-note";
      document.getElementById("map-wrap").appendChild(note);
    }
    note.textContent = t("camsLabel");
  } catch (e) {
    console.error("CAMS load failed", e);
  } finally {
    btn.disabled = false;
    btn.textContent = t("camsBtn");
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
applyTheme();
applyLang();
loadStations();

// Invalidate map size when header height changes (e.g. legend wraps on mobile)
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => map.invalidateSize()).observe(
    document.getElementById("header")
  );
}

// Safety: force Leaflet to re-measure after page paint settles (needed on iOS)
window.addEventListener("load", () => {
  setTimeout(() => map.invalidateSize(), 50);
  setTimeout(() => map.invalidateSize(), 400);
});
