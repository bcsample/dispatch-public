// 09-voice-main.js — voice orb/mute/read-news buttons + main poll loop bootstrap

// The voice orb: idle breath when the voice is live, fast bright pulse when
// something speak-worthy is pending, dimmed + struck through when it can't
// speak (meeting / Focus / muted / quiet hours) — with the reason on hover.
function renderOrb(voice, alerts) {
  const orb = document.getElementById("jarvisOrb");
  const btn = document.getElementById("readNewsBtn");
  const label = document.getElementById("voiceLabel");
  if (!orb) return;
  const canSpeak = !voice || voice.can_speak !== false;
  const pending = (alerts || []).filter(a => a.speak_worthy).length;
  orb.classList.toggle("muted", !canSpeak);
  orb.classList.toggle("speaking", canSpeak && pending > 0);
  const reason = voice && voice.reason;
  const tooltip = (canSpeak
    ? (pending ? `Voice — ${pending} to announce` : "Voice — listening")
    : `Voice — quiet (${reason})`) + " · click to read the news now";
  if (btn) btn.title = tooltip;
  if (label) {
    label.textContent = canSpeak ? (pending ? `${pending} to announce` : "voice live") : reason;
  }
  renderMuteButton(voice);
}

// The mute button reflects the MANUAL switch; the label/orb still show when
// something else (a meeting, Focus) is what's keeping it quiet.
let _muteBusy = false;
function renderMuteButton(voice) {
  const btn = document.getElementById("muteBtn");
  if (!btn || _muteBusy) return;
  const muted = !!(voice && voice.muted);
  btn.classList.toggle("muted", muted);
  document.getElementById("muteIcon").innerHTML = muted ? "&#128263;" : "&#128266;";
  document.getElementById("muteText").textContent = muted ? "Muted" : "Voice on";
  btn.title = muted
    ? "Voice is muted — click to turn it back on"
    : "Silence the Dispatch voice";
}

(function bindMuteButton() {
  const btn = document.getElementById("muteBtn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const turningOn = !btn.classList.contains("muted");  // currently live -> mute it
    _muteBusy = true;                                    // hold off poll overwrites
    try {
      const r = await fetch(`/api/voice/mute?on=${turningOn}`, { method: "POST" });
      const body = await r.json();
      _muteBusy = false;
      renderMuteButton({ muted: body.muted });
    } catch (e) {
      _muteBusy = false;
    }
  });
})();

// Click "Read news" -> read the news/headlines RIGHT NOW (on-demand,
// server-side reuses the same "one news brain" digest the scheduled bulletin
// and any external voice-agent tool integration reads from). Local only -- no external calls,
// speaks through this machine's speakers. Only blocked by mute/meeting/quiet
// hours -- the exact same gate as the scheduled bulletin.
(function bindReadNewsButton() {
  const btn = document.getElementById("readNewsBtn");
  if (!btn) return;
  let busy = false;
  btn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    btn.classList.add("reading");
    try {
      const r = await fetch("/api/voice/read_news", { method: "POST" });
      const body = await r.json();
      if (!body.ok) {
        const prevTitle = btn.title;
        btn.title = `Can't read right now (${body.reason || "unknown"})`;
        setTimeout(() => { btn.title = prevTitle; }, 4000);
      }
    } catch (e) {
      // network hiccup -- fail silently, button just stops glowing
    } finally {
      busy = false;
      // Keep the glow up for a beat so the click feels acknowledged even
      // though the actual speech (background thread server-side) continues
      // past this point.
      setTimeout(() => btn.classList.remove("reading"), 2500);
    }
  });
})();

let _polling = false;
async function poll() {
  if (_polling) return;   // never stack concurrent polls (a slow cycle otherwise piles up)
  _polling = true;
  try {
    const [status, deltas, current, news, alertsBody, flights, voice, feedHealth] = await Promise.all([
      fetchJSON("/api/status"), fetchJSON("/api/deltas?limit=200"),
      fetchJSON("/api/current"), fetchJSON("/api/news"),
      fetchJSON("/api/alerts"), fetchJSON("/api/flights"), fetchJSON("/api/voice"),
      fetchJSON("/api/feed_health"),
    ]);
    const alerts = (alertsBody && alertsBody.alerts) || [];
    renderOrb(voice, alerts);

    if (status.started_at) {
      if (_serverStartedAt && status.started_at !== _serverStartedAt) { location.reload(); return; }
      _serverStartedAt = status.started_at;
    }
    _lastGoodPoll = Date.now();
    document.body.classList.remove("stale");

    document.getElementById("healthDot").className = "dot " + (status.ok ? "ok" : "bad");
    document.getElementById("healthText").textContent = status.ok ? "live" : "degraded";
    document.getElementById("meta").textContent =
      "last sweep: " + (status.last_sweep_at ? fmtTime(status.last_sweep_at) : "none yet");

    // News-first stats: /api/news is the star (stories/clustered/top story).
    // "Sources reporting" = distinct news outlets carrying a story in the
    // window (counted from /api/news itself, so it's a real news metric, not
    // the two world-feed adapters). Quake/severity context moves to a small
    // badge on the World Events panel, not a headline tile.

    // World events (quakes/sanctions/vulns) are quiet map context now — the
    // dedicated panels were removed to give the map/headlines/Skies room. The
    // deltaKeySet still marks "new this sweep" pins.
    const deltaKeySet = new Set(deltas.map(d => d.source_name + "||" + d.title));
    renderPins(flattenCurrentPins(current), deltaKeySet);
    renderNewsPins(news);
    renderNews(news);
    renderTicker(news, deltas, alerts);
    renderAlertPulses(alerts);
    renderFlights(flights);
    renderFeedHealth(feedHealth && feedHealth.feeds);
    renderThreatGauge(alerts);
  } catch (e) {
    document.getElementById("healthDot").className = "dot bad";
    document.getElementById("healthText").textContent = "unreachable";
    markStaleIfNeeded();
  } finally {
    _polling = false;
  }
}

poll();
setInterval(poll, 10000);
// Independent 1s stale watchdog: a HUNG (not refused) server keeps poll()
// awaiting so its catch never runs — this timer still trips the STALE overlay.
setInterval(markStaleIfNeeded, 1000);
