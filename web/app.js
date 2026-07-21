/* UK Data Centre Grid — pipeline monitor */

const COLORS = {
  Undecided: "#e8a24d",  // pastel peach — the live pipeline
  Permitted: "#6fbf8e",  // pastel green
  Conditions: "#6cc0b8", // pastel mint
  Rejected: "#e3877f",   // pastel coral
  Withdrawn: "#b09ce0",  // pastel lavender
  Unknown: "#b3bdb3",
  Referred: "#b3bdb3",
  Unresolved: "#b3bdb3",
};

const stateMatch = () => {
  const expr = ["match", ["get", "state"]];
  for (const [k, v] of Object.entries(COLORS)) expr.push(k, v);
  expr.push("#8ea3b0");
  return expr;
};

// Dark basemap (CARTO, no API key) recoloured toward the grid aesthetic.
const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    glyphs: "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf",
    sources: {
      carto: {
        type: "raster",
        tiles: [
          "https://cartodb-basemaps-a.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png",
          "https://cartodb-basemaps-b.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png",
          "https://cartodb-basemaps-c.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png",
        ],
        tileSize: 256,
        attribution: "© OpenStreetMap · © CARTO · PlanIt",
      },
    },
    layers: [
      {
        id: "carto",
        type: "raster",
        source: "carto",
        paint: { "raster-opacity": 0.82, "raster-saturation": -0.28, "raster-brightness-min": 0.05 },
      },
    ],
  },
  center: [-1.4, 52.6],
  zoom: 5.4,
  minZoom: 4,
  maxZoom: 15,
  attributionControl: { compact: true },
});

map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

map.on("load", async () => {
  const data = await fetch("api/sites.geojson").then((r) => r.json());
  map.addSource("sites", { type: "geojson", data });

  // Outer glow — radius scales with the number of applications on the site.
  map.addLayer({
    id: "sites-glow",
    type: "circle",
    source: "sites",
    paint: {
      "circle-color": stateMatch(),
      "circle-blur": 1,
      "circle-opacity": 0.28,
      "circle-radius": ["interpolate", ["linear"], ["get", "n_apps"], 1, 7, 5, 14, 40, 28],
    },
  });
  // Core node — radius scales with zoom.
  map.addLayer({
    id: "sites-core",
    type: "circle",
    source: "sites",
    paint: {
      "circle-color": stateMatch(),
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 3, 12, 7],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 1.5,
      "circle-opacity": 0.92,
    },
  });

  map.on("click", "sites-core", (e) => openFlyout(e.features[0].properties));
  for (const l of ["sites-core", "sites-glow"]) {
    map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "crosshair"));
    map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
  }
});

/* ---------- Filters ---------- */
const active = new Set(["Undecided", "Permitted", "Conditions", "Rejected", "Withdrawn"]);
document.querySelectorAll(".leg").forEach((btn) => {
  btn.addEventListener("click", () => {
    const s = btn.dataset.state;
    if (s === "all") {
      const turnOn = !btn.classList.contains("active");
      document.querySelectorAll(".leg").forEach((b) => b.classList.toggle("active", turnOn));
      active.clear();
      if (turnOn) ["Undecided", "Permitted", "Conditions", "Rejected", "Withdrawn"].forEach((x) => active.add(x));
    } else {
      btn.classList.toggle("active");
      btn.classList.contains("active") ? active.add(s) : active.delete(s);
      document.querySelector('.leg[data-state="all"]').classList.toggle("active", active.size === 5);
    }
    applyFilter();
  });
});

function applyFilter() {
  const states = [...active];
  const f = states.length ? ["in", ["get", "state"], ["literal", states]] : ["==", ["get", "state"], "__none__"];
  for (const l of ["sites-glow", "sites-core"]) if (map.getLayer(l)) map.setFilter(l, f);
}

