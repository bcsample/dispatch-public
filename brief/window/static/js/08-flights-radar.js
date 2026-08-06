// 08-flights-radar.js — local radar map + Skies departure board

// Local radar map: aircraft plotted around home (equirectangular over the
// configured bounding box, cos-lat corrected). Triangles point along heading.
// Vertical-trend class shared by the radar and the board: climbing (departing),
// descending (arriving), level (overflying), or on the ground.
function climbClass(a) {
  if (a.on_ground) return "ground";
  if (a.climb != null && a.climb > 300) return "up";
  if (a.climb != null && a.climb < -300) return "down";
  return "level";
}
function climbLabel(a) {
  const c = climbClass(a);
  return c === "ground" ? "on the ground"
    : c === "up" ? "departing (climbing)"
    : c === "down" ? "arriving (descending)" : "level / overflying";
}

// The current aircraft, so a click on the board or radar can look one up.
let lastFlights = [];
let lastFlightPayload = null;     // cached so a click can re-sort without a poll
let selectedFlightCs = null;      // the clicked jet — pinned to the top of the board

// Clicked-flight detail: who it is, where it's going, what it's doing, plus a
// live-track link. Filled by clicks on a board row or a radar aircraft.
function setFlightDetail(a) {
  const el = document.getElementById("flightDetail");
  if (!el || !a) return;
  const route = (a.from && a.to) ? `${a.from} → ${a.to}`
    : (a.from || a.to || "route unknown");
  const alt = a.on_ground ? "on the ground"
    : (a.altitude != null ? a.altitude.toLocaleString() + " ft" : "altitude —");
  const bits = [alt, (a.speed != null ? a.speed + " mph" : null),
    (a.heading != null ? a.heading + "°" : null), climbLabel(a)].filter(Boolean);
  el.innerHTML = `<b>${escapeHtml(a.callsign)}</b>`
    + (a.airline ? ` <span class="muted">· ${escapeHtml(a.airline)}</span>` : "")
    + ` <span class="muted">— ${escapeHtml(route)}</span><br>`
    + `<span class="muted">${escapeHtml(bits.join(" · "))}</span>`
    + `<a href="https://www.flightaware.com/live/flight/${encodeURIComponent(a.callsign)}" target="_blank" rel="noopener">track ↗</a>`;
  // Highlight the matching board row.
  document.querySelectorAll("#flightBoard .flightrow.sel").forEach(r => r.classList.remove("sel"));
  const row = document.querySelector(`#flightBoard .flightrow[data-cs="${CSS.escape(a.callsign)}"]`);
  if (row) row.classList.add("sel");
}

function bindFlightClicks() {
  const handler = e => {
    const el = e.target.closest("[data-cs]");
    if (!el) return;
    const cs = el.getAttribute("data-cs");
    const a = lastFlights.find(f => f.callsign === cs);
    if (!a) return;
    selectedFlightCs = cs;                  // pin it to the top…
    setFlightDetail(a);
    if (lastFlightPayload) renderFlights(lastFlightPayload);  // …and re-sort now
  };
  const board = document.getElementById("flightBoard");
  const radar = document.getElementById("radarSvg");
  if (board) board.addEventListener("click", handler);
  if (radar) radar.addEventListener("click", handler);
}
bindFlightClicks();

// Ground reference points for the local radar (DC / DMV area). Airports anchor
// the arriving/departing clusters; cities give orientation. Any that fall
// outside the current box are simply skipped, so moving the box degrades
// gracefully (they just stop drawing).
// DC/VA/MD state borders for the radar (static/radar_boundaries.json, made by
// scripts/generate_radar_boundaries.py). Loaded once; the Potomac shows up as
// the VA/DC/MD border along the river.
let radarBoundaries = null;
async function loadRadarBoundaries() {
  try {
    const r = await fetch("/static/radar_boundaries.json");
    if (r.ok) radarBoundaries = (await r.json()).lines || null;
  } catch (e) { /* decorative — radar still works without borders */ }
}
loadRadarBoundaries();

