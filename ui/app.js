/* ClipMaker PC - UI logic. Talks to Python via pywebview js_api. */
let jobId = null;
let pollTimer = null;
let selectedMoments = [];
let lastMomentsSig = "";
let lastRendersSig = "";

const $ = (id) => document.getElementById(id);

function fmtTime(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

async function poll() {
  if (!jobId) return;
  const st = await window.pywebview.api.job_status(jobId);
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
    show($("videoInfo"));
    show($("step2"));
    $("importBtn").disabled = false;
  }
  if (st.status === "error") {
    hide($("importProgress"));
    $("importError").textContent = st.error;
    show($("importError"));
    $("importBtn").disabled = false;
  }
  // detect progress
  if (st.status === "working" && st.stage.startsWith("AI analyzing")) {
    show($("detectProgress"));
    $("detectBar").style.width = st.progress + "%";
    $("detectStage").textContent = st.stage;
  }
  if (st.detect_error) {
    hide($("detectProgress"));
    $("detectError").textContent = st.detect_error;
    show($("detectError"));
  }
  if (st.status === "moments") {
    hide($("detectProgress"));
    const sig = JSON.stringify(st.moments.map((m) =>
      [m.title, m.start, m.end, m.virality, !!m.caption_edited]));
    if (sig !== lastMomentsSig) { lastMomentsSig = sig; renderMoments(st.moments); }
  }
  // render progress
  if (st.status === "working" && st.stage.startsWith("Rendering")) {
    show($("step3"));
    show($("renderProgress"));
    $("renderBar").style.width = st.progress + "%";
    $("renderStage").textContent = st.stage;
  }
  if (st.render_error) {
    hide($("renderProgress"));
    $("renderError").textContent = st.render_error;
    show($("renderError"));
  }
  if (st.status === "done") {
    hide($("renderProgress"));
    const sig = JSON.stringify((st.renders || []).map((r) => r.path));
    if (sig !== lastRendersSig) { lastRendersSig = sig; renderRenders(st.renders); }
    show($("step3"));
  }
  if (st.status === "moments" && st.renders && st.renders.length) {
    const sig = JSON.stringify(st.renders.map((r) => r.path));
    if (sig !== lastRendersSig) { lastRendersSig = sig; renderRenders(st.renders); }
    show($("step3"));
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
    <div class="vbar"><div class="vfill ${cls}" style="width:${v}%"></div></div>
    <b>${v}</b></div>`;
}

function renderMoments(moments) {
  const box = $("moments");
  box.innerHTML = "";
  selectedMoments = moments.map((_, i) => i);
  moments.forEach((m, i) => {
    const div = document.createElement("div");
    div.className = "moment";
    const edited = m.caption_edited ? ' <span class="edited-tag">✏️ edited</span>' : "";
    div.innerHTML = `
      <input type="checkbox" data-i="${i}" checked>
      <div class="moment-body">
        <div class="moment-title">${escapeHtml(m.title)}${edited}</div>
        <div class="moment-meta">⏱ ${fmtTime(m.start)} → ${fmtTime(m.end)} · ${Math.round(m.end - m.start)}s</div>
        <div class="moment-hook">🪝 ${escapeHtml(m.hook)}</div>
        <div class="vbreak">
          ${bar("Hook", m.hook_score ?? m.virality)}
          ${bar("Flow", m.flow_score ?? m.virality)}
          ${bar("Value", m.value_score ?? m.virality)}
        </div>
        <div class="moment-reason">${escapeHtml(m.reason)}</div>
        <button class="ghost-btn small editCapBtn" data-i="${i}">✏️ Edit captions</button>
      </div>
      <div class="badge ${badgeClass(m.virality)}">🔥 ${m.virality}</div>`;
    box.appendChild(div);
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
  renders.forEach((r, i) => {
    const div = document.createElement("div");
    div.className = "render-item";
    div.innerHTML = `
      <input type="checkbox" class="merge-check" data-i="${i}" title="Tick to merge">
      <div class="render-body"><strong>${escapeHtml(r.title)}</strong>
        <span class="badge ${badgeClass(r.virality)}">🔥 ${r.virality}</span>
        <div class="path">${escapeHtml(r.path)}</div></div>
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
      const res = await window.pywebview.api.export_srt(
        jobId, parseInt(btn.dataset.i, 10));
      if (res.ok) {
        btn.textContent = "✓ SRT";
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
  const res = await window.pywebview.api.get_caption_words(jobId, i);
  if (!res.ok) { alert(res.error); return; }
  $("captionModalTitle").textContent = res.title || "";
  $("captionText").value = res.words || "";
  show($("captionModal"));
}

$("saveCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await window.pywebview.api.save_caption_words(
    jobId, editingIndex, $("captionText").value);
  if (res.ok) {
    $("captionMsg").textContent = `✓ Saved (${res.word_count} words). Re-render the clip to bake in corrections.`;
    poll(); // refresh moment list to show the edited tag
  } else {
    $("captionMsg").textContent = "⚠ " + (res.error || "Save failed.");
  }
});
$("resetCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await window.pywebview.api.reset_caption_words(jobId, editingIndex);
  if (res.ok) { hide($("captionModal")); poll(); }
});
$("closeCaptionBtn").addEventListener("click", () => hide($("captionModal")));

