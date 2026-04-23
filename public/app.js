/* global GridStack, Hls */

let grid;
let cameras = [];
let columns = 3;
let editing = false;
let configHash = "";
const players = new Map(); // cameraId -> { hls, video, pollTimer }

// --- Init ---
document.addEventListener("DOMContentLoaded", async () => {
  grid = GridStack.init({
    column: columns,
    cellHeight: 280,
    animate: true,
    draggable: { handle: ".camera-header" },
    resizable: { handles: "se, sw, e, w, n, s" },
    float: false,
    disableDrag: true,
    disableResize: true,
    margin: 0,
  });

  grid.on("change", onLayoutChange);

  await loadConfig();
  fitGridToViewport();
  window.addEventListener("resize", fitGridToViewport);

  document.getElementById("btn-edit").addEventListener("click", toggleEditMode);
  document.getElementById("btn-add").addEventListener("click", openAddModal);
  document.getElementById("btn-cancel").addEventListener("click", closeModal);
  document.getElementById("camera-form").addEventListener("submit", onFormSubmit);
  document.getElementById("btn-download").addEventListener("click", downloadConfig);
  document.getElementById("file-import").addEventListener("change", importConfig);
  document.getElementById("btn-columns").addEventListener("click", cycleColumns);
  document.getElementById("btn-fullscreen").addEventListener("click", toggleFullscreen);

  document.getElementById("camera-grid").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const camId = btn.getAttribute("data-camera-id");
    if (btn.dataset.action === "edit") openEditModal(camId);
    else if (btn.dataset.action === "delete") deleteCamera(camId);
  });

  initHeaderAutoHide();
  initConfigWatcher();

});

// --- Viewport Fit ---
function fitGridToViewport() {
  const availableHeight = window.innerHeight;

  // Find the max row bottom in the grid
  let maxRow = 1;
  grid.engine.nodes.forEach((node) => {
    const bottom = node.y + node.h;
    if (bottom > maxRow) maxRow = bottom;
  });

  if (maxRow < 1) maxRow = 1;
  const cellHeight = Math.floor(availableHeight / maxRow);
  grid.cellHeight(cellHeight);
}

// --- Edit Mode ---
function toggleEditMode() {
  editing = !editing;
  const btn = document.getElementById("btn-edit");
  const gridEl = document.getElementById("camera-grid");

  const header = document.getElementById("main-header");
  if (editing) {
    btn.textContent = "Save Layout";
    btn.classList.add("active");
    document.getElementById("btn-add").hidden = false;
    document.getElementById("btn-columns").hidden = false;
    gridEl.classList.add("editing");
    grid.enableMove(true);
    grid.enableResize(true);
    header.classList.add("visible");
  } else {
    btn.textContent = "Edit Layout";
    btn.classList.remove("active");
    document.getElementById("btn-add").hidden = true;
    document.getElementById("btn-columns").hidden = true;
    gridEl.classList.remove("editing");
    grid.enableMove(false);
    grid.enableResize(false);
    header.classList.remove("visible");
    // Force save current layout and fit to viewport
    saveCurrentLayout();
    fitGridToViewport();
  }
}

function saveCurrentLayout() {
  const items = [];
  grid.engine.nodes.forEach((node) => {
    items.push({
      id: node.el?.getAttribute("gs-id") || node.id,
      x: node.x,
      y: node.y,
      w: node.w,
      h: node.h,
    });
  });
  fetch("/api/layout", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, columns }),
  });
}

// --- Config ---
async function loadConfig() {
  const resp = await fetch("/api/config");
  const config = await resp.json();
  cameras = config.cameras || [];
  columns = config.layout?.columns || 3;

  document.getElementById("col-count").textContent = columns;
  grid.column(columns);

  // Add camera widgets
  for (const cam of cameras) {
    addCameraWidget(cam);
  }
}

function onLayoutChange(_event, items) {
  if (!items || items.length === 0) return;

  const payload = items.map((node) => ({
    id: node.el?.getAttribute("gs-id") || node.id,
    x: node.x,
    y: node.y,
    w: node.w,
    h: node.h,
  }));

  fetch("/api/layout", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: payload, columns }),
  });
}

