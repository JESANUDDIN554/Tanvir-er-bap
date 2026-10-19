/* ClipMaker PC - UI logic. Talks to the Python backend over HTTP
   (localhost server); `api.<method>(...args)` POSTs to /api/<method>. */
let jobId = null;
let pollTimer = null;
let selectedMoments = [];
let lastMomentsSig = "";
let lastRendersSig = "";
let detectModeReady = false;

const $ = (id) => document.getElementById(id);

/* HTTP bridge to the Python backend (replaces the old pywebview js_api). */
const api = new Proxy({}, {
  get: (_t, method) => async (...args) => {
    const r = await fetch("/api/" + method, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
    });
    return r.json();
  },
});

function fmtTime(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

/* entrance animation, only when the element was hidden */
function reveal(el) {
  const wasHidden = el.classList.contains("hidden");
  el.classList.remove("hidden");
  if (wasHidden && el.classList.contains("step-card")) {
    el.classList.remove("pop");
    void el.offsetWidth;
    el.classList.add("pop");
  }
}

/* step indicator: 1 import, 2 moments, 3 clips */
function setStep(n) {
  document.querySelectorAll(".step-dot").forEach((d) => {
    const s = parseInt(d.dataset.step, 10);
    d.classList.toggle("active", s === n);
    d.classList.toggle("done", s < n);
  });
}

/* toast notifications */
function toast(msg, isErr) {
  const box = $("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add("in"));
  setTimeout(() => { t.classList.remove("in"); setTimeout(() => t.remove(), 350); }, 2600);
}

/* animated virality count-up */
function countUp(el, target) {
  const t0 = performance.now(), dur = 750;
  function frame(t) {
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

async function poll() {
  if (!jobId) return;
  let st;
  try {
    st = await api.job_status(jobId);
  } catch (e) { return; }
  if (st.status === "missing") return;

  // import progress
  if (!$("importProgress").classList.contains("hidden")) {
    $("importBar").style.width = st.progress + "%";
    $("importStage").textContent = st.stage;
  }
  if (st.status === "transcribed" || st.status === "moments" || st.status === "done") {
    hide($("importProgress"));
    $("videoTitle").textContent = st.title || "Video";
    $("videoMeta").textContent =
      `${fmtTime(st.duration)} · ${st.word_count} words transcribed`;
    reveal($("videoInfo"));
    reveal($("step2"));
    setStep(2);
    $("importBtn").disabled = false;
    if (!detectModeReady) { detectModeReady = true; initDetectMode(); }
  }
  if (st.status === "error") {
    hide($("importProgress"));
    $("importError").textContent = st.error;
    show($("importError"));
    $("importBtn").disabled = false;
    toast("Import failed", true);
  }
  // detect progress (AI or offline)
  if (st.status === "working" && (st.stage.startsWith("AI analyzing")
      || st.stage.startsWith("Scanning transcript"))) {
    show($("detectProgress"));
    hide($("detectError"));
    $("detectBar").style.width = st.progress + "%";
    $("detectStage").textContent = st.stage;
  }
  if (st.detect_error) {
    hide($("detectProgress"));
    hide($("detectSkeleton"));
    $("detectError").textContent = st.detect_error;
    show($("detectError"));
  }
  if (st.status === "moments") {
    hide($("detectProgress"));
    hide($("detectSkeleton"));
    const sig = JSON.stringify(st.moments.map((m) =>
      [m.title, m.start, m.end, m.virality, !!m.caption_edited]));
    if (sig !== lastMomentsSig) { lastMomentsSig = sig; renderMoments(st.moments); }
  }
  // render progress
  if (st.status === "working" && st.stage.startsWith("Rendering")) {
    reveal($("step3"));
    setStep(3);
    show($("renderProgress"));
    $("renderBar").style.width = st.progress + "%";
    $("renderStage").textContent = st.stage;
  }
  if (st.render_error) {
    hide($("renderProgress"));
    $("renderError").textContent = st.render_error;
    show($("renderError"));
    toast("Render failed", true);
  }
  if (st.status === "done") {
    hide($("renderProgress"));
    const sig = JSON.stringify((st.renders || []).map((r) => r.path));
    if (sig !== lastRendersSig) {
      lastRendersSig = sig;
      renderRenders(st.renders);
      if (st.renders && st.renders.length) toast(`Done — ${st.renders.length} clip${st.renders.length > 1 ? "s" : ""} rendered`);
    }
    reveal($("step3"));
    setStep(3);
  }
  if (st.status === "moments" && st.renders && st.renders.length) {
    const sig = JSON.stringify(st.renders.map((r) => r.path));
    if (sig !== lastRendersSig) { lastRendersSig = sig; renderRenders(st.renders); }
    reveal($("step3"));
  }
}

function startPoll() {
  stopPoll();
  pollTimer = setInterval(poll, 800);
}
function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function badgeClass(v) {
  return v >= 80 ? "" : v >= 60 ? "mid" : "low";
}

function bar(label, v) {
  const cls = v >= 80 ? "" : v >= 60 ? "mid" : "low";
  return `<div class="vrow"><span>${label}</span>
    <div class="vbar"><div class="vfill ${cls}" data-w="${v}" style="width:0%"></div></div>
    <b>${v}</b></div>`;
}

function renderMoments(moments) {
  const box = $("moments");
  box.innerHTML = "";
  hide($("momentsEmpty"));
  selectedMoments = moments.map((_, i) => i);
  moments.forEach((m, i) => {
    const div = document.createElement("div");
    div.className = "moment";
    div.style.animationDelay = Math.min(i * 70, 420) + "ms";
    const edited = m.caption_edited ? ' <span class="edited-tag">edited</span>' : "";
    const srcTag = m.offline ? ' <span class="edited-tag">offline</span>' : "";
    div.innerHTML = `
      <input type="checkbox" data-i="${i}" checked aria-label="Select clip">
      <div class="moment-body">
        <div class="moment-title">${escapeHtml(m.title)}${edited}${srcTag}</div>
        <div class="moment-meta">${fmtTime(m.start)} → ${fmtTime(m.end)} · ${Math.round(m.end - m.start)}s</div>
        <div class="moment-hook">${escapeHtml(m.hook)}</div>
        <div class="vbreak">
          ${bar("Hook", m.hook_score ?? m.virality)}
          ${bar("Flow", m.flow_score ?? m.virality)}
          ${bar("Value", m.value_score ?? m.virality)}
        </div>
        <div class="moment-reason">${escapeHtml(m.reason)}</div>
        <button class="ghost-btn small editCapBtn" data-i="${i}">Edit captions</button>
      </div>
      <div class="vbadge ${badgeClass(m.virality)}">
        <span class="vscore" data-v="${m.virality}">0</span>
        <span class="vlabel">virality</span>
      </div>`;
    box.appendChild(div);
  });
  // animate bars + score count-ups
  requestAnimationFrame(() => {
    box.querySelectorAll(".vfill").forEach((f) => { f.style.width = f.dataset.w + "%"; });
    box.querySelectorAll(".vscore").forEach((s) => countUp(s, parseInt(s.dataset.v, 10)));
  });
  box.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const i = parseInt(cb.dataset.i, 10);
      selectedMoments = selectedMoments.filter((x) => x !== i);
      if (cb.checked) selectedMoments.push(i);
    });
  });
  box.querySelectorAll(".editCapBtn").forEach((btn) => {
    btn.addEventListener("click", () => openCaptionEditor(parseInt(btn.dataset.i, 10)));
  });
  show($("renderRow"));
}

