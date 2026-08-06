// 04-map-zoom.js — COP zoom & pan (viewBox drive, wheel/drag/dblclick, zoom buttons)

// --- COP zoom & pan ---------------------------------------------------------
// Drive the map's viewBox so the user can zoom into a hot region (Middle East,
// East Asia) to read a dense cluster, then reset to the world. Everything is
// drawn in this 720x278 space, so zooming the viewBox scales pins, land and
// labels together — no per-layer maths. getScreenCTM handles the
// letterboxing from preserveAspectRatio, so cursor-anchored zoom stays exact.
const MAP_W = 720, MAP_H = 278, MAP_MIN_W = 60;   // MIN_W => ~12x max zoom
const mapVB = { x: 0, y: 0, w: MAP_W, h: MAP_H };
const zoomInfoEl = document.getElementById("mapZoomInfo");
const zoomResetBtn = document.getElementById("mapZoomReset");
let zoomInfoTimer = null;
// Current zoom factor (1 = world view, up to MAP_W/MAP_MIN_W ~= 12 at max
// zoom) — read by markerAnchor/applyMarkerScale (03-map-core.js) to keep
// pins/labels/pulses a constant on-screen size regardless of viewBox zoom.
let currentMapZoom = 1;
let _lastScaledZoomW = MAP_W;   // skip refreshMarkerScale on pure pans (w unchanged)

function mapZoomedIn() { return mapVB.w < MAP_W - 0.5; }

function applyMapView() {
  svg.setAttribute("viewBox",
    `${mapVB.x.toFixed(2)} ${mapVB.y.toFixed(2)} ${mapVB.w.toFixed(2)} ${mapVB.h.toFixed(2)}`);
  currentMapZoom = MAP_W / mapVB.w;
  if (mapVB.w !== _lastScaledZoomW) {
    _lastScaledZoomW = mapVB.w;
    refreshMarkerScale();
  }
  const zoomed = mapZoomedIn();
  svg.classList.toggle("zoomed", zoomed);
  zoomResetBtn.classList.toggle("hidden", !zoomed);
  if (zoomed) {
    zoomInfoEl.textContent = `${(MAP_W / mapVB.w).toFixed(1)}× · drag to pan`;
    zoomInfoEl.classList.add("on");
    clearTimeout(zoomInfoTimer);
    zoomInfoTimer = setTimeout(() => zoomInfoEl.classList.remove("on"), 1600);
  } else {
    zoomInfoEl.classList.remove("on");
  }
}

function clampMapView() {
  mapVB.w = Math.max(MAP_MIN_W, Math.min(MAP_W, mapVB.w));
  mapVB.h = mapVB.w * (MAP_H / MAP_W);           // lock aspect — no distortion
  mapVB.x = Math.max(0, Math.min(MAP_W - mapVB.w, mapVB.x));
  mapVB.y = Math.max(0, Math.min(MAP_H - mapVB.h, mapVB.y));
}

function clientToUser(clientX, clientY) {
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
  return { x: p.x, y: p.y };
}

// Zoom by `factor` keeping the user-space point (ux,uy) under the cursor.
function zoomMapAt(factor, ux, uy) {
  const rx = (ux - mapVB.x) / mapVB.w;
  const ry = (uy - mapVB.y) / mapVB.h;
  mapVB.w = Math.max(MAP_MIN_W, Math.min(MAP_W, mapVB.w / factor));
  mapVB.h = mapVB.w * (MAP_H / MAP_W);
  mapVB.x = ux - rx * mapVB.w;
  mapVB.y = uy - ry * mapVB.h;
  clampMapView();
  applyMapView();
}

svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  const u = clientToUser(e.clientX, e.clientY);
  if (!u) return;
  // Trackpad pinch arrives as ctrl+wheel; treat both the same.
  zoomMapAt(e.deltaY < 0 ? 1.18 : 1 / 1.18, u.x, u.y);
}, { passive: false });

// Drag to pan. We only start moving the map after a few pixels of travel, so a
// plain click still falls through to a pin's own handler; once it's a real
// drag we swallow the trailing click so panning never selects a dot.
let panActive = false, panMoved = false, panLast = null, swallowClick = false;

