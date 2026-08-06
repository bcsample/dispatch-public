// 11-threat-gauge.js — world-state/threat-index gauge: a single glanceable
// "how hot is the world right now", weighted from the SAME active alerts
// (dispatch-alerts-v1) that already drive the ticker and map pulses. Pure
// client-side derivation -- no new backend endpoint, one signal several views.

// Per-alert weight by kind (and severity, for world events) -- a rough,
// deliberately simple "how much does this matter" score, not a calibrated
// model. Tune here if the gauge feels too twitchy or too flat in practice.
function _alertWeight(a) {
  if (a.kind === "world") return (a.severity != null && a.severity >= 7) ? 18 : 10;
  if (a.kind === "surge") return 12;
  if (a.kind === "watchlist") return 8;
  if (a.kind === "news") return 6;
  return 4;
}

function computeThreatIndex(alerts) {
  const score = (alerts || []).reduce((sum, a) => sum + _alertWeight(a), 0);
  return Math.max(0, Math.min(100, score));
}

const THREAT_TIERS = [
  { max: 20, label: "Quiet", color: "var(--green)" },
  { max: 45, label: "Elevated", color: "var(--yellow)" },
  { max: 70, label: "Active", color: "#ff8c3a" },
  { max: 101, label: "Critical", color: "var(--red)" },
];

function threatTier(score) {
  return THREAT_TIERS.find(t => score < t.max) || THREAT_TIERS[THREAT_TIERS.length - 1];
}

function renderThreatGauge(alerts) {
  const fill = document.getElementById("tgFill");
  const label = document.getElementById("tgLabel");
  const wrap = document.getElementById("threatGauge");
  if (!fill || !label) return;
  const score = computeThreatIndex(alerts);
  const tier = threatTier(score);
  fill.style.width = score + "%";
  fill.style.background = tier.color;
  label.textContent = tier.label;
  if (wrap) wrap.title = `World-state index: ${score}/100 (${tier.label}) — weighted from active alerts`;
}
