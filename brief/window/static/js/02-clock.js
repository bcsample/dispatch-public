// 02-clock.js — command-bar clock + world-clock strip ticking

function fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return iso; }
}

// Command-bar clock — local time is what a wall-mounted operator reads,
// UTC is what every feed timestamp is actually in. Pure Date formatting,
// no network call, ticks independently of the 10s poll.
function updateClock() {
  const el = document.getElementById("clock");
  if (!el) return;
  const now = new Date();
  const local = now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const utc = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" });
  el.textContent = local + " local · " + utc + " UTC";
}
// World-clock strip: intel capitals across the theaters that matter, day/night
// tinted so a wall-glance reads where it's morning vs. midnight. Timezone math
// is all Intl (IANA zones), no network, shares the 1s tick with the header.
const WORLD_CLOCKS = [
  ["Washington", "America/New_York"],
  ["London", "Europe/London"],
  ["Kyiv", "Europe/Kyiv"],
  ["Moscow", "Europe/Moscow"],
  ["Jerusalem", "Asia/Jerusalem"],
  ["Tehran", "Asia/Tehran"],
  ["Beijing", "Asia/Shanghai"],
  ["Taipei", "Asia/Taipei"],
  ["Tokyo", "Asia/Tokyo"],
];
function buildWorldClocks() {
  const wrap = document.getElementById("worldClocks");
  if (!wrap) return;
  wrap.innerHTML = WORLD_CLOCKS.map(([city], i) =>
    `<div class="wc" id="wc${i}"><span class="city">${city}</span><span class="t">–</span></div>`
  ).join("");
}
function updateWorldClocks() {
  const now = new Date();
  WORLD_CLOCKS.forEach(([, tz], i) => {
    const cell = document.getElementById("wc" + i);
    if (!cell) return;
    const t = cell.querySelector(".t");
    try {
      t.textContent = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz });
      const hr = parseInt(now.toLocaleString("en-GB", { hour: "2-digit", hour12: false, timeZone: tz }), 10);
      cell.className = "wc " + ((hr < 6 || hr >= 19) ? "night" : "day");
    } catch (e) { t.textContent = "—"; }
  });
}
buildWorldClocks();

function tickClocks() { updateClock(); updateWorldClocks(); }
tickClocks();
setInterval(tickClocks, 1000);