svg.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !mapZoomedIn()) return;
  panActive = true; panMoved = false;
  panLast = clientToUser(e.clientX, e.clientY);
});
svg.addEventListener("pointermove", (e) => {
  if (!panActive || !panLast) return;
  const u = clientToUser(e.clientX, e.clientY);
  if (!u) return;
  const dx = u.x - panLast.x, dy = u.y - panLast.y;
  if (!panMoved && Math.hypot(dx, dy) < (mapVB.w / MAP_W) * 4) return;
  panMoved = true;
  svg.classList.add("grabbing");
  mapVB.x -= dx; mapVB.y -= dy;
  clampMapView();
  applyMapView();
  panLast = clientToUser(e.clientX, e.clientY);   // re-anchor in the new frame
});
function endPan() {
  if (!panActive) return;
  panActive = false;
  svg.classList.remove("grabbing");
  if (panMoved) swallowClick = true;
}
svg.addEventListener("pointerup", endPan);
svg.addEventListener("pointerleave", endPan);
// Capture phase: runs before any pin's bubble handler, so a pan never selects.
svg.addEventListener("click", (e) => {
  if (swallowClick) { swallowClick = false; e.stopPropagation(); e.preventDefault(); }
}, true);

// Double-click to zoom in toward the cursor; a quick way in on the wall.
svg.addEventListener("dblclick", (e) => {
  const u = clientToUser(e.clientX, e.clientY);
  if (u) zoomMapAt(2, u.x, u.y);
});

document.getElementById("mapZoomIn").addEventListener("click",
  () => zoomMapAt(1.6, mapVB.x + mapVB.w / 2, mapVB.y + mapVB.h / 2));
document.getElementById("mapZoomOut").addEventListener("click",
  () => zoomMapAt(1 / 1.6, mapVB.x + mapVB.w / 2, mapVB.y + mapVB.h / 2));
zoomResetBtn.addEventListener("click", () => {
  mapVB.x = 0; mapVB.y = 0; mapVB.w = MAP_W; mapVB.h = MAP_H;
  applyMapView();
});

// Zoom straight to a lon/lat point (e.g. a clicked breaking alert), centered,
// at a fixed regional width so the surrounding context is always legible.
function zoomToPoint(lon, lat, targetW) {
  const x = lonToX(lon), y = latToY(lat);
  mapVB.w = Math.max(MAP_MIN_W, Math.min(MAP_W, targetW || 150));
  mapVB.h = mapVB.w * (MAP_H / MAP_W);
  mapVB.x = x - mapVB.w / 2;
  mapVB.y = y - mapVB.h / 2;
  clampMapView();
  applyMapView();
}

// Quick region jump — a fast way to a theater without scroll/drag. Boxes are
// deliberately generous (a "which part of the map am I looking at" jump, not
// a precise crop); MAP_MIN_W/aspect-lock still apply via clampMapView.
const MAP_REGIONS = {
  americas: { lonMin: -130, lonMax: -30, latMin: -55, latMax: 60 },
  europe: { lonMin: -12, lonMax: 45, latMin: 35, latMax: 65 },
  middle_east: { lonMin: 25, lonMax: 63, latMin: 12, latMax: 42 },
  east_asia: { lonMin: 95, lonMax: 148, latMin: 15, latMax: 50 },
};

function zoomToRegion(key) {
  const r = MAP_REGIONS[key];
  if (!r) {
    mapVB.x = 0; mapVB.y = 0; mapVB.w = MAP_W; mapVB.h = MAP_H;
    applyMapView();
    return;
  }
  const x0 = lonToX(r.lonMin), x1 = lonToX(r.lonMax);
  const y0 = latToY(r.latMax), y1 = latToY(r.latMin);   // latMax -> smaller Y (top)
  mapVB.w = Math.max(MAP_MIN_W, Math.min(MAP_W, x1 - x0));
  mapVB.h = mapVB.w * (MAP_H / MAP_W);
  mapVB.x = (x0 + x1) / 2 - mapVB.w / 2;
  mapVB.y = (y0 + y1) / 2 - mapVB.h / 2;
  clampMapView();
  applyMapView();
}

const mapRegionSelect = document.getElementById("mapRegionSelect");
if (mapRegionSelect) {
  mapRegionSelect.addEventListener("change", () => {
    zoomToRegion(mapRegionSelect.value);
    mapRegionSelect.value = "";   // a one-shot jump, not a persistent filter
  });
}
