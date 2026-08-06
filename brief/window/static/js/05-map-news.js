// 05-map-news.js — news pin rendering/importance, map detail + legend, geometry-to-path helpers, dotmap/land loading, quake pins + delta list, escapeHtml/escapeAttr/fmtNum

function setNewsDetail(h) {
  const el = document.getElementById("mapDetail");
  if (!el) return;
  el.innerHTML = `<a href="${escapeAttr(h.url)}" target="_blank" rel="noopener"><b>${escapeHtml(h.title)}</b></a>`
    + ` <span class="muted">— ${escapeHtml(h.place || "")} · ${escapeHtml(h.source_name)}</span>`;
}

// v4.2 stage 1: geolocated news stories as cyan pins, distinct from quake pins.
// Coords come from /api/news (server-side gazetteer match, off the live path);
// click/hover shows the headline + a link to the story.
// How much a headline "matters" for the map — so we show the signal and drop
// the noise ("a lot of stuff that doesn't matter"). Uses the local-model beat
// score (curation), multi-source clustering, and freshness.
function newsImportance(h) {
  let s = 0;
  if (h.beat) s += 6;
  else if (h.beat_score != null) s += h.beat_score * 0.6;
  s += Math.min((h.dupe_count || 1) - 1, 5) * 1.5;   // outlets on the story
  if (isFreshNews(h)) s += 3;
  return s;
}
// Junk = sports/entertainment (server stoplist), OR the model curated it and
// scored it low with nobody else carrying it and it isn't fresh. The stoplist
// works even when curation skipped this cycle.
function isNewsNoise(h) {
  if (h.noise) return true;
  return h.beat_score != null && h.beat_score < 4
    && (h.dupe_count || 1) <= 1 && !isFreshNews(h);
}

const NEWS_PIN_CAP = 60, NEWS_LABEL_CAP = 8;

