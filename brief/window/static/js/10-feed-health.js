// 10-feed-health.js — per-source freshness chips (fresh/stale/very_stale/error),
// tucked into the ticker's right edge. Distinct from the full-board STALE
// overlay (a whole-connection watchdog); this is per-individual-feed.
const FEED_BUCKET_TITLE = {
  fresh: "fresh",
  stale: "stale (15-60m old)",
  very_stale: "very stale (1-6h old)",
  error: "error / no recent data",
};

function _fmtFeedAge(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

function renderFeedHealth(feeds) {
  const el = document.getElementById("feedHealth");
  if (!el) return;
  if (!feeds || !feeds.length) { el.innerHTML = ""; return; }
  el.innerHTML = feeds.map(f => {
    const bucket = f.bucket || "error";
    const label = FEED_BUCKET_TITLE[bucket] || bucket;
    const age = _fmtFeedAge(f.age_seconds);
    return `<span class="fchip ${bucket}" title="${escapeAttr(f.name)} — ${label} (${age})">`
      + `<span class="fdot"></span>${escapeHtml(f.name)}</span>`;
  }).join("");
}