async function downloadConfig() {
  const resp = await fetch("/api/config/download");
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "cctv-config.json";
  a.click();
  URL.revokeObjectURL(url);
}

async function importConfig(e) {
  const file = e.target.files[0];
  if (!file) return;

  const text = await file.text();
  let config;
  try {
    config = JSON.parse(text);
  } catch {
    alert("Invalid JSON file");
    return;
  }

  // Stop all current players
  for (const [id] of players) {
    destroyPlayer(id);
  }
  grid.removeAll();

  const resp = await fetch("/api/config/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });

  if (resp.ok) {
    cameras = config.cameras;
    columns = config.layout?.columns || 3;
    document.getElementById("col-count").textContent = columns;
    grid.column(columns);
    for (const cam of cameras) {
      addCameraWidget(cam);
    }
  } else {
    alert("Failed to import config");
  }

  // Reset file input
  e.target.value = "";
}

function cycleColumns() {
  const options = [1, 2, 3, 4, 5, 6];
  const idx = options.indexOf(columns);
  columns = options[(idx + 1) % options.length];
  document.getElementById("col-count").textContent = columns;
  grid.column(columns);

  fetch("/api/layout", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: [], columns }),
  });
}

// --- Camera Widget ---
function addCameraWidget(cam) {
  const id = cam.id;
  const content = `
    <div class="camera-header">
      <span class="cam-name" title="${escHtml(cam.name)}">${escHtml(cam.name)}</span>
      <span class="cam-actions">
        <button class="edit" data-action="edit" data-camera-id="${escHtml(id)}" title="Edit">&#9998;</button>
        <button class="delete" data-action="delete" data-camera-id="${escHtml(id)}" title="Delete">&times;</button>
      </span>
    </div>
    <div class="video-container" id="vc-${id}">
      <video id="video-${id}" muted autoplay playsinline></video>
      <div class="status-msg" id="status-${id}">Connecting...</div>
    </div>
  `;

  grid.addWidget({
    id: id,
    x: cam.x,
    y: cam.y,
    w: cam.w || 1,
    h: cam.h || 1,
    content: content,
  });

  // Start polling for stream readiness
  pollStream(cam);
}

function pollStream(cam) {
  const statusEl = document.getElementById(`status-${cam.id}`);
  const videoEl = document.getElementById(`video-${cam.id}`);
  if (!statusEl || !videoEl) return;

  let attempts = 0;
  const maxAttempts = 60; // 60 seconds

  const timer = setInterval(async () => {
    attempts++;
    try {
      const resp = await fetch(`/api/cameras/${cam.id}/status`);
      const status = await resp.json();

      if (status.ready) {
        clearInterval(timer);
        statusEl.style.display = "none";
        startPlayer(cam.id, videoEl);
      } else if (attempts >= maxAttempts) {
        clearInterval(timer);
        statusEl.textContent = "Stream timeout - check URL";
        statusEl.classList.add("error");
      }
    } catch {
      // Server might be processing, keep trying
    }
  }, 1000);

  players.set(cam.id, { pollTimer: timer });
}

function startPlayer(cameraId, videoEl) {
  const src = `/streams/${cameraId}/stream.m3u8`;
  const existing = players.get(cameraId);

  if (Hls.isSupported()) {
    const hls = new Hls({
      liveSyncDurationCount: 1,
      liveMaxLatencyDurationCount: 3,
      enableWorker: true,
      lowLatencyMode: true,
    });
    hls.loadSource(src);
    hls.attachMedia(videoEl);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      videoEl.play().catch(() => {});
    });
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        const statusEl = document.getElementById(`status-${cameraId}`);
        if (statusEl) {
          statusEl.textContent = "Stream error - retrying...";
          statusEl.style.display = "";
          statusEl.classList.add("error");
        }
        // Retry after a delay
        setTimeout(() => {
          if (statusEl) statusEl.style.display = "none";
          hls.loadSource(src);
        }, 3000);
      }
    });

    if (existing) existing.hls = hls;
    else players.set(cameraId, { hls });
  } else if (videoEl.canPlayType("application/vnd.apple.mpegurl")) {
    // Safari native HLS
    videoEl.src = src;
    videoEl.play().catch(() => {});
  }
}

