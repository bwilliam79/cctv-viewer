/* global GridStack, Hls */

let grid;
let cameras = [];
let columns = 3;
let editing = false;
let configHash = "";
const players = new Map(); // cameraId -> { hls, video, pollTimer }

// ============================================================
// Backend Recovery Coordinator
// Makes redeploys / container restarts much less painful.
// ============================================================

let backendHealthy = true;
let lastSuccessfulPing = Date.now();
let backendDownSince = null;
let reconnectBanner = null;

let currentAppVersion = null;
let versionCheckInterval = null;

function createReconnectUI() {
  if (reconnectBanner) return;

  // Purely informational banner — no button because media-server is headless
  // (no keyboard/mouse on the display running the viewer).
  reconnectBanner = document.createElement("div");
  reconnectBanner.id = "reconnect-banner";
  reconnectBanner.style.cssText = `
    position: fixed; top: 0; left: 0; right: 0; z-index: 99999;
    background: #b45309; color: white; padding: 8px 12px;
    font-size: 14px; font-weight: 500; display: none; align-items: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    font-family: system-ui, -apple-system, sans-serif;
    min-height: 32px;
  `;
  reconnectBanner.innerHTML = `
    <span id="reconnect-text">Backend connection lost — streams will recover automatically</span>
  `;
  document.body.prepend(reconnectBanner);

  // Ensure it's visible even if something is covering the top
  console.log("[recovery] Reconnect banner created and prepended to body");
}

function showReconnectBanner(message) {
  if (!reconnectBanner) createReconnectUI();
  document.getElementById("reconnect-text").textContent = message;
  reconnectBanner.style.display = "flex";
}

function hideReconnectBanner() {
  if (reconnectBanner) reconnectBanner.style.display = "none";
}

function initBackendHealthMonitor() {
  // Check backend health more frequently (every 4 seconds).
  // We now also periodically check the actual per-camera status endpoints.
  // This catches cases where the backend responds to /ping but the streams
  // themselves are broken — something we've seen cause long black screens.
  setInterval(async () => {
    try {
      const resp = await fetch("/api/ping", { cache: "no-store" });
      const now = Date.now();

      if (resp.ok) {
        lastSuccessfulPing = now;

        // Also check if any cameras are reporting not ready for a while.
        // If so, treat it as a stream-level outage even if /ping succeeds.
        const cameraStatuses = await Promise.all(
          Array.from(players.keys()).map(id =>
            fetch(`/api/cameras/${id}/status`, { cache: "no-store" })
              .then(r => r.ok ? r.json() : { ready: false })
              .catch(() => ({ ready: false }))
          )
        );
        const anyCameraNotReady = cameraStatuses.some(s => !s.ready);

        if (!backendHealthy || anyCameraNotReady) {
          backendHealthy = true;
          const downDuration = backendDownSince ? Math.round((now - backendDownSince) / 1000) : 0;
          backendDownSince = null;

          hideReconnectBanner();
          console.log(`[recovery] Backend/streams recovered after ~${downDuration}s. Triggering recovery...`);

          setTimeout(() => {
            recoverAllPlayers();
          }, 1500);
        }
      }
    } catch {
      if (backendHealthy) {
        backendHealthy = false;
        backendDownSince = Date.now();
        showReconnectBanner("Backend unavailable — streams will recover automatically when it returns");
        console.log("[recovery] Backend outage detected (ping failed)");
      }
    }
  }, 4000);
}

// Fallback: Periodically check if the UI looks stuck in error state.
// If many cameras have been showing "retrying" or error for a while,
// force the good recovery path. This helps when the /api/ping detection
// misses a short outage.
function initStuckRecoveryChecker() {
  let stuckSince = null;

  setInterval(() => {
    const errorStatuses = document.querySelectorAll('.status-msg.error');
    const totalCameras = document.querySelectorAll('.status-msg').length;

    if (totalCameras > 0 && errorStatuses.length >= Math.ceil(totalCameras * 0.5)) {
      // Lowered threshold to 50% so we react faster with only 4 cameras.
      if (!stuckSince) {
        stuckSince = Date.now();
        console.log("[recovery] Cameras stuck in error state — forcing recovery");
        recoverAllPlayers();
      } else {
        const stuckDuration = Math.round((Date.now() - stuckSince) / 1000);

        // Escalation: After 60s of being stuck, force a full page reload.
        // This is more aggressive than the previous 90s, but still conservative
        // enough to give the proper recovery logic a chance first.
        if (stuckDuration > 60) {
          console.log(`[recovery] Streams still stuck after ${stuckDuration}s — forcing page reload`);
          showReconnectBanner("Recovery failed — reloading page...");
          setTimeout(() => {
            window.location.reload();
          }, 3000);
        } else if (stuckDuration > 30) {
          // Retry recovery after 30s
          console.log(`[recovery] Still stuck after ${stuckDuration}s — retrying recovery`);
          recoverAllPlayers();
        }
      }
    } else {
      stuckSince = null;
    }
  }, 25000);
}

