// 07-alerts-ticker.js — breaking chyron ticker, alert map pulses, staleness watchdog state, nightly reload, fetchJSON

// Breaking chyron. Priority: high-severity world events (big quakes, sev>=6)
// first as "hot", then clustered multi-source stories (a story many outlets
// carry at once is, by definition, the breaking one), then the newest
// headlines. Only rebuilds when the item SET changes, so the CSS scroll runs
// uninterrupted across 10s polls instead of snapping back to the start.
let _tickerSig = "";
// Active alerts (dispatch-alerts-v1) own the front of the chyron and drop a
// red pulse on the map — the board and the spoken alerts share one signal.
// Plain-English label for what an alert IS, so the red marker explains itself.
const ALERT_KIND_LABEL = {
  world: "Major world event",
  news: "Breaking — many outlets",
  surge: "Coverage surge",
  watchlist: "On your watchlist",
  // The deepest signal: 3+ independent channels (world/news/surge/watchlist)
  // all hitting the same place at once -- rarer and more significant than
  // any single alert kind, so it gets its own color on the map (see
  // renderAlertPulses) rather than blending into the ordinary red pulses.
  convergence: "⬥ Convergence — multiple signals",
};
function setAlertDetail(a) {
  const el = document.getElementById("mapDetail");
  if (!el || !a) return;
  const kind = ALERT_KIND_LABEL[a.kind] || "Alert";
  const where = a.place ? ` · ${escapeHtml(a.place)}` : "";
  el.innerHTML = `<b style="color:var(--red)">⚠ ${kind}</b>${where}<br>`
    + `<span>${escapeHtml(a.title || "")}</span>`;
}

function renderAlertPulses(alerts) {
  while (alertPulseLayer.firstChild) alertPulseLayer.removeChild(alertPulseLayer.firstChild);
  for (const a of (alerts || [])) {
    if (a.lat === null || a.lat === undefined || a.lon === null || a.lon === undefined) continue;
    const x = lonToX(a.lon), y = latToY(a.lat);
    // Convergence (3+ signals, one place) gets its own color/size -- the
    // deepest, rarest signal shouldn't blend into an ordinary red pulse.
    const isConvergence = a.kind === "convergence";
    const color = isConvergence ? "#c86dff" : "var(--red)";
    addPulse(alertPulseLayer, x, y, color);
    // A solid, CLICKABLE dot at the centre of the pulse — the ring itself is
    // pointer-events:none (it's animating), so this is what you click/hover to
    // find out what the red blinker is.
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", x); dot.setAttribute("cy", y);
    dot.setAttribute("r", isConvergence ? 5.5 : 4);
    dot.setAttribute("class", "alertdot" + (isConvergence ? " convergencedot" : ""));
    const t = document.createElementNS(SVG_NS, "title");
    t.textContent = `${ALERT_KIND_LABEL[a.kind] || "Alert"}: ${a.title || ""}`;
    dot.appendChild(t);
    dot.addEventListener("mouseenter", () => setAlertDetail(a));
    // Click both shows the detail AND zooms the map in on the alert -- a
    // breaking marker is easy to spot at world-view but hard to read; one
    // click brings the surrounding region into focus.
    dot.addEventListener("click", () => {
      setAlertDetail(a);
      zoomToPoint(a.lon, a.lat, 150);
    });
    const dotG = markerAnchor(x, y);
    dotG.appendChild(dot);
    alertPulseLayer.appendChild(dotG);
  }
}