const RADAR_LANDMARKS = [
  { n: "DCA", lat: 38.851, lon: -77.038, k: "air" },
  { n: "IAD", lat: 38.944, lon: -77.456, k: "air" },
  { n: "BWI", lat: 39.176, lon: -76.668, k: "air" },
  { n: "Washington", lat: 38.895, lon: -77.036, k: "city" },
  { n: "Baltimore", lat: 39.290, lon: -76.612, k: "city" },
  { n: "Manassas", lat: 38.751, lon: -77.475, k: "city" },
  { n: "Annapolis", lat: 38.978, lon: -76.492, k: "city" },
];

function renderRadar(center, radiusNm, aircraft) {
  const svg = document.getElementById("radarSvg");
  if (!svg || !center) return;
  const [cLat, cLon] = center, R = 120, CX = 120, CY = 120;
  const halfDeg = (radiusNm || 30) / 60;              // ~1° lat = 60nm
  const coslat = Math.cos(cLat * Math.PI / 180) || 1;
  const px = (lat, lon) => [
    CX + ((lon - cLon) * coslat / halfDeg) * R,
    CY - ((lat - cLat) / halfDeg) * R,
  ];
  const parts = [];
  // Range rings (full + half radius) and N-S/E-W crosshair.
  parts.push(`<circle class="radar-ring" cx="${CX}" cy="${CY}" r="${R}"/>`);
  parts.push(`<circle class="radar-ring" cx="${CX}" cy="${CY}" r="${R / 2}"/>`);
  parts.push(`<line class="radar-cross" x1="${CX}" y1="8" x2="${CX}" y2="${2 * R - 8}"/>`);
  parts.push(`<line class="radar-cross" x1="8" y1="${CY}" x2="${2 * R - 8}" y2="${CY}"/>`);
  parts.push(`<text class="radar-scale" x="${CX + 4}" y="14">N</text>`);
  parts.push(`<text class="radar-scale" x="${CX + R - 22}" y="${CY - 4}">${Math.round(radiusNm)}nm</text>`);
  // State borders (DC/VA/MD) under everything — the Potomac is the VA/DC/MD line.
  for (const line of (radarBoundaries || [])) {
    const pts = line.map(([lat, lon]) => {
      const [x, y] = px(lat, lon);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    parts.push(`<polyline class="radar-border" points="${pts}"/>`);
  }
  // Ground reference: airports as diamonds (3-letter code), cities as dots.
  for (const L of RADAR_LANDMARKS) {
    const [x, y] = px(L.lat, L.lon);
    if (x < 6 || x > 2 * R - 6 || y < 6 || y > 2 * R - 6) continue;
    const xf = x.toFixed(1), yf = y.toFixed(1);
    if (L.k === "air") {
      parts.push(`<rect class="radar-airport" x="${(x - 3).toFixed(1)}" y="${(y - 3).toFixed(1)}" width="6" height="6" transform="rotate(45 ${xf} ${yf})"/>`);
      parts.push(`<text class="radar-place air" x="${(x + 5).toFixed(1)}" y="${(y + 3).toFixed(1)}">${L.n}</text>`);
    } else {
      parts.push(`<circle class="radar-city" cx="${xf}" cy="${yf}" r="1.6"/>`);
      parts.push(`<text class="radar-place" x="${(x + 4).toFixed(1)}" y="${(y + 3).toFixed(1)}">${L.n}</text>`);
    }
  }
  for (const a of (aircraft || [])) {
    if (a.lat == null || a.lon == null) continue;
    const [x, y] = px(a.lat, a.lon);
    if (x < 0 || x > 2 * R || y < 0 || y > 2 * R) continue;
    const hdg = a.heading || 0;
    const cls = "radar-ac " + climbClass(a);
    const cs = escapeAttr(a.callsign);
    const tip = `${escapeAttr(a.callsign)} · ${a.on_ground ? "ground" : (a.altitude || "?") + "ft"}`;
    // A little triangle pointing "up" (north) then rotated to the track…
    parts.push(`<path class="${cls}" transform="translate(${x.toFixed(1)},${y.toFixed(1)}) rotate(${hdg})"`
      + ` d="M0,-6 L4,5 L-4,5 Z"></path>`);
    // …plus a big INVISIBLE hit target so a tiny moving triangle is easy to
    // click (the triangle itself is only a few px — this is what you actually
    // click/hover). data-cs drives the shared flight-click delegation.
    parts.push(`<circle class="radar-hit" data-cs="${cs}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="10"><title>${tip}</title></circle>`);
  }
  parts.push(`<circle class="radar-home" cx="${CX}" cy="${CY}" r="2.5"/>`);
  svg.innerHTML = parts.join("");
}

// "Skies" departure board — live aircraft near the configured point
// (/api/flights, adsb.fi). Hidden entirely when no location is configured.
function renderFlights(payload) {
  const panel = document.getElementById("skiesPanel");
  const board = document.getElementById("flightBoard");
  const count = document.getElementById("skiesCount");
  if (!panel || !board) return;
  const aircraft = (payload && payload.aircraft) || [];
  lastFlights = aircraft;
  lastFlightPayload = payload;
  renderRadar(payload && payload.center, payload && payload.radius_nm, aircraft);
  if (!aircraft || !aircraft.length) {
    // Keep the panel hidden until the first non-empty board arrives, so a
    // location-less deploy shows nothing rather than an empty shell.
    if (!board.dataset.seen) { panel.style.display = "none"; return; }
    board.innerHTML = '<div class="empty">No aircraft in range right now.</div>';
    count.textContent = "0";
    return;
  }
  board.dataset.seen = "1";
  panel.style.display = "";
  count.textContent = aircraft.length + " tracked";
  const airborne = aircraft.filter(a => !a.on_ground).length;
  count.textContent = `${airborne} airborne · ${aircraft.length} tracked`;
  const head = '<div class="flightrow head"><div>Flight</div><div>Route</div>'
    + '<div>Alt</div><div class="num">MPH</div><div class="num">Climb</div></div>';
  const climbCell = c => {
    if (c === null || c === undefined || Math.abs(c) < 100) return '<span class="climb level">— level</span>';
    return c > 0 ? `<span class="climb up">▲ ${c}</span>` : `<span class="climb down">▼ ${Math.abs(c)}</span>`;
  };
  const routeCell = a => {
    if (a.from && a.to) return `<span class="route">${escapeHtml(a.from)}<span class="arr">→</span>${escapeHtml(a.to)}</span>`;
    if (a.from || a.to) return `<span class="route">${escapeHtml(a.from || a.to)}</span>`;
    return '<span class="route none">—</span>';
  };
  // Clicked jet floats to the top so all its data is visible without scrolling.
  let ordered = aircraft;
  if (selectedFlightCs) {
    const i = aircraft.findIndex(a => a.callsign === selectedFlightCs);
    if (i > 0) ordered = [aircraft[i], ...aircraft.slice(0, i), ...aircraft.slice(i + 1)];
  }
  board.innerHTML = head + ordered.map(a => {
    const alt = a.on_ground ? "GND" : (a.altitude != null ? a.altitude.toLocaleString() + "′" : "—");
    const airline = a.airline ? `<span class="airline" title="${escapeAttr(a.airline)}">${escapeHtml(a.airline)}</span>` : "";
    const sel = a.callsign === selectedFlightCs ? " sel" : "";
    return `<div class="flightrow${a.on_ground ? " ground" : ""}${sel}" data-cs="${escapeAttr(a.callsign)}">
      <div class="fcell"><span class="cs ${climbClass(a)}">${escapeHtml(a.callsign)}</span>${airline}</div>
      <div>${routeCell(a)}</div>
      <div>${alt}</div>
      <div class="num">${a.speed ?? "—"}</div>
      <div class="num">${climbCell(a.climb)}</div>
    </div>`;
  }).join("");
}