// -- merge ------------------------------------------------------------------
$("mergeBtn").addEventListener("click", async () => {
  if (mergeSelected.length < 2) return;
  $("mergeBtn").disabled = true;
  const res = await window.pywebview.api.merge_selected(jobId, mergeSelected);
  $("mergeBtn").disabled = false;
  if (res.ok) {
    mergeSelected = [];
    hide($("mergeBtn"));
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
  lastMomentsSig = ""; lastRendersSig = ""; mergeSelected = [];
  hide($("renderRow"));
  show($("importProgress"));
  $("importBar").style.width = "0%";
  $("importBtn").disabled = true;
  const res = await window.pywebview.api.import_url(url);
  jobId = res.job_id;
  startPoll();
});

$("detectBtn").addEventListener("click", async () => {
  hide($("detectError"));
  const res = await window.pywebview.api.detect(jobId, 8);
  if (!res.ok) {
    if (res.error === "no_api_key") openSettings();
    $("detectError").textContent = res.message || res.error;
    show($("detectError"));
    return;
  }
  show($("detectProgress"));
  startPoll();
});

$("renderSelBtn").addEventListener("click", async () => {
  if (!selectedMoments.length) return;
  hide($("renderError"));
  await window.pywebview.api.render(jobId, selectedMoments);
  show($("step3")); show($("renderProgress"));
  startPoll();
});
$("renderAllBtn").addEventListener("click", async () => {
  const boxes = document.querySelectorAll("#moments input[type=checkbox]");
  const all = [...boxes].map((cb) => parseInt(cb.dataset.i, 10));
  if (!all.length) return;
  hide($("renderError"));
  await window.pywebview.api.render(jobId, all);
  show($("step3")); show($("renderProgress"));
  startPoll();
});

$("openFolderBtn").addEventListener("click", async () => {
  await window.pywebview.api.open_folder(jobId);
});
$("newVideoBtn").addEventListener("click", () => {
  stopPoll(); jobId = null;
  lastMomentsSig = ""; lastRendersSig = ""; mergeSelected = [];
  $("urlInput").value = "";
  ["step2", "step3"].forEach((s) => hide($(s)));
  hide($("videoInfo")); hide($("importProgress")); hide($("importError"));
  $("importBtn").disabled = false;
});

// -- settings ---------------------------------------------------------------
function openSettings() { show($("settingsModal")); }
$("settingsBtn").addEventListener("click", async () => {
  const s = await window.pywebview.api.get_settings();
  $("apiKeyInput").value = s.api_key || "";
  $("clipLenSelect").value = s.clip_length || "30-60s";
  $("captionStyleSelect").value = s.caption_style || "bold-pop";
  $("faceTrackCheck").checked = s.face_track !== false;
  $("fillerCheck").checked = !!s.remove_fillers;
  openSettings();
});
$("closeSettingsBtn").addEventListener("click", () => hide($("settingsModal")));
$("saveSettingsBtn").addEventListener("click", async () => {
  await window.pywebview.api.save_settings({
    api_key: $("apiKeyInput").value.trim(),
    clip_length: $("clipLenSelect").value,
    caption_style: $("captionStyleSelect").value,
    face_track: $("faceTrackCheck").checked,
    remove_fillers: $("fillerCheck").checked,
  });
  hide($("settingsModal"));
});
$("keyHelp").addEventListener("click", (e) => {
  e.preventDefault();
  window.pywebview.api.open_folder(null); // noop keeps pywebview happy
  alert("Get a free key at:\nhttps://aistudio.google.com/app/apikey\n\nPaste it here and press Save.");
});