// --- Version-based auto-reload ---
// This lets the kiosk browser pick up new deployments without a manual
// hard refresh (which is impossible on a truly headless display).
function initVersionChecker() {
  // Check every 30 seconds. Very cheap endpoint.
  versionCheckInterval = setInterval(async () => {
    try {
      const resp = await fetch("/api/version", { cache: "no-store" });
      if (!resp.ok) return;

      const data = await resp.json();
      const newVersion = data.version;

      if (currentAppVersion === null) {
        // First load
        currentAppVersion = newVersion;
        return;
      }

      if (newVersion !== currentAppVersion) {
        console.log(`[version] New deployment detected (${currentAppVersion} → ${newVersion}). Reloading...`);
        // Small delay so any recovery banners can be seen if desired
        setTimeout(() => {
          window.location.reload();
        }, 2000);
      }
    } catch {
      // Ignore transient errors
    }
  }, 30000);
}

function recoverAllPlayers() {
  if (players.size === 0) return;

  console.log("[recovery] recoverAllPlayers() called — showing banner and starting recovery sequence");
  showReconnectBanner("Backend recovered — reconnecting streams...");

  // Nuclear option: Give the new ffmpeg processes a solid head start.
  // Many "black screen after retrying" cases happen because we try to
  // attach HLS.js too early, before the playlist + segments are stable.
  const RECOVERY_DELAY_MS = 8000; // 8 seconds of grace after backend reports ready

  console.log(`[recovery] Backend recovered. Waiting ${RECOVERY_DELAY_MS}ms before attempting to reconnect streams...`);

  setTimeout(() => {
    let recovered = 0;

    for (const [cameraId, playerData] of players) {
      const videoEl = document.getElementById(`video-${cameraId}`);
      const container = document.getElementById(`vc-${cameraId}`);
      const statusEl = document.getElementById(`status-${cameraId}`);

      if (!videoEl || !container) continue;

      try {
        // Aggressively clean up everything for this camera
        if (playerData.hls) playerData.hls.destroy();
        if (playerData.stallWatchdog) clearInterval(playerData.stallWatchdog);
        if (playerData.recoveryTimer) clearTimeout(playerData.recoveryTimer);
        if (playerData.pollTimer) clearInterval(playerData.pollTimer);

        // Fully reset the video element by replacing it.
        const newVideo = document.createElement('video');
        newVideo.id = `video-${cameraId}`;
        newVideo.muted = true;
        newVideo.autoplay = true;
        newVideo.playsInline = true;

        videoEl.parentNode.replaceChild(newVideo, videoEl);

        if (statusEl) {
          statusEl.textContent = "Connecting...";
          statusEl.classList.remove("error");
          statusEl.style.display = "";
        }

        const cam = cameras.find(c => c.id === cameraId);
        if (cam) {
          players.delete(cameraId);
          // Use a more patient polling function after a full backend outage
          pollStreamAfterRecovery(cam, newVideo);
          recovered++;
        }
      } catch (e) {
        console.warn("Error during recovery for", cameraId, e);
      }
    }

    console.log(`[recovery] Triggered recovery for ${recovered} stream(s) after stabilization delay`);

    setTimeout(() => {
      if (reconnectBanner && reconnectBanner.style.display !== "none") {
        hideReconnectBanner();
      }
    }, 25000);
  }, RECOVERY_DELAY_MS);
}

// More patient version used after a full backend outage.
// Requires 3 consecutive "ready" responses before starting the player.
// This dramatically reduces the "attach too early → immediate error → black screen" problem.
function pollStreamAfterRecovery(cam, videoEl) {
  const statusEl = document.getElementById(`status-${cam.id}`);
  if (!statusEl || !videoEl) return;

  let attempts = 0;
  let consecutiveReady = 0;
  const REQUIRED_CONSECUTIVE_READY = 3;
  const maxAttempts = 90; // allow more time

  const pollIntervals = [1000, 1500, 2000, 2500];

  const timer = setInterval(async () => {
    attempts++;

    try {
      const resp = await fetch(`/api/cameras/${cam.id}/status`);
      const status = await resp.json();

      if (status.ready) {
        consecutiveReady++;
      } else {
        consecutiveReady = 0;
      }

      if (consecutiveReady >= REQUIRED_CONSECUTIVE_READY) {
        clearInterval(timer);
        statusEl.style.display = "none";
        console.log(`[recovery] ${cam.id} stable for ${REQUIRED_CONSECUTIVE_READY} checks — starting player`);
        startPlayer(cam.id, videoEl);
      } else if (attempts >= maxAttempts) {
        clearInterval(timer);
        statusEl.textContent = "Stream timeout - check URL";
        statusEl.classList.add("error");
      } else {
        // Still waiting for stability
        if (statusEl) {
          statusEl.textContent = `Connecting... (${consecutiveReady}/${REQUIRED_CONSECUTIVE_READY})`;
        }
      }
    } catch {
      consecutiveReady = 0;
    }
  }, pollIntervals[Math.min(attempts, pollIntervals.length - 1)]);

  players.set(cam.id, { pollTimer: timer });
}

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
  createReconnectUI();
  initBackendHealthMonitor();
  initVersionChecker();
  initStuckRecoveryChecker();

  // Fetch initial version so the checker has a baseline immediately
  fetch("/api/version", { cache: "no-store" })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (data && data.version) {
        currentAppVersion = data.version;
      }
    })
    .catch(() => {});

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

  // Progressive backoff during the initial "Connecting..." phase.
  // Previously fixed 1s interval for up to 60s per camera created unnecessary load.
  const pollIntervals = [800, 1200, 1800, 2500, 3000];
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
  }, pollIntervals[Math.min(attempts, pollIntervals.length - 1)]);

  players.set(cam.id, { pollTimer: timer });
}