let mergeSelected = [];

function renderRenders(renders) {
  const box = $("renders");
  box.innerHTML = "";
  mergeSelected = [];
  if (renders && renders.length) hide($("rendersEmpty"));
  (renders || []).forEach((r, i) => {
    const div = document.createElement("div");
    div.className = "render-item";
    div.style.animationDelay = Math.min(i * 60, 360) + "ms";
    div.innerHTML = `
      <input type="checkbox" class="merge-check" data-i="${i}" title="Tick to merge">
      <div class="render-body">
        <div class="render-title">${escapeHtml(r.title)}
          <span class="mini-badge ${badgeClass(r.virality)}">${r.virality}</span></div>
        <div class="path">${escapeHtml(r.path)}</div>
      </div>
      <button class="ghost-btn small srtBtn" data-i="${i}">SRT</button>`;
    box.appendChild(div);
  });
  box.querySelectorAll(".merge-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      const i = parseInt(cb.dataset.i, 10);
      mergeSelected = mergeSelected.filter((x) => x !== i);
      if (cb.checked) mergeSelected.push(i);
      mergeSelected.sort((a, b) => a - b);
      if (mergeSelected.length >= 2) show($("mergeBtn"));
      else hide($("mergeBtn"));
    });
  });
  box.querySelectorAll(".srtBtn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await api.export_srt(
        jobId, parseInt(btn.dataset.i, 10));
      if (res.ok) {
        btn.textContent = "✓ SRT";
        toast("SRT captions saved");
        setTimeout(() => { btn.textContent = "SRT"; }, 2000);
      } else {
        alert(res.error || "SRT export failed.");
      }
    });
  });
  hide($("mergeBtn"));
}

