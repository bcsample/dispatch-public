// 06-panels.js — current-state panel, headlines panel, feed status chips

// v2 — /api/current gives per-feed CURRENT state (not just deltas), already
// floored by each feed's min_severity. Flatten every feed's lat/lon records
// into one list so the map shows the world now, same shape renderPins()
// already expects (lat/lon/severity/title/source_name/delta_kind).
function flattenCurrentPins(currentFeeds) {
  const out = [];
  for (const feed of (currentFeeds || [])) {
    for (const r of (feed.records || [])) {
      if (r.lat === null || r.lat === undefined || r.lon === null || r.lon === undefined) continue;
      out.push({ lat: r.lat, lon: r.lon, severity: r.severity, title: r.title, source_name: feed.name, delta_kind: "" });
    }
  }
  return out;
}

// v2 — current-state board. Feeds carrying a severity (quakes, fires) render
// as a ranked list (magnitude desc, ties keep the adapter's own recency
// order — a stable sort). Feeds with no severity signal (sanctions, flights)
// render as a current count + this-sweep's additions/changes, cross-referenced
// against /api/deltas so newly-changed records get a chip.
function renderCurrent(currentFeeds, deltaKeySet) {
  const wrap = document.getElementById("currentLists");
  if (!currentFeeds || !currentFeeds.length) {
    wrap.innerHTML = '<div class="empty">No current-state data yet — waiting on the first sweep.</div>';
    return;
  }
  wrap.innerHTML = currentFeeds.map(feed => {
    const recs = feed.records || [];
    const hasSeverity = recs.some(r => r.severity !== null && r.severity !== undefined);
    const titleLink = r => r.url
      ? `<a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a>`
      : escapeHtml(r.title);
    const chip = r => deltaKeySet.has(feed.name + "||" + r.title) ? '<span class="kind-badge">NEW/CHANGED</span>' : "";

    if (hasSeverity) {
      const ranked = recs.slice().sort((a, b) => (b.severity ?? -Infinity) - (a.severity ?? -Infinity)).slice(0, 15);
      const rows = ranked.map(r => `<div class="delta">
        <div class="sev-dot sev-${sevBucket(r.severity)}"></div>
        <div class="body">
          <div class="title">${titleLink(r)}${chip(r)}</div>
          <div class="sub">${r.severity === null || r.severity === undefined ? "" : "severity " + fmtNum(r.severity)}</div>
        </div>
      </div>`).join("") || '<div class="empty">Nothing above the display floor.</div>';
      return `<div class="feedgroup"><h3>${escapeHtml(feed.name)} — ${feed.total ?? recs.length} current</h3>${rows}</div>`;
    }

    const additions = recs.filter(r => deltaKeySet.has(feed.name + "||" + r.title)).slice(0, 10);
    const rows = additions.map(r => `<div class="delta">
      <div class="sev-dot sev-none"></div>
      <div class="body"><div class="title">${titleLink(r)}${chip(r)}</div></div>
    </div>`).join("") || '<div class="empty">No changes this sweep.</div>';
    return `<div class="feedgroup"><h3>${escapeHtml(feed.name)} — ${feed.total ?? recs.length} current</h3>${rows}</div>`;
  }).join("");
}

// v3 — headlines hero panel: last news_window_hours, newest-first, many
// sources. Reuses brief/ingest/rss.py's fetch via the firehose roster, no
// synthesis. The 2h/undated filtering itself happens server-side
// (service.recent_news) so this just renders whatever /api/news returns.
function fmtClock(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

function renderNews(headlines) {
  const el = document.getElementById("newsList");
  if (!headlines || !headlines.length) {
    el.innerHTML = '<div class="empty">No headlines in the last window — firehose sources unreachable, or still loading.</div>';
    return;
  }
  // v4.1 — dupe_count > 1 means this row is a clustered representative for
  // the same story from multiple sources (brief/window/dedup.py); show a
  // "+N more" badge titled with the other sources' names.
  el.innerHTML = headlines.map(h => {
    let badge = "";
    if (h.dupe_count && h.dupe_count > 1) {
      const others = (h.sources || []).slice(1).join(", ");
      badge = ` <span class="dupebadge" title="Also reported by: ${escapeAttr(others)}">+${h.dupe_count - 1} more</span>`;
    }
    if (h.beat) {
      badge += ` <span class="beatbadge" title="On your beat (local model score ${h.beat_score}/10)">◆ BEAT</span>`;
    }
    return `<div class="headline">
    <div class="time">${fmtClock(h.published_at)}</div>
    <div class="body">
      <div class="title"><a href="${escapeAttr(h.url)}" target="_blank" rel="noopener">${escapeHtml(h.title)}</a>${badge}</div>
      <div class="src">${escapeHtml(h.source_name)}</div>
    </div>
  </div>`;
  }).join("");
}

function renderFeedChips(feeds) {
  const row = document.getElementById("feedRow");
  row.innerHTML = (feeds || []).map(f => {
    const dotClass = f.ok ? "ok" : "bad";
    return `<span class="feedchip"><span class="dot ${dotClass}"></span>${escapeHtml(f.name)} · ${f.records} rec · ${f.deltas_last_sweep} Δ</span>`;
  }).join("");
}
