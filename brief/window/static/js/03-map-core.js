// 03-map-core.js — freshness test, SMIL pulse-ring helper, map SVG layer stack setup

// Shared "genuinely new" test — first_seen is server-stamped SQLite UTC
// ("YYYY-MM-DD HH:MM:SS"). Used by the ticker's hot state, the map pulse
// rings, and the tempo sparkline, so "breaking" means ONE thing everywhere.
const FRESH_MS = 20 * 60 * 1000;
function isFreshNews(h) {
  if (!h || !h.first_seen) return false;
  const t = Date.parse(String(h.first_seen).replace(" ", "T") + "Z");
  return !isNaN(t) && (Date.now() - t) < FRESH_MS;
}

// SMIL pulse ring — an expanding, fading circle that marks a breaking event
// on the map. SMIL (not CSS r-animation) for dependable SVG support.
function addPulse(layer, x, y, color) {
  const g = markerAnchor(x, y);
  const ring = document.createElementNS(SVG_NS, "circle");
  ring.setAttribute("cx", x);
  ring.setAttribute("cy", y);
  ring.setAttribute("r", 4);
  ring.setAttribute("class", "pulsering");
  ring.setAttribute("stroke", color);
  const grow = document.createElementNS(SVG_NS, "animate");
  grow.setAttribute("attributeName", "r");
  grow.setAttribute("from", "4"); grow.setAttribute("to", "18");
  grow.setAttribute("dur", "1.8s"); grow.setAttribute("repeatCount", "indefinite");
  const fade = document.createElementNS(SVG_NS, "animate");
  fade.setAttribute("attributeName", "opacity");
  fade.setAttribute("from", "0.9"); fade.setAttribute("to", "0");
  fade.setAttribute("dur", "1.8s"); fade.setAttribute("repeatCount", "indefinite");
  ring.appendChild(grow);
  ring.appendChild(fade);
  g.appendChild(ring);
  layer.appendChild(g);
}

// Zoom-independent markers — the COP's viewBox-zoom scales EVERYTHING drawn
// in map space together (comment above), which is right for the land/dot
// basemap (more detail as you zoom in) but wrong for pins/labels/pulses: at
// ~9x zoom a headline label was rendering 9x bigger (measured ~1100px wide,
// clipped off the panel) and pulse rings ballooned into the frame. Every
// marker/label/pulse gets created inside one of these <g> anchors instead of
// going straight into its layer; refreshMarkerScale() (04-map-zoom.js, called
// from applyMapView on every zoom change) keeps each one pinned to its
// original on-screen size by counter-scaling around its own (ux,uy) point —
// the anchor design (translate/scale/translate, not a group-wide scale) is
// what lets each marker resize in place instead of sliding toward one corner.
function markerAnchor(x, y) {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "markeranchor");
  g.dataset.ux = x;
  g.dataset.uy = y;
  applyMarkerScale(g);
  return g;
}

function applyMarkerScale(g) {
  const x = g.dataset.ux, y = g.dataset.uy;
  const s = 1 / (typeof currentMapZoom === "number" && currentMapZoom > 0 ? currentMapZoom : 1);
  g.setAttribute("transform", `translate(${x} ${y}) scale(${s}) translate(${-x} ${-y})`);
}

function refreshMarkerScale() {
  for (const layer of [heatLayer, newsPinLayer, pinLayer, labelLayer, newsLabelLayer, alertPulseLayer]) {
    for (const g of layer.children) {
      if (g.classList.contains("markeranchor")) applyMarkerScale(g);
    }
  }
}

const svg = document.getElementById("map");
buildGraticule(svg);

// v3 — land layer sits between the graticule and the pins (appended after
// the graticule, before pinLayer, so SVG's paint order puts continents over
// the grid but under the dots). Populated once by loadLand(), never touched
// by the 10s poll.
let landLayer = document.createElementNS(SVG_NS, "g");
landLayer.setAttribute("id", "landLayer");
svg.appendChild(landLayer);

let heatLayer = document.createElementNS(SVG_NS, "g");   // attention heat under the pins (radial glow per news story)
svg.appendChild(heatLayer);
let newsPinLayer = document.createElementNS(SVG_NS, "g");   // geolocated news stories (under quake pins)
svg.appendChild(newsPinLayer);
let pinLayer = document.createElementNS(SVG_NS, "g");
svg.appendChild(pinLayer);
let labelLayer = document.createElementNS(SVG_NS, "g");   // labels for the top quakes, above pins
svg.appendChild(labelLayer);
let newsLabelLayer = document.createElementNS(SVG_NS, "g");   // labels for top news pins, painted above quake labels
svg.appendChild(newsLabelLayer);
let alertPulseLayer = document.createElementNS(SVG_NS, "g");   // red rings for active /api/alerts, above everything
svg.appendChild(alertPulseLayer);