function renderTicker(news, deltas, alerts) {
  const track = document.getElementById("tickerTrack");
  if (!track) return;
  const items = [];
  // This is a NEWS monitor: news-type alerts (surge / watchlist / multi-source)
  // and the breaking news itself LEAD. Earthquakes trail quietly at the end.
  const newsAlerts = (alerts || []).filter(a => a.kind !== "world");
  const worldAlerts = (alerts || []).filter(a => a.kind === "world");
  newsAlerts.forEach(a => items.push({
    label: "⚠ " + (a.place || "alert"), title: a.title, url: null, hot: true,
  }));
  // "Genuinely new" = the shared isFreshNews test (same signal as the map
  // pulse rings — "breaking" means one thing). Sports/entertainment is dropped.
  const isNew = isFreshNews;
  const newsworthy = (news || []).filter(h => !h.noise);
  const fresh = newsworthy.filter(isNew);
  const clustered = newsworthy.filter(h => !isNew(h) && h.dupe_count > 1);
  const rest = newsworthy.filter(h => !isNew(h) && !(h.dupe_count > 1));
  [...fresh, ...clustered, ...rest].slice(0, 22).forEach(h => items.push({
    label: h.place || h.source_name || "world",
    title: h.title, url: h.url, hot: isNew(h) || (h.dupe_count || 0) >= 3,
  }));
  // World events (quakes) come LAST, not red, and only if genuinely major.
  worldAlerts.filter(a => (a.severity ?? 0) >= 6.5).slice(0, 3).forEach(a => items.push({
    label: "M" + fmtNum(a.severity), title: a.title, url: null, hot: false,
  }));

  const sig = items.map(i => i.label + "|" + i.title).join("~");
  if (sig === _tickerSig) return;          // unchanged -> leave the scroll running
  _tickerSig = sig;

  if (!items.length) {
    track.innerHTML = '<span class="tempty">Monitoring — no breaking items in the window.</span>';
    track.style.animation = "none";
    return;
  }
  const one = items.map(i => {
    const t = escapeHtml(i.title);
    const body = i.url ? `<a href="${escapeAttr(i.url)}" target="_blank" rel="noopener">${t}</a>` : t;
    return `<span class="titem${i.hot ? " hot" : ""}"><span class="tplace">${escapeHtml(i.label)}</span>`
      + `<span class="tsep">—</span>${body}</span>`;
  }).join("");
  track.innerHTML = one + one;            // duplicate for a seamless -50% loop
  // Scale speed to content (~90px/s) so a busy day scrolls at a readable pace.
  track.style.animation = "none";
  requestAnimationFrame(() => {
    const dur = Math.min(160, Math.max(30, (track.scrollWidth / 2) / 90));
    track.style.animation = `tickerscroll ${dur}s linear infinite`;
  });
}

// Story-tempo sparkline: stories-first-seen per 10-min bin over the last 2h.
// The "is the world getting busier" glance — the visual seed of surge
// detection (the Welford upgrade replaces eyeballing this with a z-score).
// Wall-mode hardening state: the tab is expected to run unattended for weeks.
// - _serverStartedAt: when /api/status's started_at changes, the service was
//   redeployed/restarted -> reload so template changes reach the wall in <=10s.
// - _lastGoodPoll: drives the STALE overlay (unreachable >30s dims the board).
// - nightly reload at 04:00 local clears slow tab creep (classic kiosk hygiene).
let _serverStartedAt = null;
let _lastGoodPoll = null;
const STALE_AFTER_MS = 30 * 1000;

// Nightly reload at 04:00 — but only if the server actually answers, else we'd
// swap the STALE overlay for a Chrome error page. Retry every 5 min while down.
async function nightlyReload() {
  try { await fetchJSON("/api/status"); location.reload(); }
  catch (e) { setTimeout(nightlyReload, 5 * 60 * 1000); }
}
(function scheduleNightlyReload() {
  const now = new Date();
  const next = new Date(now);
  next.setHours(4, 0, 0, 0);
  if (next <= now) next.setDate(next.getDate() + 1);
  setTimeout(nightlyReload, next - now);
})();

// 8s-abort fetch: one hung TCP connection must not stall the whole poll for
// minutes (which would leave stale data up with no STALE takeover).
async function fetchJSON(url) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetch(url, { signal: ctrl.signal });
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

function markStaleIfNeeded() {
  if (_lastGoodPoll === null || (Date.now() - _lastGoodPoll) < STALE_AFTER_MS) return;
  document.body.classList.add("stale");
  const t = document.getElementById("staleTime");
  if (t) t.textContent = new Date(_lastGoodPoll).toLocaleTimeString(undefined,
    { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