/* ---------- Overlay cards (About / Methodology) ---------- */
const overlay = document.getElementById("overlay");
const cards = { about: document.getElementById("card-about"), method: document.getElementById("card-method") };
function openCard(which) {
  Object.entries(cards).forEach(([k, el]) => (el.hidden = k !== which));
  overlay.classList.add("open");
}
function closeCard() { overlay.classList.remove("open"); }
document.querySelectorAll(".nav-link").forEach((b) => b.addEventListener("click", () => openCard(b.dataset.card)));
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeCard));
overlay.addEventListener("click", (e) => { if (e.target === overlay) closeCard(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCard(); });

/* ---------- Flyout ---------- */
const flyout = document.getElementById("flyout");
function openFlyout(p) {
  const color = COLORS[p.state] || COLORS.Unknown;
  const st = document.getElementById("fly-state");
  st.textContent = (p.state || "—").toUpperCase();
  st.style.background = color;
  st.style.color = "#fff";
  document.getElementById("fly-area").textContent = p.area || "—";
  document.getElementById("fly-meta").innerHTML = `
    <div class="row"><span class="k">APPLICATIONS</span><span class="v">${p.n_apps}</span></div>
    <div class="row"><span class="k">TYPE</span><span class="v">${p.type || "—"}</span></div>
    <div class="row"><span class="k">FIRST SEEN</span><span class="v">${p.year || "—"}</span></div>
    <div class="row"><span class="k">AGENT</span><span class="v">${p.agent || "—"}</span></div>`;
  document.getElementById("fly-desc").textContent = p.description || "";
  document.getElementById("fly-link").href = p.link || "#";
  flyout.classList.add("open");
}
document.getElementById("flyout-close").addEventListener("click", () => flyout.classList.remove("open"));

/* ---------- Stats ---------- */
fetch("api/stats").then((r) => r.json()).then((s) => {
  animateNum("m-sites", s.sites);
  document.getElementById("m-apps").textContent = s.applications.toLocaleString();
  document.getElementById("m-mapped").textContent = s.mapped_sites.toLocaleString();
  animateNum("m-undecided", s.undecided_sites);
  document.getElementById("m-refusal").textContent = s.refusal_pct + "%";
  document.getElementById("m-withdrawn").textContent = s.withdrawal_pct + "%";
  document.getElementById("m-median").innerHTML = (s.median_days ?? "—") + '<span class="unit">d</span>';

  // Card figures (About / Methodology)
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set("ab-sites", s.sites.toLocaleString());
  set("ab-apps", s.applications.toLocaleString());
  set("ab-live", s.undecided_sites.toLocaleString());
  set("me-apps", s.applications.toLocaleString());
  set("me-sites", s.sites.toLocaleString());
  set("me-material", (s.material_sites ?? "—").toLocaleString());
  set("me-median", s.median_days ?? "—");
  set("me-lapsed", (s.lapsed_dead_sites ?? "—").toLocaleString());
  set("me-mapped", s.mapped_sites.toLocaleString());

  // Year bars
  const bars = document.getElementById("year-bars");
  const max = Math.max(...s.by_year.map((y) => y.sites), 1);
  s.by_year.forEach((y) => {
    const b = document.createElement("div");
    b.className = "bar";
    b.style.height = "0%";
    b.innerHTML = `<span class="tip">${y.year} · ${y.sites}</span>`;
    bars.appendChild(b);
    requestAnimationFrame(() => (b.style.height = (y.sites / max) * 100 + "%"));
  });

  // Areas
  const list = document.getElementById("area-list");
  const amax = Math.max(...s.top_areas.map((a) => a.sites), 1);
  s.top_areas.forEach((a) => {
    const row = document.createElement("div");
    row.className = "area-row";
    row.innerHTML = `<span class="area-name">${a.area}</span>
      <span class="area-track">
        <span class="area-fill" style="flex-grow:${a.sites}"></span>
        <span style="flex-grow:${amax - a.sites}"></span>
      </span>
      <span class="area-val">${a.sites}</span>`;
    list.appendChild(row);
  });
});

function animateNum(id, target) {
  const el = document.getElementById(id);
  const dur = 1100, start = performance.now();
  const step = (now) => {
    const t = Math.min((now - start) / dur, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(eased * target).toLocaleString();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------- Clock ---------- */
setInterval(() => {
  const d = new Date();
  document.getElementById("clock").textContent = d.toLocaleTimeString("en-GB", { hour12: false }) + " UTC";
}, 1000);
