// 01-utils.js — shared SVG/map constants, lon/lat coordinate helpers, graticule drawing, severity bucket/color
const SVG_NS = "http://www.w3.org/2000/svg";
// Cropped to the populated world — Antarctica and the empty polar oceans are
// dropped so the map isn't 1/4 wasted space. Keep in sync with the same range
// in scripts/generate_dotmap.py (the baked dot coordinates depend on it).
const W = 720, H = 278;
const LAT_TOP = 82, LAT_BOTTOM = -57;

function lonToX(lon) { return (lon + 180) / 360 * W; }
function latToY(lat) { return (LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM) * H; }

function buildGraticule(svg) {
  for (let lon = -180; lon <= 180; lon += 30) {
    const x = lonToX(lon);
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", x); line.setAttribute("y1", 0);
    line.setAttribute("x2", x); line.setAttribute("y2", H);
    line.setAttribute("class", "grat" + (lon === 0 ? " major" : ""));
    svg.appendChild(line);
  }
  for (let lat = -90; lat <= 90; lat += 30) {
    const y = latToY(lat);
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", 0); line.setAttribute("y1", y);
    line.setAttribute("x2", W); line.setAttribute("y2", y);
    line.setAttribute("class", "grat" + (lat === 0 ? " major" : ""));
    svg.appendChild(line);
  }
  const border = document.createElementNS(SVG_NS, "rect");
  border.setAttribute("x", 0); border.setAttribute("y", 0);
  border.setAttribute("width", W); border.setAttribute("height", H);
  border.setAttribute("fill", "none"); border.setAttribute("class", "landline");
  svg.appendChild(border);
}

function sevBucket(sev) {
  if (sev === null || sev === undefined) return "none";
  if (sev >= 6) return "high";
  if (sev >= 4) return "med";
  return "low";
}
const SEV_COLOR = { high: "var(--red)", med: "var(--yellow)", low: "var(--green)", none: "var(--gray)" };