function startPlayer(cameraId, videoEl) {
  const src = `/streams/${cameraId}/stream.m3u8`;

  function restartPlayer(attempt = 0) {
    const player = players.get(cameraId);
    if (player) {
      if (player.stallWatchdog) clearInterval(player.stallWatchdog);
      if (player.recoveryTimer) clearTimeout(player.recoveryTimer);
      if (player.hls) player.hls.destroy();
    }
    const statusEl = document.getElementById(`status-${cameraId}`);
    if (!statusEl) return; // Camera removed from DOM

    statusEl.textContent = "Stream error - retrying...";
    statusEl.style.display = "";
    statusEl.classList.add("error");

    // Exponential backoff: 3s, 6s, 12s, 24s, capped at 30s.
    // After a source outage (e.g. router reboot), the server-side FFmpeg
    // process needs time to re-establish the stream before HLS.js can
    // connect. Checking /status before retrying avoids a rapid-fire error
    // loop that can silently break the recovery cycle.
    const delay = Math.min(3000 * Math.pow(2, attempt), 30000);
    const timer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/cameras/${cameraId}/status`);
        const status = await resp.json();
        if (status.ready) {
          const el = document.getElementById(`status-${cameraId}`);
          if (el) { el.style.display = "none"; el.classList.remove("error"); }
          startPlayer(cameraId, videoEl);
        } else {
          restartPlayer(Math.min(attempt + 1, 4));
        }
      } catch {
        restartPlayer(Math.min(attempt + 1, 4));
      }
    }, delay);

    players.set(cameraId, { recoveryTimer: timer });
  }

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
      // Treat fatal errors + common network death cases as reasons to recover.
      // This helps a lot when the entire backend container is restarted.
      const isFatal = data.fatal;
      const isNetworkDeath = data.type === Hls.ErrorTypes.NETWORK_ERROR ||
                             data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR ||
                             data.details === Hls.ErrorDetails.LEVEL_LOAD_ERROR;

      if (isFatal || isNetworkDeath) {
        restartPlayer();
      }
    });

    let lastTime = -1;
    let stallCount = 0;
    const stallWatchdog = setInterval(() => {
      if (videoEl.ended) return;
      if (videoEl.paused) {
        if (videoEl.readyState >= 2) {
          // Buffer has data — soft resume (display woke, energy saver released, etc.)
          videoEl.play().catch(() => {});
          lastTime = -1;
          stallCount = 0;
        } else {
          // Paused with drained buffer — HLS.js stopped fetching; restart after 15s
          if (++stallCount >= 3) restartPlayer();
        }
        return;
      }
      // Playing — verify time is advancing
      if (videoEl.readyState >= 2) {
        if (videoEl.currentTime === lastTime) {
          if (++stallCount >= 2) restartPlayer();
        } else {
          stallCount = 0;
          lastTime = videoEl.currentTime;
        }
      } else {
        // Playing but buffer drained — count as stall
        if (++stallCount >= 3) restartPlayer();
      }
    }, 5000);

    players.set(cameraId, { hls, stallWatchdog });
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
  if (player.stallWatchdog) clearInterval(player.stallWatchdog);
  if (player.recoveryTimer) clearTimeout(player.recoveryTimer);
  if (player.hls) player.hls.destroy();
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
  // Reduced from 3s → 45s. The previous aggressive polling caused very high
  // request volume (visible in production logs). External config edits are rare,
  // so a 45s check is sufficient for the "reload on external change" feature.
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
  }, 45000);
}

// --- Visibility Recovery ---
// Resume all paused players when the page regains visibility (e.g. display
// wakes from sleep or the tab is foregrounded after being background-throttled).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  for (const [cameraId, player] of players) {
    if (!player.hls) continue;
    const videoEl = document.getElementById(`video-${cameraId}`);
    if (videoEl && videoEl.paused && !videoEl.ended) {
      videoEl.play().catch(() => {});
    }
  }
});
