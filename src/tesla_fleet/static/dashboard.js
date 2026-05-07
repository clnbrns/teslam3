// Dashboard — polls FastAPI service.
const VIN_KEY = "fleet.vin";
const POLL_MS = 10_000;
const $ = (id) => document.getElementById(id);

let timer = null;
let lastEventCount = 0;

function setStatus(online) {
  const pill = $("status-pill");
  if (!pill) return;
  pill.textContent = online ? "Online" : "Offline";
  pill.className = "status-dot " + (online ? "online" : "offline");
}

function fmt(n, d = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function refreshStatus(_vin) {
  // /api/status was removed to cut Tesla Fleet API quota usage. The hidden
  // /dashboard page no longer shows live speed/battery; refresh from the
  // Map page (uses logged samples) or the Tesla mobile app.
  setStatus(false);
}

async function refreshEvents() {
  try {
    const r = await fetch("/api/events?limit=20");
    if (!r.ok) return;
    const events = await r.json();
    const ul = $("events");
    ul.innerHTML = "";

    let lastDriver = null;
    for (const ev of events) {
      const li = document.createElement("li");
      const time = new Date((ev.ts || 0) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      let cls = "", tag = ev.type || "evt", body = "";

      if (ev.type === "driver_sample") {
        if (!lastDriver) lastDriver = ev.driver;
        const e = ev.event;
        if (e?.type === "hard_brake") {
          cls = "brake"; tag = "Hard brake";
          body = `<b>${ev.driver}</b> · ${ev.speed_mph} mph <span class="meta">Δ ${e.delta_mph_per_s} mph/s</span>`;
        } else if (e?.type === "rapid_accel") {
          cls = "accel"; tag = "Rapid accel";
          body = `<b>${ev.driver}</b> · ${ev.speed_mph} mph <span class="meta">Δ ${e.delta_mph_per_s} mph/s</span>`;
        } else {
          tag = "Drive";
          body = `<b>${ev.driver}</b> · ${ev.speed_mph ?? "—"} mph <span class="meta">${ev.shift_state ?? ""}</span>`;
        }
      } else if (ev.type === "roi_report") {
        cls = "roi"; tag = "ROI";
        body = `${fmt(ev.total_miles, 1)} mi · saved $${fmt(ev.savings_usd, 2)}`;
      } else {
        body = ev.type;
      }

      li.className = cls;
      li.innerHTML = `<span class="ts">${time}</span><span class="tag">${tag}</span><span class="body">${body}</span>`;
      ul.appendChild(li);
    }

    if (lastDriver) $("driver").textContent = lastDriver;
    if (!ul.children.length) ul.innerHTML = '<li><span class="muted">No events yet — start the monitor.</span></li>';

    if (events.length > lastEventCount) {
      const fresh = events.slice(0, events.length - lastEventCount);
      const dramatic = fresh.find(e =>
        e.type === "driver_sample" && (e.event?.type === "hard_brake" || e.event?.type === "rapid_accel")
      );
      if (dramatic) toast(dramatic.event.type === "hard_brake" ? "Hard brake detected" : "Rapid acceleration detected");
    }
    lastEventCount = events.length;
  } catch (_) { /* no-op */ }
}

async function tick() {
  const vin = $("vin").value.trim();
  if (!vin) { refreshEvents(); return; }
  await Promise.all([refreshStatus(vin), refreshEvents()]);
}

function start() {
  const vin = $("vin").value.trim();
  if (!vin) { toast("Enter a VIN"); return; }
  localStorage.setItem(VIN_KEY, vin);
  if (timer) clearInterval(timer);
  tick();
  timer = setInterval(tick, POLL_MS);
}

window.refresh = tick;
document.addEventListener("DOMContentLoaded", () => {
  const saved = localStorage.getItem(VIN_KEY);
  if (saved) { $("vin").value = saved; start(); }
  $("connect").addEventListener("click", start);
  $("vin").addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
  refreshEvents();
  setInterval(refreshEvents, POLL_MS);
});