function destroyPlayer(cameraId) {
  const player = players.get(cameraId);
  if (!player) return;
  if (player.pollTimer) clearInterval(player.pollTimer);
  if (player.hls) {
    player.hls.destroy();
  }
  players.delete(cameraId);
}

// --- Modal ---
function openAddModal() {
  document.getElementById("modal-title").textContent = "Add Camera";
  document.getElementById("cam-id").value = "";
  document.getElementById("cam-name").value = "";
  document.getElementById("cam-url").value = "";
  document.getElementById("modal-overlay").hidden = false;
  document.getElementById("cam-name").focus();
}

function openEditModal(id) {
  const cam = cameras.find((c) => c.id === id);
  if (!cam) return;
  document.getElementById("modal-title").textContent = "Edit Camera";
  document.getElementById("cam-id").value = cam.id;
  document.getElementById("cam-name").value = cam.name;
  document.getElementById("cam-url").value = cam.url;
  document.getElementById("modal-overlay").hidden = false;
  document.getElementById("cam-name").focus();
}

function closeModal() {
  document.getElementById("modal-overlay").hidden = true;
}

async function onFormSubmit(e) {
  e.preventDefault();
  const id = document.getElementById("cam-id").value;
  const name = document.getElementById("cam-name").value.trim();
  const url = document.getElementById("cam-url").value.trim();

  if (!name || !url) return;

  if (id) {
    // Edit existing
    const resp = await fetch(`/api/cameras/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url }),
    });
    if (resp.ok) {
      const updated = await resp.json();
      const idx = cameras.findIndex((c) => c.id === id);
      if (idx !== -1) cameras[idx] = updated;

      // Update the name in the DOM
      const widget = document.querySelector(`[gs-id="${id}"]`);
      if (widget) {
        const nameEl = widget.querySelector(".cam-name");
        if (nameEl) {
          nameEl.textContent = name;
          nameEl.title = name;
        }
      }

      // Restart player if URL changed
      destroyPlayer(id);
      const cam = cameras.find((c) => c.id === id);
      if (cam) pollStream(cam);
    }
  } else {
    // Add new
    const resp = await fetch("/api/cameras", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url }),
    });
    if (resp.ok) {
      const cam = await resp.json();
      cameras.push(cam);
      addCameraWidget(cam);
      fitGridToViewport();
    }
  }

  closeModal();
}

async function deleteCamera(id) {
  if (!confirm("Remove this camera?")) return;

  const resp = await fetch(`/api/cameras/${id}`, { method: "DELETE" });
  if (resp.ok) {
    destroyPlayer(id);
    const el = document.querySelector(`[gs-id="${id}"]`);
    if (el) grid.removeWidget(el);
    cameras = cameras.filter((c) => c.id !== id);
    fitGridToViewport();
  }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// --- Fullscreen ---
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
}

document.addEventListener("fullscreenchange", () => {
  const btn = document.getElementById("btn-fullscreen");
  btn.textContent = document.fullscreenElement ? "Exit Fullscreen" : "Fullscreen";
  fitGridToViewport();
});

// --- Header Auto-Hide ---
function initHeaderAutoHide() {
  const header = document.getElementById("main-header");
  const trigger = document.getElementById("header-trigger");
  let hideTimeout;

  function showHeader() {
    clearTimeout(hideTimeout);
    header.classList.add("visible");
  }

  function scheduleHide() {
    clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => {
      // Don't hide if in edit mode
      if (!editing) header.classList.remove("visible");
    }, 600);
  }

  trigger.addEventListener("mouseenter", showHeader);
  header.addEventListener("mouseenter", showHeader);
  header.addEventListener("mouseleave", scheduleHide);
  trigger.addEventListener("mouseleave", scheduleHide);
}

// --- Config Watcher ---
function initConfigWatcher() {
  setInterval(async () => {
    if (editing) return;
    try {
      const resp = await fetch("/api/config");
      const text = await resp.text();
      if (!configHash) {
        configHash = text;
        return;
      }
      if (text !== configHash) {
        // Config changed externally — reload the page
        window.location.reload();
      }
    } catch {
      // Server unavailable, skip
    }
  }, 3000);
}