// -- caption editor ---------------------------------------------------------
let editingIndex = null;

async function openCaptionEditor(i) {
  editingIndex = i;
  $("captionMsg").textContent = "";
  const res = await api.get_caption_words(jobId, i);
  if (!res.ok) { alert(res.error); return; }
  $("captionModalTitle").textContent = res.title || "";
  $("captionText").value = res.words || "";
  show($("captionModal"));
}

$("saveCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await api.save_caption_words(
    jobId, editingIndex, $("captionText").value);
  if (res.ok) {
    $("captionMsg").textContent = `✓ Saved (${res.word_count} words). Re-render the clip to bake in corrections.`;
    toast("Captions saved");
    poll(); // refresh moment list to show the edited tag
  } else {
    $("captionMsg").textContent = "⚠ " + (res.error || "Save failed.");
  }
});
$("resetCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await api.reset_caption_words(jobId, editingIndex);
  if (res.ok) { hide($("captionModal")); toast("Captions reset"); poll(); }
});
$("closeCaptionBtn").addEventListener("click", () => hide($("captionModal")));

// -- merge ------------------------------------------------------------------
$("mergeBtn").addEventListener("click", async () => {
  if (mergeSelected.length < 2) return;
  $("mergeBtn").disabled = true;
  const res = await api.merge_selected(jobId, mergeSelected);
  $("mergeBtn").disabled = false;
  if (res.ok) {
    mergeSelected = [];
    hide($("mergeBtn"));
    toast("Clips merged");
    poll();
  } else {
    alert(res.error || "Merge failed.");
  }
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// -- events ---------------------------------------------------------------
$("importBtn").addEventListener("click", async () => {
  const url = $("urlInput").value.trim();
  if (!url) return;
  hide($("importError")); hide($("videoInfo"));
  hide($("step2")); hide($("step3"));
  $("moments").innerHTML = ""; $("renders").innerHTML = "";
  show($("momentsEmpty")); show($("rendersEmpty"));
  lastMomentsSig = ""; lastRendersSig = ""; mergeSelected = [];
  detectModeReady = false;
  hide($("renderRow"));
  show($("importProgress"));
  $("importBar").style.width = "0%";
  $("importBtn").disabled = true;
  setStep(1);
  const res = await api.import_url(url);
  jobId = res.job_id;
  startPoll();
});

// -- detection mode (AI vs offline) ------------------------------------------
function currentDetectMode() {
  const on = document.querySelector("#detectModeSeg .seg.on");
  return on ? on.dataset.value : "ai";
}
function syncDetectModeSub(mode) {
  const sub = $("detectModeSub");
  if (!sub) return;
  sub.textContent = mode === "offline"
    ? "Offline mode scans the transcript on your PC — no key, no internet. Scores are heuristic (energy + speech density), not AI."
    : "AI mode needs your free Gemini API key (Settings). Every clip is scored 0–100 for virality.";
}
async function initDetectMode() {
  // default to Offline when no API key is saved
  let mode = "offline";
  try {
    const s = await api.get_settings();
    if (s.api_key && String(s.api_key).trim()) mode = "ai";
  } catch (e) { /* default offline */ }
  document.querySelectorAll("#detectModeSeg .seg").forEach((p) => {
    p.classList.toggle("on", p.dataset.value === mode);
  });
  syncDetectModeSub(mode);
}
document.querySelectorAll("#detectModeSeg .seg").forEach((p) => {
  p.addEventListener("click", () => {
    document.querySelectorAll("#detectModeSeg .seg").forEach((x) =>
      x.classList.remove("on"));
    p.classList.add("on");
    syncDetectModeSub(p.dataset.value);
  });
});

$("detectBtn").addEventListener("click", async () => {
  hide($("detectError"));
  const mode = currentDetectMode();
  const res = mode === "offline"
    ? await api.detect_offline(jobId, 8)
    : await api.detect(jobId, 8);
  if (!res.ok) {
    if (res.error === "no_api_key") openSettings();
    $("detectError").textContent = res.message || res.error;
    show($("detectError"));
    return;
  }
  show($("detectProgress"));
  show($("detectSkeleton"));
  startPoll();
});

$("renderSelBtn").addEventListener("click", async () => {
  if (!selectedMoments.length) return;
  hide($("renderError"));
  await api.render(jobId, selectedMoments);
  show($("step3")); show($("renderProgress"));
  startPoll();
});
$("renderAllBtn").addEventListener("click", async () => {
  const boxes = document.querySelectorAll("#moments input[type=checkbox]");
  const all = [...boxes].map((cb) => parseInt(cb.dataset.i, 10));
  if (!all.length) return;
  hide($("renderError"));
  await api.render(jobId, all);
  show($("step3")); show($("renderProgress"));
  startPoll();
});

$("openFolderBtn").addEventListener("click", async () => {
  await api.open_folder(jobId);
});
$("newVideoBtn").addEventListener("click", () => {
  stopPoll(); jobId = null;
  lastMomentsSig = ""; lastRendersSig = ""; mergeSelected = [];
  detectModeReady = false;
  $("urlInput").value = "";
  ["step2", "step3"].forEach((s) => hide($(s)));
  hide($("videoInfo")); hide($("importProgress")); hide($("importError"));
  show($("momentsEmpty")); show($("rendersEmpty"));
  $("importBtn").disabled = false;
  setStep(1);
});

// -- settings ---------------------------------------------------------------
function syncStyleCards() {
  const v = $("captionStyleSelect").value;
  document.querySelectorAll("#styleCards .style-card").forEach((c) => {
    c.classList.toggle("on", c.dataset.value === v);
  });
}
function syncLenPills() {
  const v = $("clipLenSelect").value;
  document.querySelectorAll("#clipLenPills .seg").forEach((p) => {
    p.classList.toggle("on", p.dataset.value === v);
  });
}
document.querySelectorAll("#styleCards .style-card").forEach((c) => {
  c.addEventListener("click", () => {
    $("captionStyleSelect").value = c.dataset.value;
    syncStyleCards();
  });
});
document.querySelectorAll("#clipLenPills .seg").forEach((p) => {
  p.addEventListener("click", () => {
    $("clipLenSelect").value = p.dataset.value;
    syncLenPills();
  });
});
$("keyToggle").addEventListener("click", () => {
  const inp = $("apiKeyInput");
  const showing = inp.type === "text";
  inp.type = showing ? "password" : "text";
  $("keyToggle").textContent = showing ? "Show" : "Hide";
});

function openSettings() { show($("settingsModal")); }
$("settingsBtn").addEventListener("click", async () => {
  const s = await api.get_settings();
  $("apiKeyInput").value = s.api_key || "";
  $("apiKeyInput").type = "password";
  $("keyToggle").textContent = "Show";
  $("clipLenSelect").value = s.clip_length || "30-60s";
  $("captionStyleSelect").value = s.caption_style || "bold-pop";
  syncLenPills();
  syncStyleCards();
  $("faceTrackCheck").checked = s.face_track !== false;
  $("fillerCheck").checked = !!s.remove_fillers;
  openSettings();
});
$("closeSettingsBtn").addEventListener("click", () => hide($("settingsModal")));
$("saveSettingsBtn").addEventListener("click", async () => {
  await api.save_settings({
    api_key: $("apiKeyInput").value.trim(),
    clip_length: $("clipLenSelect").value,
    caption_style: $("captionStyleSelect").value,
    face_track: $("faceTrackCheck").checked,
    remove_fillers: $("fillerCheck").checked,
  });
  hide($("settingsModal"));
  toast("Settings saved");
});
$("keyHelp").addEventListener("click", (e) => {
  e.preventDefault();
  alert("Get a free key at:\nhttps://aistudio.google.com/app/apikey\n\nPaste it here and press Save.");
});

// -- splash -----------------------------------------------------------------
window.addEventListener("DOMContentLoaded", () => {
  setStep(1);
  setTimeout(() => {
    const sp = document.getElementById("splash");
    if (sp) {
      sp.classList.add("gone");
      setTimeout(() => sp.remove(), 600);
    }
  }, 900);
});