function renderNewsPins(headlines) {
  while (newsPinLayer.firstChild) newsPinLayer.removeChild(newsPinLayer.firstChild);
  while (newsLabelLayer.firstChild) newsLabelLayer.removeChild(newsLabelLayer.firstChild);
  while (heatLayer.firstChild) heatLayer.removeChild(heatLayer.firstChild);

  const located = (headlines || []).filter(h =>
    h.lat != null && h.lon != null && !isNewsNoise(h));
  // Most-important first, so the label budget goes to what matters.
  located.sort((a, b) => newsImportance(b) - newsImportance(a));

  const placed = [];  // laid-down label boxes for simple overlap avoidance
  located.slice(0, NEWS_PIN_CAP).forEach((h, i) => {
    const x = lonToX(h.lon), y = latToY(h.lat);
    const heat = document.createElementNS(SVG_NS, "circle");
    heat.setAttribute("cx", x); heat.setAttribute("cy", y);
    heat.setAttribute("r", 16); heat.setAttribute("fill", "url(#heatdot)");
    const heatG = markerAnchor(x, y);
    heatG.appendChild(heat);
    heatLayer.appendChild(heatG);
    if (isFreshNews(h)) addPulse(newsPinLayer, x, y, "var(--accent)");
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", x); c.setAttribute("cy", y);
    c.setAttribute("r", h.beat ? 5 : 4);
    c.setAttribute("class", "newspin" + (h.beat ? " beatpin" : ""));
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${h.title} — ${h.place} (${h.source_name})`;
    c.appendChild(title);
    c.addEventListener("mouseenter", () => setNewsDetail(h));
    c.addEventListener("click", () => setNewsDetail(h));
    const pinG = markerAnchor(x, y);
    pinG.appendChild(c);
    newsPinLayer.appendChild(pinG);
    // Label the top-importance pins with the HEADLINE; skip a label if it would
    // stack right on top of one already placed (cheap deconfliction).
    if (h.title && placed.length < NEWS_LABEL_CAP) {
      const near = placed.some(p => Math.abs(p.y - y) < 11 && Math.abs(p.x - x) < 150);
      if (!near) {
        const t = document.createElementNS(SVG_NS, "text");
        const leftSide = x > W - 160;   // near right edge -> label to the left
        t.setAttribute("x", leftSide ? x - 6 : x + 6);
        t.setAttribute("y", y - 6);
        t.setAttribute("class", "pinlabel newslabel" + (h.beat ? " beatlabel" : ""));
        if (leftSide) t.setAttribute("text-anchor", "end");
        t.textContent = h.title.length > 34 ? h.title.slice(0, 33) + "…" : h.title;
        const labelG = markerAnchor(x, y);
        labelG.appendChild(t);
        newsLabelLayer.appendChild(labelG);
        placed.push({ x, y });
      }
    }
  });
}

function setMapDetail(d, isNew) {
  const el = document.getElementById("mapDetail");
  if (!el) return;
  if (!d) { el.innerHTML = '<span class="muted">Click or hover a dot for the event detail.</span>'; return; }
  const sev = (d.severity === null || d.severity === undefined) ? "" : ` · severity ${d.severity}`;
  const nw = isNew ? ' · <b style="color:#fff">NEW this sweep</b>' : '';
  el.innerHTML = `<b>${escapeHtml(d.title)}</b>${sev}${nw} <span class="muted">— ${escapeHtml(d.source_name)}</span>`;
}

function buildLegend() {
  const el = document.getElementById("mapLegend");
  if (!el) return;
  // News-first legend — news stories are the signal; quakes are quiet context.
  el.innerHTML =
    '<span class="lg"><span class="sw" style="background:var(--red)"></span>breaking (click it)</span>'
    + '<span class="lg"><span class="sw" style="background:var(--accent)"></span>news story</span>'
    + '<span class="lg"><span class="sw" style="background:var(--yellow)"></span>on your beat</span>'
    + '<span class="lg"><span class="sw" style="background:#56617e"></span>quake / event</span>'
    + '<span class="lg muted-note">labels: top news headlines</span>';
}
buildLegend();

// v3 — same equirectangular lonToX/latToY the pins use: a linear coordinate
// map, so each land ring is just a direct "M x0 y0 L x1 y1 ... Z" path, no
// projection library needed.
function ringToPathD(ring) {
  return ring.map((pt, i) => `${i === 0 ? "M" : "L"} ${lonToX(pt[0])} ${latToY(pt[1])}`).join(" ") + " Z";
}

function geometryToPathD(geom) {
  if (!geom) return "";
  if (geom.type === "Polygon") {
    return geom.coordinates.map(ringToPathD).join(" ");
  }
  if (geom.type === "MultiPolygon") {
    return geom.coordinates.map(poly => poly.map(ringToPathD).join(" ")).join(" ");
  }
  return "";
}

// Facelift — dotted-world basemap (Stripe/Vercel/ShadowBroker style):
// continents as a grid of dots, pre-generated offline by
// scripts/generate_dotmap.py from the SAME bundled Natural Earth data.
// Fetched ONCE on page load; returns true when it rendered.
async function loadDotMap() {
  try {
    const res = await fetch("/static/dotmap.json");
    if (!res.ok) return false;
    const dm = await res.json();
    if (!dm.dots || !dm.dots.length) return false;
    for (const [x, y] of dm.dots) {
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", x);
      c.setAttribute("cy", y);
      c.setAttribute("r", dm.r || 1.35);
      c.setAttribute("class", "landdot");
      landLayer.appendChild(c);
    }
    return true;
  } catch (e) {
    return false;
  }
}

// v3 — fetch the bundled Natural Earth land file ONCE on page load (not in
// the 10s poll loop). Fail-soft: if /static isn't mounted or the file is
// missing, the map still renders (graticule + pins), just without
// continents — this is decorative, never blocks the live data path.
async function loadLand() {
  // Facelift: dotted world first; polygon fill is the fallback basemap.
  if (await loadDotMap()) return;
  // Prefer country boundaries (coastlines + borders); fall back to plain land.
  for (const src of ["/static/ne_110m_countries.json", "/static/ne_110m_land.json"]) {
    try {
      const res = await fetch(src);
      if (!res.ok) continue;
      const geo = await res.json();
      for (const feature of (geo.features || [])) {
        const d = geometryToPathD(feature.geometry);
        if (!d) continue;
        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("d", d);
        path.setAttribute("class", "country");
        landLayer.appendChild(path);
      }
      return;  // first source that loads wins
    } catch (e) {
      // decorative; try the next source, else the board still works without it
    }
  }
}
loadLand();

// This is a NEWS monitor, not an earthquake monitor. World-delta events
// (quakes/fires) are quiet background context: small, uniform, muted, NO
// magnitude labels and NO severity colors competing with the news pins. Still
// hover/clickable for the detail line. Only a genuinely major quake (>=6.5)
// keeps a faint marker of note.
function renderPins(records, deltaKeySet) {
  while (pinLayer.firstChild) pinLayer.removeChild(pinLayer.firstChild);
  while (labelLayer.firstChild) labelLayer.removeChild(labelLayer.firstChild);
  const withCoords = records.filter(d => d.lat != null && d.lon != null);
  for (const d of withCoords) {
    const x = lonToX(d.lon), y = latToY(d.lat);
    const major = (d.severity ?? 0) >= 6.5;
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", x);
    c.setAttribute("cy", y);
    c.setAttribute("r", major ? 3 : 2);
    c.setAttribute("class", "quakepin" + (major ? " major" : ""));
    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `${d.title} (${d.source_name})`;
    c.appendChild(title);
    c.addEventListener("mouseenter", () => setMapDetail(d, false));
    c.addEventListener("click", () => setMapDetail(d, false));
    const g = markerAnchor(x, y);
    g.appendChild(c);
    pinLayer.appendChild(g);
  }
}

function renderDeltaList(deltas) {
  const el = document.getElementById("deltaList");
  const feeds = {};
  if (!deltas.length) {
    el.innerHTML = '<div class="empty">No world events yet — first sweep still seeding, or nothing has changed.</div>';
    return;
  }
  el.innerHTML = deltas.slice(0, 100).map(d => {
    const bucket = sevBucket(d.severity);
    const sevText = d.severity === null || d.severity === undefined ? "" : ` · severity ${d.severity}`;
    return `<div class="delta">
      <div class="sev-dot sev-${bucket}"></div>
      <div class="body">
        <div class="title">${escapeHtml(d.title)}<span class="kind-badge">${d.delta_kind}</span></div>
        <div class="sub">${escapeHtml(d.source_name)}${sevText} · ${fmtTime(d.first_seen)}</div>
      </div>
    </div>`;
  }).join("");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function escapeAttr(s) {
  return (s || "").replace(/"/g, "&quot;");
}

function fmtNum(n) {
  if (n === null || n === undefined) return "";
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}
