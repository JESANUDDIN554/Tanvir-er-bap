/* Itraji Cliper - full Clip Maker web-app parity UI.
   Talks to the Python backend over HTTP (localhost server). */
let jobId = null;
let pollTimer = null;
let batchTimer = null;
let batchId = null;
let selectedMoments = [];
let lastMoments = [];
let lastMomentsSig = "";
let lastRendersSig = "";
let lastErrSig = "";
let lastQueueSig = "";
let controlsInitFor = null;
let thumbLoadedFor = null;
let prompts = [];               // {q, state: pending|matched|unmatched}
let sortDesc = true;
let viewMode = "grid";
let settings = {};
let mergeSelected = [];
let lastToast = { msg: "", t: 0 };
let captionStyle = "bold-pop";
let clipLen = "30-60";
let ratio = "9:16";
let quality = "1080p";
let encodeTier = "maximum";
let clipCount = 8;
let genre = "";
/* navigation history state (Back button / logo home) */
let navStack = [];
let navNow = { view: "hub" };
let navApplying = false;
let lastWizardStep = 1;
let maxStepReached = 1;
/* Studio hub + tool views (hub is home; tools are overlay views like
   Library / Caption Remover) */
let hubOpen = true;
let toolOpen = null;
const TOOL_VIEWS = {
  generator: "generatorView", titles: "titlesView", script: "scriptView",
  transcript: "transcriptView", enhancer: "enhancerView",
};

const $ = (id) => document.getElementById(id);

/* HTTP bridge to the Python backend. */
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
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function reveal(el) {
  const wasHidden = el.classList.contains("hidden");
  el.classList.remove("hidden");
  if (wasHidden && el.classList.contains("step-card")) {
    el.classList.remove("pop");
    void el.offsetWidth;
    el.classList.add("pop");
  }
}

function setStep(n) {
  document.querySelectorAll(".step-dot").forEach((d) => {
    const s = parseInt(d.dataset.step, 10);
    d.classList.toggle("active", s === n);
    d.classList.toggle("done", s < n);
  });
  // navigation tracking: wizard step changes are history entries
  if (!libraryOpen && !removerOpen && !hubOpen && !toolOpen) {
    lastWizardStep = n;
    if (n > maxStepReached) maxStepReached = n;
    noteNav({ view: "wizard", step: n });
  }
}

/* toast notifications (deduped: same message within 4s shows once) */
function toast(msg, isErr) {
  const now = Date.now();
  if (msg === lastToast.msg && now - lastToast.t < 4000) return;
  lastToast = { msg, t: now };
  const box = $("toasts");
  if (!box) return;
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add("in"));
  setTimeout(() => { t.classList.remove("in"); setTimeout(() => t.remove(), 350); }, 2600);
}

function countUp(el, target) {
  const t0 = performance.now(), dur = 750;
  function frame(t) {
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function downloadUrl(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = decodeURIComponent(url.split("/").pop());
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ---------------- poll ---------------- */
async function poll() {
  if (!jobId) return;
  let st;
  try { st = await api.job_status(jobId); }
  catch (e) { return; }
  if (st.status === "missing") return;

  if (!$("importProgress").classList.contains("hidden")) {
    $("importBar").style.width = st.progress + "%";
    $("importStage").textContent = st.stage;
  }
  if (st.status === "transcribed" || st.status === "moments" || st.status === "done") {
    hide($("importProgress"));
    hide($("importError"));
    lastErrSig = "";
    $("videoTitle").textContent = st.title || "Video";
    $("videoMeta").textContent =
      `${fmtTime(st.duration)} · ${st.word_count} words transcribed`;
    reveal($("videoInfo"));
    if (st.long_video) show($("longVideoWarn")); else hide($("longVideoWarn"));
    if (thumbLoadedFor !== jobId) {
      thumbLoadedFor = jobId;
      api.get_thumbnail(jobId).then((res) => {
        if (res.ok && res.url) {
          $("videoThumb").src = res.url;
          show($("videoThumb"));
          hide($("videoThumbFallback"));
        }
      });
    }
    reveal($("step2"));
    setStep(2);
    $("importBtn").disabled = false;
    $("localFileBtn").disabled = false;
    if (controlsInitFor !== jobId) { controlsInitFor = jobId; initControls(); }
  }
  if (st.status === "error") {
    hide($("importProgress"));
    $("importError").textContent = st.error;
    show($("importError"));
    $("importBtn").disabled = false;
    $("localFileBtn").disabled = false;
    const sig = "err:" + st.error;          // toast once per distinct error
    if (sig !== lastErrSig) { lastErrSig = sig; toast("Import failed", true); }
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
    hide($("detectError"));   // clear any stale error from a previous failed run
    const sig = JSON.stringify(st.moments.map((m) =>
      [m.title, m.start, m.end, m.virality, !!m.caption_edited, m.matched || ""]));
    if (sig !== lastMomentsSig) {
      lastMomentsSig = sig;
      lastMoments = st.moments;
      renderMoments(st.moments);
    }
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
    hide($("renderError"));   // clear any stale error from a previous failed run
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
  updateLivePanel(st);
  refreshQueue();
}

/* ---------------- Live Analysis panel ----------------
   Fed entirely by the real job status: the phase chip maps the
   job's actual stage text, the badge maps its status, the console
   renders the backend per-job event log incrementally, and the
   rail mirrors renders as they land in job state. */
let liveLogJobId = null, liveLogRendered = 0, liveLogFirstT = -1;
let liveThumbJob = null, lastRailSig = "";

function fmtClock(epochSec) {
  const d = new Date(epochSec * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function livePhaseFor(st) {
  if (st.status === "error") return "ERROR";
  if (st.status === "done") return "COMPLETE";
  const s = (st.stage || "").toLowerCase();
  if (s.includes("render")) return "RENDERING CLIPS";
  if (s.includes("download")) return "DOWNLOADING";
  if (s.includes("scanning transcript") || s.includes("analyz")
      || s.includes("built-in ai") || s.includes("moment"))
    return "VIRAL DETECTION: ACTIVE";
  if (s.includes("transcri") || s.includes("whisper") || s.includes("audio")
      || s.includes("speech"))
    return "SCANNING CONTENT";
  return "SCANNING CONTENT";
}

function renderLiveLog(log) {
  const box = $("liveLog");
  if (liveLogJobId !== jobId) {
    liveLogJobId = jobId; liveLogRendered = 0; liveLogFirstT = -1;
    box.innerHTML = "";
  }
  const firstT = log.length ? log[0].t : -1;
  if (log.length < liveLogRendered || firstT !== liveLogFirstT) {
    box.innerHTML = ""; liveLogRendered = 0;  // cap dropped old lines
  }
  liveLogFirstT = firstT;
  for (let i = liveLogRendered; i < log.length; i++) {
    const e = log[i];
    const div = document.createElement("div");
    if (String(e.text).startsWith("ERROR")) div.className = "err";
    div.innerHTML = `<span class="lt">[${fmtClock(e.t)}]</span>`
      + escapeHtml(e.text);
    box.appendChild(div);
  }
  liveLogRendered = log.length;
  box.scrollTop = box.scrollHeight;
}

function renderLiveRail(st) {
  const renders = st.renders || [];
  if (st.status === "done" && renders.length) show($("liveGoClips"));
  else hide($("liveGoClips"));
  const sig = JSON.stringify(renders.map((r) => r.path));
  if (sig === lastRailSig) return;
  lastRailSig = sig;
  const list = $("liveRailList");
  list.innerHTML = "";
  if (renders.length) hide($("liveRailEmpty")); else show($("liveRailEmpty"));
  const thumb = thumbLoadedFor === jobId
    ? $("videoThumb").getAttribute("src") : null;
  renders.forEach((r) => {
    const div = document.createElement("div");
    div.className = "rail-clip";
    div._render = r;
    let dur = "";
    if (r.merged_duration) dur = fmtTime(r.merged_duration);
    else {
      const m = (lastMoments || [])[r.moment_index];
      if (m) dur = fmtTime(Math.max(0, Math.round(m.end - m.start)));
    }
    div.innerHTML = `${thumb ? `<img src="${thumb}" alt="">` : ""}
      <div class="rc-text">
        <div class="rc-title">${escapeHtml(r.title || "Clip")}</div>
        <div class="rc-meta">${dur ? dur + " · " : ""}virality ${r.virality ?? "—"}</div>
      </div>
      <button class="ghost-btn small rc-dl" type="button">Download</button>
      <button class="ghost-btn small rc-cap" type="button">Captions</button>`;
    div.querySelector(".rc-dl").addEventListener("click", () => {
      if (r.url) downloadUrl(r.url);
    });
    div.querySelector(".rc-cap").addEventListener("click", () => {
      openCaptionStudio(r);
    });
    list.appendChild(div);
  });
}

function updateLivePanel(st) {
  const panel = $("livePanel");
  const active = st.status === "working" || st.status === "error";
  if (!active) { hide(panel); panel.classList.remove("live"); return; }
  show(panel);
  panel.classList.toggle("live", st.status === "working");
  const badge = st.status === "error" ? "ERROR" : "PROCESSING";
  const bEl = $("liveBadge");
  bEl.textContent = badge;
  bEl.className = "live-badge " + badge.toLowerCase();
  $("livePhaseChip").textContent = livePhaseFor(st);
  const chip = $("liveStateChip");
  chip.textContent = badge;
  chip.className = "live-chip right " + badge.toLowerCase();
  if (liveThumbJob !== jobId) {          // new job: clear stale thumb
    liveThumbJob = jobId;
    $("liveThumb").removeAttribute("src");
    hide($("liveThumb")); show($("liveThumbFallback"));
  }
  const vsrc = thumbLoadedFor === jobId
    ? $("videoThumb").getAttribute("src") : null;
  if (vsrc && $("liveThumb").getAttribute("src") !== vsrc) {
    $("liveThumb").src = vsrc;
    show($("liveThumb")); hide($("liveThumbFallback"));
  }
  $("liveLine1").innerHTML = "&gt; AUDIO_TRANSCRIPT: <b>"
    + (st.word_count ? st.word_count + " WORDS READY"
       : st.status === "error" ? "ERROR" : "PROCESSING") + "</b>";
  $("liveLine2").innerHTML = "&gt; ANALYSIS: <b>"
    + escapeHtml(st.stage || "—") + "</b>";
  renderLiveLog(st.log || []);
  renderLiveRail(st);
}

$("liveGoClips").addEventListener("click", () => {
  reveal($("step3"));
  setStep(3);
  $("step3").scrollIntoView({ behavior: "smooth", block: "start" });
});

function startPoll() { stopPoll(); pollTimer = setInterval(poll, 800); }
function stopPoll() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

/* ---------------- job queue ---------------- */
async function refreshQueue() {
  let res;
  try { res = await api.list_jobs(); } catch (e) { return; }
  const jobs = res.jobs || [];
  const sig = JSON.stringify(jobs.map((j) => [j.job_id, j.status, j.title]));
  if (sig === lastQueueSig) return;
  lastQueueSig = sig;
  const wrap = $("jobQueueWrap"), box = $("jobQueue");
  if (!jobs.length) { hide(wrap); return; }
  show(wrap);
  box.innerHTML = "";
  jobs.forEach((j) => {
    const div = document.createElement("div");
    div.className = "job-item" + (j.job_id === jobId ? " on" : "");
    div.innerHTML = `<span class="job-dot ${j.status}"></span>
      <span class="job-name">${escapeHtml(j.title || "Video")}</span>
      <span class="job-stage">${escapeHtml(j.stage || j.status)}</span>`;
    div.addEventListener("click", () => selectJob(j.job_id));
    box.appendChild(div);
  });
}

function selectJob(id) {
  stopPoll();
  maxStepReached = 1;  // step dots re-unlock as this job's state polls in
  lastWizardStep = 1;
  jobId = id;
  lastMomentsSig = ""; lastRendersSig = ""; lastErrSig = "";
  mergeSelected = []; prompts = [];
  controlsInitFor = null; thumbLoadedFor = null;
  $("moments").innerHTML = ""; $("renders").innerHTML = "";
  show($("momentsEmpty")); show($("rendersEmpty"));
  hide($("renderRow")); hide($("mergeBtn")); hide($("momentsBar"));
  hide($("detectError")); hide($("renderError"));
  renderPrompts();
  startPoll();
  poll();
  toast("Switched video");
}

function resetForNewImport() {
  stopPoll();
  lastMomentsSig = ""; lastRendersSig = ""; lastErrSig = "";
  lastMoments = []; mergeSelected = []; prompts = [];
  controlsInitFor = null; thumbLoadedFor = null;
  detectModeReadyReset();
  hide($("importError")); hide($("detectError")); hide($("renderError"));
  hide($("videoInfo")); hide($("longVideoWarn"));
  hide($("videoThumb")); show($("videoThumbFallback"));
  hide($("step2")); hide($("step3"));
  $("moments").innerHTML = ""; $("renders").innerHTML = "";
  show($("momentsEmpty")); show($("rendersEmpty"));
  hide($("renderRow")); hide($("mergeBtn")); hide($("momentsBar"));
  renderPrompts();
  show($("importProgress"));
  $("importBar").style.width = "0%";
  $("importBtn").disabled = true;
  $("localFileBtn").disabled = true;
  setStep(1);
}
function detectModeReadyReset() { /* controls re-init on next transcribed poll */ }

/* ---------------- step 1 events ---------------- */
$("importBtn").addEventListener("click", async () => {
  const url = $("urlInput").value.trim();
  if (!url) return;
  resetForNewImport();
  try {
    const res = await api.import_url(url, $("cookiesCheck").checked, $("langSelect").value);
    jobId = res.job_id;
    startPoll();
  } catch (e) {
    hide($("importProgress"));
    $("importError").textContent = "Couldn't reach the local backend — is the app running?";
    show($("importError"));
    $("importBtn").disabled = false;
    $("localFileBtn").disabled = false;
  }
});

$("localFileBtn").addEventListener("click", async () => {
  const p = $("localPathInput").value.trim();
  if (!p) { toast("Paste a local video file path first", true); return; }
  resetForNewImport();
  const res = await api.import_file(p, $("langSelect").value);
  if (!res.ok) {
    hide($("importProgress"));
    $("importError").textContent = res.error;
    show($("importError"));
    $("importBtn").disabled = false;
    $("localFileBtn").disabled = false;
    toast("Import failed", true);
    return;
  }
  jobId = res.job_id;
  startPoll();
});

$("batchBtn").addEventListener("click", async () => {
  const urls = $("batchUrls").value.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 10);
  if (!urls.length) { toast("Paste at least one URL", true); return; }
  $("batchBtn").disabled = true;
  const res = await api.import_batch(urls, $("cookiesCheck").checked, $("langSelect").value);
  if (!res.ok) {
    $("batchBtn").disabled = false;
    toast(res.error || "Batch failed", true);
    return;
  }
  batchId = res.batch_id;
  clearInterval(batchTimer);
  batchTimer = setInterval(async () => {
    const b = await api.batch_status(batchId);
    if (b.status === "missing") { clearInterval(batchTimer); return; }
    $("batchStatus").textContent = `Imported ${b.done}/${b.total}…`;
    refreshQueue();
    if (b.status === "done") {
      clearInterval(batchTimer);
      $("batchBtn").disabled = false;
      $("batchStatus").textContent = `Done — ${b.total} videos imported. Click one above.`;
      toast(`Batch done — ${b.total} videos`);
      if (b.job_ids && b.job_ids.length && !jobId) selectJob(b.job_ids[0]);
    }
  }, 1500);
});

$("srtBtn").addEventListener("click", async () => {
  const p = $("srtPathInput").value.trim();
  if (!jobId) { toast("Import a video first", true); return; }
  if (!p) { toast("Paste an .srt file path first", true); return; }
  const res = await api.import_srt_transcript(jobId, p);
  if (res.ok) {
    toast(`Transcript replaced (${res.word_count} words)`);
    lastMomentsSig = ""; lastMoments = [];
    $("moments").innerHTML = "";
    show($("momentsEmpty"));
    poll();
  } else {
    toast(res.error || "SRT failed", true);
  }
});

/* ---------------- step 2: controls ---------------- */
function segSet(id, value) {
  document.querySelectorAll(`#${id} .seg`).forEach((b) =>
    b.classList.toggle("on", b.dataset.value === String(value)));
}
function segGet(id) {
  const on = document.querySelector(`#${id} .seg.on`);
  return on ? on.dataset.value : null;
}

/* Detect modes are "local" (Built-in AI) and "offline". Any older
   saved value (e.g. the removed cloud mode) lands on "local". */
function migrateDetectMode(m) {
  return m === "offline" ? "offline" : "local";
}

function initControls() {
  // reflect saved settings into every control
  segSet("detectModeSeg", migrateDetectMode(settings.detect_mode));
  syncDetectModeSub(segGet("detectModeSeg"));
  segSet("clipCountSeg", String(settings.clip_count || 8));
  clipCount = parseInt(segGet("clipCountSeg"), 10) || 8;
  ratio = settings.ratio || "9:16";
  document.querySelectorAll("#ratioPicker .ratio-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === ratio));
  const savedLen = String(settings.clip_length || "");
  if (savedLen.startsWith("custom:")) {
    clipLen = "30-60"; // pill fallback if the custom box gets cleared
    $("customLenInput").value = savedLen.slice("custom:".length);
    document.querySelectorAll("#clipLenPills .seg").forEach((p) => p.classList.remove("on"));
  } else {
    clipLen = migrateClipLen(savedLen); // legacy "60s" -> a range
    segSet("clipLenPills", clipLen);
    $("customLenInput").value = "";
  }
  captionStyle = settings.caption_style || "bold-pop";
  syncStyleCards();
  renderCustomStyles();
  genre = settings.genre || "";
  document.querySelectorAll("#genreGrid .genre-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === genre));
  $("emojisCheck").checked = !!settings.emojis;
  $("highlightCheck").checked = !!settings.highlight;
  $("silencesCheck").checked = !!settings.remove_silences;
  $("fillerCheck").checked = !!settings.remove_fillers;
  $("censorCheck").checked = !!settings.auto_censor;
  $("faceTrackCheck").checked = settings.face_track !== false;
  $("speakerColorsCheck").checked = !!settings.speaker_colors;
  $("hookOverlayCheck").checked = settings.hook_overlay !== false;
  $("ctaOverlayCheck").checked = settings.cta_overlay !== false;
  $("punchZoomCheck").checked = settings.punch_zoom !== false;
  $("enhanceCheck").checked = !!settings.enhance_clarity;
  $("safeZoneCheck").checked = !!settings.safe_zone;
  $("speakerColor1").value = settings.speaker_color_1 || "#ffd166";
  $("speakerColor2").value = settings.speaker_color_2 || "#6cc7ff";
  $("ctaTextInput").value = settings.cta_text || "Follow for more 🔔";
  $("brandLogoInput").value = settings.logo_path || "";
  populateBrandKits();
  quality = settings.quality || "1080p";
  document.querySelectorAll("#qualityPicker .quality-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === quality));
  encodeTier = settings.quality_tier || "maximum";
  document.querySelectorAll("#encodePicker .quality-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === encodeTier));
}

function syncDetectModeSub(mode) {
  $("detectModeSub").textContent = mode === "offline"
    ? "Offline mode scans the transcript on your PC — no key, no internet. Scores are heuristic (energy + speech density), not AI."
    : "Built-in AI runs a professional AI model on your PC — no key, no internet. First use downloads the model once (~9 GB).";
  if (mode === "local") {
    show($("localAiCard"));
    refreshLocalAiStatus();
    startLocalAiPolling();
  } else {
    hide($("localAiCard"));
    stopLocalAiPolling();
  }
}

/* ---------------- built-in AI download card ---------------- */
let localAiTimer = null;

function startLocalAiPolling() {
  if (localAiTimer) return;
  localAiTimer = setInterval(refreshLocalAiStatus, 1500);
}

function stopLocalAiPolling() {
  if (localAiTimer) { clearInterval(localAiTimer); localAiTimer = null; }
}

async function refreshLocalAiStatus() {
  let st;
  try {
    const r = await fetch("/api/local-ai/status");
    st = await r.json();
  } catch (e) { return; }  // server hiccup - next poll retries
  const btn = $("localAiDownloadBtn"), barWrap = $("localAiBarWrap"),
    bar = $("localAiBar"), pct = $("localAiPct"), txt = $("localAiText");
  if (!btn) return;
  if (st.ready) {
    txt.textContent = "Built-in AI ready ✓ — the model is on your PC and works fully offline.";
    hide(btn); hide(barWrap); pct.textContent = "";
    stopLocalAiPolling();
    return;
  }
  if (st.downloading) {
    hide(btn); show(barWrap);
    bar.style.width = (st.pct || 0) + "%";
    pct.textContent = (st.stage || "Downloading…") + " — " + Math.round(st.pct || 0) + "%";
  } else {
    show(btn); hide(barWrap); pct.textContent = "";
    if (st.error) {
      txt.textContent = "Download failed: " + st.error + " Check your connection and try again.";
    }
  }
  // Studio hub card mirrors the same Built-in AI state.
  const hBtn = $("hubAiDownloadBtn"), hTxt = $("hubAiText"),
    hWrap = $("hubAiBarWrap"), hBar = $("hubAiBar"), hPct = $("hubAiPct");
  if (hBtn && hTxt) {
    if (st.ready) {
      hTxt.textContent = "Built-in AI ready ✓ — the model is on your PC and works fully offline.";
      hide(hBtn); hide(hWrap); hPct.textContent = "";
    } else if (st.downloading) {
      hide(hBtn); show(hWrap);
      hBar.style.width = (st.pct || 0) + "%";
      hPct.textContent = (st.stage || "Downloading…") + " — " + Math.round(st.pct || 0) + "%";
    } else {
      show(hBtn); hide(hWrap); hPct.textContent = "";
      if (st.error) {
        hTxt.textContent = "Download failed: " + st.error + " Check your connection and try again.";
      }
    }
  }
}

$("localAiDownloadBtn").addEventListener("click", async () => {
  try {
    await fetch("/api/local-ai/download", { method: "POST" });
  } catch (e) { /* the status poll below will surface any problem */ }
  startLocalAiPolling();
  refreshLocalAiStatus();
});

/* Clip length is a RANGE ("min-max" seconds). Legacy single-length
   settings ("60s", "2min", ...) migrate to a sensible range; a custom
   value is either a range ("30-60") or one exact number ("45"). */
const CLIP_LEN_MIGRATION = {
  "15s": "15-30", "30s": "15-30", "60s": "30-60", "90s": "60-90",
  "2min": "90-120", "3min": "120-180", "5min": "180-300",
  "10min": "300-600", "15min": "600-900",
  "30-60s": "30-60", "1-2min": "60-120",
};
function migrateClipLen(v) {
  const s = String(v || "").trim();
  if (/^\d+\s*-\s*\d+$/.test(s)) return s.replace(/\s+/g, "");
  return CLIP_LEN_MIGRATION[s] || "30-60";
}
function parseClipLenValue(str) {
  const s = String(str || "").trim();
  const m = s.match(/^(\d+)\s*-\s*(\d+)$/);
  if (m) {
    let a = parseInt(m[1], 10), b = parseInt(m[2], 10);
    if (a > b) { const t = a; a = b; b = t; }
    return Math.max(5, a) + "-" + Math.max(5, b);
  }
  if (/^\d+$/.test(s)) {
    const n = Math.max(5, parseInt(s, 10));
    return n + "-" + n; // one number = exact length
  }
  return null;
}
function currentClipLen() {
  const custom = $("customLenInput").value.trim();
  if (custom) {
    const parsed = parseClipLenValue(custom);
    if (parsed) return "custom:" + parsed;
  }
  return clipLen;
}
async function persistClipLen() {
  try { await api.save_settings({ clip_length: currentClipLen() }); } catch (e) { /* offline */ }
}

document.querySelectorAll("#detectModeSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("detectModeSeg", b.dataset.value);
    syncDetectModeSub(b.dataset.value);
    settings.detect_mode = b.dataset.value;
    api.save_settings({ detect_mode: b.dataset.value }).catch(() => {});
  }));
document.querySelectorAll("#clipCountSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("clipCountSeg", b.dataset.value);
    clipCount = parseInt(b.dataset.value, 10) || 8;
  }));
document.querySelectorAll("#ratioPicker .ratio-card").forEach((c) =>
  c.addEventListener("click", () => {
    document.querySelectorAll("#ratioPicker .ratio-card").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    ratio = c.dataset.value;
  }));
document.querySelectorAll("#clipLenPills .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("clipLenPills", b.dataset.value);
    clipLen = b.dataset.value;
    $("customLenInput").value = "";
    persistClipLen();
  }));
$("customLenInput").addEventListener("input", () => {
  if ($("customLenInput").value.trim())
    document.querySelectorAll("#clipLenPills .seg").forEach((p) => p.classList.remove("on"));
});
$("customLenInput").addEventListener("change", persistClipLen);
document.querySelectorAll("#qualityPicker .quality-card").forEach((c) =>
  c.addEventListener("click", () => {
    document.querySelectorAll("#qualityPicker .quality-card").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    quality = c.dataset.value;
  }));
document.querySelectorAll("#encodePicker .quality-card").forEach((c) =>
  c.addEventListener("click", async () => {
    document.querySelectorAll("#encodePicker .quality-card").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    encodeTier = c.dataset.value;
    settings.quality_tier = encodeTier;
    try { await api.save_settings({ quality_tier: encodeTier }); } catch (e) { /* offline */ }
  }));

/* genre templates apply their defaults to the controls */
const GENRE_FALLBACK = {
  podcast: { style: "clean", emojis: false, highlight: true, silences: true, fillers: true, censor: false },
  comedy: { style: "bold-pop", emojis: true, highlight: true, silences: false, fillers: false, censor: true },
  gaming: { style: "neon-glow", emojis: true, highlight: true, silences: false, fillers: false, censor: true },
  news: { style: "boxed", emojis: false, highlight: true, silences: false, fillers: false, censor: true },
  business: { style: "clean", emojis: false, highlight: true, silences: true, fillers: true, censor: false },
  fitness: { style: "lime-punch", emojis: true, highlight: true, silences: false, fillers: false, censor: false },
};
let genreDefaults = GENRE_FALLBACK;
document.querySelectorAll("#genreGrid .genre-card").forEach((c) =>
  c.addEventListener("click", () => {
    const g = c.dataset.value;
    const isOn = c.classList.contains("on");
    document.querySelectorAll("#genreGrid .genre-card").forEach((x) => x.classList.remove("on"));
    if (isOn) { genre = ""; return; }   // toggle off
    c.classList.add("on");
    genre = g;
    const d = (genreDefaults && genreDefaults[g]) || GENRE_FALLBACK[g];
    if (!d) return;
    captionStyle = d.style; syncStyleCards();
    $("emojisCheck").checked = !!d.emojis;
    $("highlightCheck").checked = !!d.highlight;
    $("silencesCheck").checked = !!d.silences;
    $("fillerCheck").checked = !!d.fillers;
    $("censorCheck").checked = !!d.censor;
    toast(`Genre: ${c.querySelector(".genre-name").textContent} defaults applied`);
  }));

/* caption styles (built-in + custom) */
function syncStyleCards() {
  document.querySelectorAll("#styleCards .style-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === captionStyle));
  document.querySelectorAll("#customStyleCards .style-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === captionStyle));
}
document.querySelectorAll("#styleCards .style-card").forEach((c) =>
  c.addEventListener("click", () => { captionStyle = c.dataset.value; syncStyleCards(); }));

function renderCustomStyles() {
  const box = $("customStyleCards");
  box.innerHTML = "";
  (settings.custom_styles || []).forEach((s) => {
    const key = String(s.name || "").trim().toLowerCase().replace(/\s+/g, "-");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "style-card" + (captionStyle === key ? " on" : "");
    b.dataset.value = key;
    b.innerHTML = `<span class="style-preview" style="color:${escapeHtml(s.primaryCss || "#fff")}">YOUR <em style="color:${escapeHtml(s.secondaryCss || "#2effc8")}">WORDS</em> POP</span><span class="style-name">${escapeHtml(s.name)}</span>`;
    b.addEventListener("click", () => { captionStyle = key; syncStyleCards(); });
    box.appendChild(b);
  });
}

function hexToAss(hex) {
  // #RRGGBB -> ASS &HAABBGGRR (opaque)
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "&H00FFFFFF";
  const r = m[1].slice(0, 2), g = m[1].slice(2, 4), b = m[1].slice(4, 6);
  return `&H00${b}${g}${r}`.toUpperCase();
}

$("saveStyleBtn").addEventListener("click", async () => {
  const name = $("customStyleName").value.trim();
  if (!name) { toast("Name your style first", true); return; }
  const styles = settings.custom_styles || [];
  const entry = {
    name,
    primary: hexToAss($("customStylePrimary").value),
    secondary: hexToAss($("customStyleSecondary").value),
    outline: hexToAss($("customStyleOutline").value),
    bold: 1,
    primaryCss: $("customStylePrimary").value,
    secondaryCss: $("customStyleSecondary").value,
  };
  const ix = styles.findIndex((s) => s.name.toLowerCase() === name.toLowerCase());
  if (ix >= 0) styles[ix] = entry; else styles.push(entry);
  await api.save_settings({ custom_styles: styles });
  settings.custom_styles = styles;
  captionStyle = name.toLowerCase().replace(/\s+/g, "-");
  renderCustomStyles(); syncStyleCards();
  $("customStyleName").value = "";
  toast("Custom style saved");
});

/* brand kits: caption style + colors + corner logo + intro/outro */
function populateBrandKits() {
  const sel = $("brandKitSelect");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">No brand kit</option>';
  (settings.brand_kits || []).forEach((k) => {
    const o = document.createElement("option");
    o.value = k.name; o.textContent = k.name;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

$("saveBrandKitBtn").addEventListener("click", async () => {
  const name = $("brandKitName").value.trim();
  if (!name) { toast("Name the brand kit first", true); return; }
  const kit = {
    caption_style: captionStyle,
    primary: $("customStylePrimary").value,
    secondary: $("customStyleSecondary").value,
    outline: $("customStyleOutline").value,
    logo_path: $("brandLogoInput").value.trim(),
    intro_path: $("brandIntroInput").value.trim(),
    outro_path: $("brandOutroInput").value.trim(),
  };
  const res = await api.save_brand_kit(name, kit);
  if (res.ok) {
    settings.brand_kits = res.kits;
    populateBrandKits();
    $("brandKitSelect").value = name;
    $("brandKitName").value = "";
    toast(`Brand kit "${name}" saved`);
  } else toast(res.error || "Save failed", true);
});

$("applyBrandKitBtn").addEventListener("click", async () => {
  const name = $("brandKitSelect").value;
  const kit = (settings.brand_kits || []).find((k) => k.name === name);
  if (!kit) { toast("Pick a brand kit first", true); return; }
  // kit colors become a custom caption style "Brand: <name>"
  const styles = settings.custom_styles || [];
  const styleName = "Brand " + kit.name;
  const entry = {
    name: styleName,
    primary: hexToAss(kit.primary || "#ffffff"),
    secondary: hexToAss(kit.secondary || "#2effc8"),
    outline: hexToAss(kit.outline || "#000000"),
    bold: 1,
    primaryCss: kit.primary || "#ffffff",
    secondaryCss: kit.secondary || "#2effc8",
  };
  const ix = styles.findIndex((s) => s.name.toLowerCase() === styleName.toLowerCase());
  if (ix >= 0) styles[ix] = entry; else styles.push(entry);
  await api.save_settings({ custom_styles: styles, logo_path: kit.logo_path || "" });
  settings.custom_styles = styles;
  settings.logo_path = kit.logo_path || "";
  captionStyle = styleName.toLowerCase().replace(/\s+/g, "-");
  renderCustomStyles(); syncStyleCards();
  $("brandLogoInput").value = kit.logo_path || "";
  $("brandIntroInput").value = kit.intro_path || "";
  $("brandOutroInput").value = kit.outro_path || "";
  $("introPathInput").value = kit.intro_path || "";
  $("outroPathInput").value = kit.outro_path || "";
  toast(`Brand kit "${kit.name}" applied`);
});

$("deleteBrandKitBtn").addEventListener("click", async () => {
  const name = $("brandKitSelect").value;
  if (!name) return;
  const res = await api.delete_brand_kit(name);
  if (res.ok) { settings.brand_kits = res.kits; populateBrandKits(); toast("Brand kit deleted"); }
});

/* ---------------- detection ---------------- */
$("detectBtn").addEventListener("click", async () => {
  hide($("detectError"));
  const mode = segGet("detectModeSeg");
  const len = currentClipLen();
  const res = mode === "offline"
    ? await api.detect_offline(jobId, clipCount, len)
    : await api.detect_local(jobId, clipCount, len);
  if (!res.ok) {
    if (res.error === "local_ai_missing") show($("localAiCard"));
    $("detectError").textContent = res.message || res.error;
    show($("detectError"));
    return;
  }
  show($("detectProgress"));
  show($("detectSkeleton"));
  startPoll();
});

/* find clip moment */
function renderPrompts() {
  const box = $("promptList");
  box.innerHTML = "";
  prompts.forEach((p) => {
    const div = document.createElement("div");
    div.className = "prompt-item " + p.state;
    const icon = p.state === "matched" ? "🎯" : p.state === "unmatched" ? "⚠" : "…";
    div.innerHTML = `<span>${icon}</span><span class="prompt-q">${escapeHtml(p.q)}</span>
      <span class="prompt-state">${p.state === "matched" ? "matched" : p.state === "unmatched" ? "no match" : "searching…"}</span>`;
    box.appendChild(div);
  });
}

$("findAddBtn").addEventListener("click", async () => {
  const q = $("findInput").value.trim();
  if (!q) return;
  if (!jobId) { toast("Import a video first", true); return; }
  const p = { q, state: "pending" };
  prompts.push(p);
  $("findInput").value = "";
  renderPrompts();
  const res = await api.find_moments(jobId, [q], currentClipLen());
  if (!res.ok) {
    p.state = "unmatched";
    toast(res.error || "Search failed", true);
  } else if (res.found > 0) {
    p.state = "matched";
    toast(`🎯 Matched: ${q}`);
  } else {
    p.state = "unmatched";
    toast(`No match for "${q}" — try different words`, true);
  }
  renderPrompts();
  startPoll();
  poll();
});
$("findInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("findAddBtn").click();
});

/* ---------------- moments list ---------------- */
function badgeClass(v) { return v >= 80 ? "" : v >= 60 ? "mid" : "low"; }
function bar(label, v) {
  const cls = v >= 80 ? "" : v >= 60 ? "mid" : "low";
  return `<div class="vrow"><span>${label}</span>
    <div class="vbar"><div class="vfill ${cls}" data-w="${v}" style="width:0%"></div></div>
    <b>${v}</b></div>`;
}

$("sortBtn").addEventListener("click", () => {
  sortDesc = !sortDesc;
  $("sortBtn").textContent = sortDesc ? "Sort: virality ↓" : "Sort: virality ↑";
  renderMoments(lastMoments);
});

function renderMoments(moments) {
  const box = $("moments");
  box.innerHTML = "";
  if (!moments.length) {
    hide($("momentsBar")); hide($("renderRow"));
    show($("momentsEmpty"));
    return;
  }
  hide($("momentsEmpty"));
  show($("momentsBar"));
  show($("renderRow"));
  const order = [...moments].sort((a, b) => sortDesc ? b.virality - a.virality : a.virality - b.virality);
  $("momentsCount").textContent = `${moments.length} moment${moments.length === 1 ? "" : "s"}`;
  selectedMoments = moments.map((_, i) => i);
  order.forEach((m) => {
    const i = moments.indexOf(m);
    const div = document.createElement("div");
    div.className = "moment";
    div.style.animationDelay = Math.min(order.indexOf(m) * 60, 360) + "ms";
    const tags = [
      m.caption_edited ? '<span class="edited-tag">edited</span>' : "",
      m.trimmed ? '<span class="edited-tag">trimmed</span>' : "",
      m.offline && !m.matched ? '<span class="edited-tag">offline</span>' : "",
      m.matched ? `<span class="matched-tag">🎯 Matched: ${escapeHtml(m.matched)}</span>` : "",
    ].join("");
    div.innerHTML = `
      <input type="checkbox" data-i="${i}" checked aria-label="Select clip">
      <div class="moment-body">
        <div class="moment-title">${escapeHtml(m.title)}${tags}</div>
        <div class="moment-meta">${fmtTime(m.start)} → ${fmtTime(m.end)} · ${Math.round(m.end - m.start)}s</div>
        <div class="moment-hook">${escapeHtml(m.hook)}</div>
        <div class="vbreak">
          ${bar("Hook", m.hook_score ?? m.virality)}
          ${bar("Flow", m.flow_score ?? m.virality)}
          ${bar("Value", m.value_score ?? m.virality)}
        </div>
        <div class="moment-reason">${escapeHtml(m.reason)}</div>
        <button class="ghost-btn small editCapBtn" data-i="${i}">Edit captions / trim</button>
        <button class="ghost-btn small pkgBtn" data-i="${i}">📝 Posting package</button>
        <div class="pkg-panel hidden" data-i="${i}"></div>
      </div>
      <div class="vbadge ${badgeClass(m.virality)}">
        <span class="vscore" data-v="${m.virality}">0</span>
        <span class="vlabel">virality</span>
      </div>`;
    box.appendChild(div);
  });
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
  box.querySelectorAll(".editCapBtn").forEach((btn) =>
    btn.addEventListener("click", () => openCaptionEditor(parseInt(btn.dataset.i, 10))));
  box.querySelectorAll(".pkgBtn").forEach((btn) =>
    btn.addEventListener("click", () => togglePostingPackage(parseInt(btn.dataset.i, 10), btn)));
}

/* posting package panel per moment */
function copyText(t, label) {
  const done = () => toast((label || "Copied") + " ✓");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const ta = document.createElement("textarea");
  ta.value = t;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed", true); }
  ta.remove();
}

async function togglePostingPackage(i, btn) {
  const panel = document.querySelector(`.pkg-panel[data-i="${i}"]`);
  if (!panel) return;
  if (!panel.classList.contains("hidden")) { hide(panel); return; }
  panel.innerHTML = '<div class="stage">Writing posting package…</div>';
  show(panel);
  const res = await api.posting_package(jobId, i);
  if (!res.ok) {
    panel.innerHTML = `<div class="stage">⚠ ${escapeHtml(res.error || "Failed")}</div>`;
    return;
  }
  const p = res.package;
  const modeLabel = p.mode === "ai" ? "🤖 AI-written" : "⚙️ Auto (on this PC)";
  const modeCls = p.mode === "ai" ? "" : "extractive";
  panel.innerHTML = `
    <div class="pkg-head"><span class="pkg-title">Posting package</span>
      <span class="pkg-mode ${modeCls}">${modeLabel}</span></div>
    <div class="pkg-note">${escapeHtml(p.note || "")}</div>
    <div class="pkg-sec">Title options</div>
    <ul class="pkg-titles">${(p.titles || []).map((t) =>
      `<li><span class="t">${escapeHtml(t)}</span><button class="copy-btn" data-copy="${escapeHtml(t)}">Copy</button></li>`).join("")}</ul>
    <div class="pkg-sec">Description</div>
    <div class="pkg-desc">${escapeHtml(p.description || "")}</div>
    <button class="copy-btn" data-copy="${escapeHtml(p.description || "")}" style="margin-bottom:4px">Copy description</button>
    <div class="pkg-sec">Hashtags</div>
    <div class="pkg-tags">${(p.hashtags || []).map((h) => `<span class="pkg-tag">${escapeHtml(h)}</span>`).join("")}
      <button class="copy-btn" data-copy="${escapeHtml((p.hashtags || []).join(" "))}">Copy all</button></div>`;
  panel.querySelectorAll(".copy-btn").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); copyText(b.dataset.copy, "Copied"); }));
}

/* caption editor + trim */
let editingIndex = null;
async function openCaptionEditor(i) {
  editingIndex = i;
  $("captionMsg").textContent = "";
  const m = lastMoments[i];
  const res = await api.get_caption_words(jobId, i);
  if (!res.ok) { toast(res.error, true); return; }
  $("captionModalTitle").textContent = res.title || "";
  $("captionText").value = res.words || "";
  $("trimStart").value = m ? m.start : "";
  $("trimEnd").value = m ? m.end : "";
  show($("captionModal"));
}
$("saveCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await api.save_caption_words(jobId, editingIndex, $("captionText").value);
  if (res.ok) {
    $("captionMsg").textContent = `✓ Saved (${res.word_count} words). Re-render the clip to bake in corrections.`;
    toast("Captions saved");
    poll();
  } else {
    $("captionMsg").textContent = "⚠ " + (res.error || "Save failed.");
  }
});
$("resetCaptionBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await api.reset_caption_words(jobId, editingIndex);
  if (res.ok) { hide($("captionModal")); toast("Captions reset"); poll(); }
});
$("saveTrimBtn").addEventListener("click", async () => {
  if (editingIndex === null) return;
  const res = await api.trim_moment(jobId, editingIndex, $("trimStart").value, $("trimEnd").value);
  if (res.ok) {
    $("captionMsg").textContent = `✓ Trimmed to ${fmtTime(res.start)} → ${fmtTime(res.end)}. Generate again to re-render.`;
    toast("Trim saved — generate again to re-render");
    poll();
  } else {
    $("captionMsg").textContent = "⚠ " + (res.error || "Trim failed.");
  }
});
$("closeCaptionBtn").addEventListener("click", () => hide($("captionModal")));

/* ---------------- generate (step 3) ---------------- */
function collectRenderOptions() {
  return {
    caption_style: captionStyle,
    ratio, quality, quality_tier: encodeTier,
    face_track: $("faceTrackCheck").checked,
    remove_fillers: $("fillerCheck").checked,
    remove_silences: $("silencesCheck").checked,
    auto_censor: $("censorCheck").checked,
    emojis: $("emojisCheck").checked,
    highlight: $("highlightCheck").checked,
    speaker_colors: $("speakerColorsCheck").checked,
    hook_overlay: $("hookOverlayCheck").checked,
    cta_overlay: $("ctaOverlayCheck").checked,
    cta_text: $("ctaTextInput").value.trim(),
    safe_zone: $("safeZoneCheck").checked,
    punch_zoom: $("punchZoomCheck").checked,
    enhance_clarity: $("enhanceCheck").checked,
    logo_path: $("brandLogoInput").value.trim(),
  };
}

/* speaker color + CTA text + logo path persist with the rest of settings */
["speakerColor1", "speakerColor2"].forEach((id) =>
  $(id).addEventListener("change", async () => {
    settings.speaker_color_1 = $("speakerColor1").value;
    settings.speaker_color_2 = $("speakerColor2").value;
    try { await api.save_settings({ speaker_color_1: settings.speaker_color_1, speaker_color_2: settings.speaker_color_2 }); } catch (e) { /* offline */ }
  }));
$("ctaTextInput").addEventListener("change", async () => {
  settings.cta_text = $("ctaTextInput").value;
  try { await api.save_settings({ cta_text: settings.cta_text }); } catch (e) { /* offline */ }
});
$("brandLogoInput").addEventListener("change", async () => {
  settings.logo_path = $("brandLogoInput").value.trim();
  try { await api.save_settings({ logo_path: settings.logo_path }); } catch (e) { /* offline */ }
});

async function startRender(indices) {
  if (!indices.length) { toast("Select at least one moment", true); return; }
  hide($("renderError"));
  const res = await api.render(jobId, indices, collectRenderOptions());
  if (!res.ok) { toast(res.error || "Render failed", true); return; }
  reveal($("step3"));
  setStep(3);
  show($("renderProgress"));
  startPoll();
}
$("renderSelBtn").addEventListener("click", () => startRender(selectedMoments));
$("renderAllBtn").addEventListener("click", () => {
  const boxes = document.querySelectorAll("#moments input[type=checkbox]");
  startRender([...boxes].map((cb) => parseInt(cb.dataset.i, 10)));
});

document.querySelectorAll("#viewSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("viewSeg", b.dataset.value);
    viewMode = b.dataset.value;
    $("renders").classList.toggle("list-view", viewMode === "list");
  }));

function renderRenders(renders) {
  const box = $("renders");
  box.innerHTML = "";
  mergeSelected = [];
  hide($("mergeBtn"));
  if (renders && renders.length) hide($("rendersEmpty"));
  (renders || []).forEach((r, i) => {
    const div = document.createElement("div");
    div.className = "render-card";
    div.style.animationDelay = Math.min(i * 60, 360) + "ms";
    div.innerHTML = `
      <div class="render-top">
        <input type="checkbox" class="merge-check" data-i="${i}" title="Tick to merge">
        <div class="render-title">${escapeHtml(r.title)}
          <span class="mini-badge ${badgeClass(r.virality)}">${r.virality}</span></div>
      </div>
      <div class="path">${escapeHtml(r.path)}</div>
      <div class="render-actions">
        <button class="ghost-btn small prevBtn" data-i="${i}">Preview</button>
        <button class="ghost-btn small capPrevBtn" data-i="${i}">Captions only</button>
        <button class="ghost-btn small studioBtn" data-i="${i}">Edit captions</button>
        <button class="ghost-btn small srtBtn" data-i="${i}">SRT</button>
        <button class="ghost-btn small vttBtn" data-i="${i}">VTT</button>
      </div>
      <div class="trim-row">
        <input type="text" class="trim-s" data-i="${i}" inputmode="decimal" placeholder="Start s" title="Trim start (seconds)">
        <input type="text" class="trim-e" data-i="${i}" inputmode="decimal" placeholder="End s" title="Trim end (seconds)">
        <button class="ghost-btn small reRenderBtn" data-i="${i}" data-m="${r.moment_index}">Re-render</button>
      </div>
      <div class="plan-row">
        <input type="datetime-local" class="plan-when" data-i="${i}" title="Schedule this clip">
        <button class="ghost-btn small planBtn" data-i="${i}">🗓 Plan</button>
      </div>`;
    box.appendChild(div);
  });
  box.querySelectorAll(".merge-check").forEach((cb) =>
    cb.addEventListener("change", () => {
      const i = parseInt(cb.dataset.i, 10);
      mergeSelected = mergeSelected.filter((x) => x !== i);
      if (cb.checked) mergeSelected.push(i);
      mergeSelected.sort((a, b) => a - b);
      if (mergeSelected.length >= 2) show($("mergeBtn")); else hide($("mergeBtn"));
    }));
  const dl = async (btn, fn, label) => {
    const res = await fn();
    if (res.ok && res.url) { downloadUrl(res.url); toast(label + " downloaded"); }
    else toast(res.error || (label + " failed"), true);
  };
  box.querySelectorAll(".srtBtn").forEach((btn) =>
    btn.addEventListener("click", () => dl(btn, () => api.export_srt(jobId, parseInt(btn.dataset.i, 10)), "SRT")));
  box.querySelectorAll(".vttBtn").forEach((btn) =>
    btn.addEventListener("click", () => dl(btn, () => api.export_vtt(jobId, parseInt(btn.dataset.i, 10)), "VTT")));
  box.querySelectorAll(".prevBtn").forEach((btn) =>
    btn.addEventListener("click", () => {
      const r = (renderRenders._list || [])[parseInt(btn.dataset.i, 10)];
      if (r && r.url) openPreview(r.title, r.url);
    }));
  box.querySelectorAll(".capPrevBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const i = parseInt(btn.dataset.i, 10);
      const list = renderRenders._list || [];
      const res = await api.preview_captions(jobId, list[i] ? list[i].moment_index : i);
      if (res.ok && res.url) openPreview("Captions preview", res.url);
      else toast(res.error || "Preview failed", true);
    }));
  box.querySelectorAll(".studioBtn").forEach((btn) =>
    btn.addEventListener("click", () => {
      const r = (renderRenders._list || [])[parseInt(btn.dataset.i, 10)];
      if (r) openCaptionStudio(r);
    }));
  box.querySelectorAll(".reRenderBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const i = parseInt(btn.dataset.i, 10);
      const mi = parseInt(btn.dataset.m, 10);
      const s = box.querySelector(`.trim-s[data-i="${i}"]`).value.trim();
      const e = box.querySelector(`.trim-e[data-i="${i}"]`).value.trim();
      if (s || e) {
        const cur = lastMoments[mi];
        const tr = await api.trim_moment(jobId, mi, s || (cur ? cur.start : 0), e || (cur ? cur.end : 0));
        if (!tr.ok) { toast(tr.error || "Trim failed", true); return; }
      }
      startRender([mi]);
    }));
  box.querySelectorAll(".planBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const i = parseInt(btn.dataset.i, 10);
      const when = box.querySelector(`.plan-when[data-i="${i}"]`).value;
      if (!when) { toast("Pick a date and time first", true); return; }
      const res = await api.add_planner_entry(jobId, i, when);
      if (res.ok) { toast("Scheduled ✓"); refreshPlanner(); }
      else toast(res.error || "Schedule failed", true);
    }));
  renderRenders._list = renders;
  refreshPlanner();
}

/* posting planner (local schedule queue - no auto-posting) */
async function refreshPlanner() {
  try {
    const res = await api.get_planner();
    renderPlanner(res.planner || []);
  } catch (e) { /* offline */ }
}
function renderPlanner(entries) {
  const box = $("plannerList");
  if (!box) return;
  box.innerHTML = "";
  if (!entries.length) {
    box.innerHTML = '<div class="empty-sub">Nothing scheduled yet — set a date/time on a clip above and hit “Plan”.</div>';
    return;
  }
  entries.forEach((e) => {
    const div = document.createElement("div");
    div.className = "planner-item" + (e.done ? " done" : "");
    div.innerHTML = `
      <input type="checkbox" class="planner-check" ${e.done ? "checked" : ""} title="Mark as posted">
      <span class="planner-title">${escapeHtml(e.title)}</span>
      <span class="planner-when">🗓 ${escapeHtml((e.scheduled || "").replace("T", " "))}</span>
      <span class="planner-export">${escapeHtml(e.export_name || "")}</span>
      <button class="copy-btn plan-copy">Copy name</button>
      <button class="copy-btn plan-del" title="Remove">✕</button>`;
    div.querySelector(".planner-check").addEventListener("change", async (ev) => {
      await api.update_planner_entry(e.id, { done: ev.target.checked });
      refreshPlanner();
    });
    div.querySelector(".plan-copy").addEventListener("click", () => copyText(e.export_name || "", "Export name copied"));
    div.querySelector(".plan-del").addEventListener("click", async () => {
      await api.delete_planner_entry(e.id);
      refreshPlanner();
    });
    box.appendChild(div);
  });
}

function openPreview(title, url) {
  $("previewTitle").textContent = title || "Preview";
  const v = $("previewVideo");
  v.src = url;
  const guide = $("safeZoneGuideCheck") && $("safeZoneGuideCheck").checked;
  $("safeZoneOverlay").classList.toggle("hidden", !guide);
  show($("previewModal"));
  v.play().catch(() => {});
}
$("closePreviewBtn").addEventListener("click", () => closeModal("previewModal"));

/* composer */
$("mergeBtn").addEventListener("click", async () => {
  if (mergeSelected.length < 2) return;
  $("mergeBtn").disabled = true;
  const res = await api.merge_selected(jobId, mergeSelected,
    $("transitionSelect").value, $("introPathInput").value, $("outroPathInput").value);
  $("mergeBtn").disabled = false;
  if (res.ok) {
    mergeSelected = [];
    hide($("mergeBtn"));
    toast("Clips merged");
    poll();
  } else {
    toast(res.error || "Merge failed", true);
  }
});

/* zip + xml + folder + new */
$("zipBtn").addEventListener("click", async () => {
  if (!jobId || $("zipBtn").disabled) return;
  $("zipBtn").disabled = true;
  toast("Building ZIP…");
  try {
    const res = await api.make_zip(jobId, true);
    if (res.ok && res.url) {
      downloadUrl(res.url);
      toast(res.message || "ZIP downloaded", !!res.message);
    } else toast(res.error || "ZIP failed", true);
  } finally {
    $("zipBtn").disabled = false;
  }
});
$("xmlBtn").addEventListener("click", async () => {
  if (!jobId) return;
  const res = await api.export_xml(jobId);
  if (res.ok && res.url) { downloadUrl(res.url); toast("XML timeline downloaded"); }
  else toast(res.error || "XML failed", true);
});
$("openFolderBtn").addEventListener("click", async () => { await api.open_folder(jobId); });
$("newVideoBtn").addEventListener("click", () => {
  stopPoll();
  resetNav();
  jobId = null;
  lastMomentsSig = ""; lastRendersSig = ""; lastErrSig = ""; lastQueueSig = "";
  lastMoments = []; mergeSelected = []; prompts = [];
  controlsInitFor = null; thumbLoadedFor = null;
  $("urlInput").value = ""; $("localPathInput").value = "";
  $("batchUrls").value = ""; $("batchStatus").textContent = "";
  ["step2", "step3"].forEach((s) => hide($(s)));
  hide($("videoInfo")); hide($("longVideoWarn")); hide($("importProgress")); hide($("importError"));
  hide($("detectError")); hide($("renderError"));
  hide($("videoThumb")); show($("videoThumbFallback"));
  show($("momentsEmpty")); show($("rendersEmpty"));
  hide($("momentsBar"));
  $("importBtn").disabled = false; $("localFileBtn").disabled = false;
  renderPrompts();
  setStep(1);
  refreshQueue();
});

/* ---------------- library ---------------- */
let libraryOpen = false;

function libStatusText(j) {
  if (j.clips > 0) return `Done - ${j.clips} clip${j.clips > 1 ? "s" : ""} rendered`;
  if (j.moments > 0) return `${j.moments} moment${j.moments > 1 ? "s" : ""} found`;
  return "Transcript ready";
}

async function refreshLibrary() {
  let res;
  try { res = await api.library(); } catch (e) { return; }
  const projects = res.projects || [];
  const grid = $("libraryGrid");
  grid.innerHTML = "";
  if (!projects.length) { show($("libraryEmpty")); return; }
  hide($("libraryEmpty"));
  projects.forEach((j) => {
    const card = document.createElement("div");
    card.className = "lib-card";
    const date = j.created
      ? new Date(j.created * 1000).toLocaleDateString(undefined,
          { month: "short", day: "numeric", year: "numeric" })
      : "";
    card.innerHTML = `
      <div class="lib-thumb">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="M10 9l5 3-5 3z" fill="currentColor" stroke="none"/></svg>
        <img src="/files/${j.job_id}/thumb.jpg" alt="" onerror="this.remove()">
      </div>
      <div class="lib-body">
        <div class="lib-title" title="${escapeHtml(j.title || "Video")}">${escapeHtml(j.title || "Video")}</div>
        <div class="lib-meta">${fmtTime(j.duration)}${date ? " · " + date : ""}</div>
        <div class="lib-status"><span class="job-dot ${j.status}"></span><span>${escapeHtml(libStatusText(j))}</span></div>
        <div class="lib-actions">
          <button class="primary small lib-open">Open</button>
          <button class="ghost-btn small lib-del">Delete</button>
        </div>
      </div>`;
    card.querySelector(".lib-open").addEventListener("click",
      () => openProject(j.job_id));
    const del = card.querySelector(".lib-del");
    del.addEventListener("click", async () => {
      if (!del.dataset.armed) {
        del.dataset.armed = "1";
        del.textContent = "Sure?";
        setTimeout(() => {
          delete del.dataset.armed;
          del.textContent = "Delete";
        }, 2600);
        return;
      }
      const res2 = await api.delete_job(j.job_id);
      if (res2.ok) {
        if (jobId === j.job_id) jobId = null;
        toast("Deleted from Library");
        refreshLibrary();
        refreshQueue();
      } else {
        toast(res2.error || "Delete failed", true);
      }
    });
    grid.appendChild(card);
  });
}

/* ---------------- navigation history (Back button + logo home) -------
   A lightweight view/step history. Navigation NEVER touches project
   state: job id, transcript, moments, renders and the clips DOM all
   live outside this system, so Back -> return shows everything
   exactly as it was left, and polling resumes where it applies.
   (State variables live with the other state at the top of the file.) */
function noteNav(entry) {
  if (navApplying) return;
  if (entry.view === navNow.view && entry.step === navNow.step) return;
  navStack.push(navNow);
  if (navStack.length > 50) navStack.shift();
  navNow = entry;
  updateBackBtn();
}
function applyNav(entry) {
  navApplying = true;
  try {
    if (entry.view === "hub") {
      showHub();
    } else if (entry.view === "library") {
      showLibrary();
    } else if (entry.view === "remover") {
      showRemover();
    } else if (TOOL_VIEWS[entry.view]) {
      showTool(entry.view);
    } else {
      if (removerOpen) hideRemover();
      if (libraryOpen) hideLibrary();
      closeStudioViews();
      show($("step1"));
      setStep(entry.step || 1);
      const sec = $("step" + (entry.step || 1));
      if (sec) setTimeout(() =>
        sec.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
      if (jobId) { startPoll(); poll(); }
    }
  } finally { navApplying = false; }
  navNow = entry;
  updateBackBtn();
}
function goBack() {
  const modal = $("settingsModal");
  if (modal && !modal.classList.contains("hidden")) {
    closeSettings();
    return;
  }
  const prev = navStack.pop();
  if (!prev) { updateBackBtn(); return; }  // at the root: a safe no-op
  applyNav(prev);
}
function updateBackBtn() {
  const b = $("backBtn");
  if (!b) return;
  const atRoot = navNow.view === "hub";
  b.disabled = navStack.length === 0 && atRoot;
}
function resetNav() {
  navStack = [];
  navNow = { view: "hub" };
  lastWizardStep = 1;
  maxStepReached = 1;
  updateBackBtn();
}
$("backBtn").addEventListener("click", goBack);
document.querySelectorAll(".step-dot").forEach((d) => {
  d.addEventListener("click", () => {
    if (libraryOpen || removerOpen || hubOpen || toolOpen) return;
    const target = parseInt(d.dataset.step, 10);
    if (!target || target > maxStepReached) return;  // reached steps only
    if (navNow.view === "wizard" && (navNow.step || 1) === target) return;
    noteNav({ view: "wizard", step: target });
    applyNav({ view: "wizard", step: target });
  });
});
/* Logo = Home (Addendum 8a): a navigation, never a reset - the
   current project stays intact and can be re-entered any time.
   Home is the Studio hub now. */
function goHome() {
  if (navNow.view === "hub" && !libraryOpen && !removerOpen
      && !toolOpen) return;
  noteNav({ view: "hub" });
  applyNav({ view: "hub" });
}
document.querySelectorAll(".home-link").forEach((el) => {
  el.addEventListener("click", goHome);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goHome(); }
  });
});

function showLibrary() {
  noteNav({ view: "library", step: lastWizardStep });
  $("libraryBtn").classList.add("on");
  $("removerBtn").classList.remove("on");
  closeStudioViews();
  libraryOpen = true;
  stopPoll();
  hide($("step1")); hide($("step2")); hide($("step3"));
  hide($("removerView"));
  show($("libraryView"));
  refreshLibrary();
}

function hideLibrary() {
  noteNav({ view: "wizard", step: lastWizardStep });
  $("libraryBtn").classList.remove("on");
  libraryOpen = false;
  hide($("libraryView"));
  show($("step1"));
  if (jobId) { startPoll(); poll(); }
  else { setStep(1); refreshQueue(); }
}

function openProject(id) {
  noteNav({ view: "wizard", step: lastWizardStep });
  hide($("libraryView"));
  libraryOpen = false;
  show($("step1"));
  if (id === jobId) { startPoll(); poll(); return; }
  selectJob(id);
}

$("libraryBtn").addEventListener("click", showLibrary);
$("libBackBtn").addEventListener("click", backToStudio);

/* ---------------- Caption Remover ---------------- */
let removerOpen = false;
let removerMode = "auto";
let removerRegionPreset = "auto";
let removerDetected = null;
let removerModeTouched = false;
let removerTimer = null;

function showRemover() {
  noteNav({ view: "remover", step: lastWizardStep });
  $("removerBtn").classList.add("on");
  $("libraryBtn").classList.remove("on");
  closeStudioViews();
  removerOpen = true;
  libraryOpen = false;
  stopPoll();
  hide($("step1")); hide($("step2")); hide($("step3"));
  hide($("libraryView"));
  show($("removerView"));
  updateRemoverRegionBox();
}
function hideRemover() {
  noteNav({ view: "wizard", step: lastWizardStep });
  $("removerBtn").classList.remove("on");
  removerOpen = false;
  if (removerTimer) { clearInterval(removerTimer); removerTimer = null; }
  hide($("removerView"));
  show($("step1"));
  if (jobId) { startPoll(); poll(); }
  else { setStep(1); refreshQueue(); }
}
$("removerBtn").addEventListener("click", showRemover);
$("removerBackBtn").addEventListener("click", backToStudio);

function removerRegion() {
  if (removerRegionPreset === "auto") return { auto: true };
  return {
    top_pct: parseInt($("removerTop").value, 10),
    bottom_pct: parseInt($("removerBot").value, 10),
  };
}
function updateRemoverRegionBox() {
  const t = parseInt($("removerTop").value, 10);
  const b = parseInt($("removerBot").value, 10);
  $("removerTopVal").textContent = t + "%";
  $("removerBotVal").textContent = b + "%";
  const box = $("removerRegionBox");
  box.style.top = t + "%";
  box.style.height = Math.max(0, b - t) + "%";
  if (removerRegionPreset === "auto") {
    $("removerStageLabel").textContent = (removerDetected && removerDetected.found)
      ? "Auto-detected caption band: " + Math.round(removerDetected.top_pct) + "% – " + Math.round(removerDetected.bottom_pct) + "% — this is what gets cleaned"
      : "Auto-detect — the app finds the caption band by itself when you press Remove";
  }
}
function setRemoverRegion(preset) {
  removerRegionPreset = preset;
  segSet("removerRegionSeg", preset);
  if (preset === "bottom") { $("removerTop").value = 82; $("removerBot").value = 100; }
  if (preset === "top") { $("removerTop").value = 0; $("removerBot").value = 14; }
  if (preset === "auto") {
    if (removerDetected && removerDetected.found) {
      $("removerTop").value = Math.round(removerDetected.top_pct);
      $("removerBot").value = Math.round(removerDetected.bottom_pct);
    }
    if (!removerModeTouched && removerMode !== "auto") {
      removerMode = "auto";
      document.querySelectorAll("#removerModePicker .mode-card").forEach((x) =>
        x.classList.toggle("on", x.dataset.value === "auto"));
    }
  }
  $("removerSliders").classList.toggle("hidden", preset !== "custom");
  $("removerAutoHint").classList.toggle("hidden", preset !== "auto");
  if (preset !== "auto")
    $("removerStageLabel").textContent = "Region preview — the highlighted band is what gets cleaned";
  updateRemoverRegionBox();
}
document.querySelectorAll("#removerRegionSeg .seg").forEach((b) =>
  b.addEventListener("click", () => setRemoverRegion(b.dataset.value)));
["removerTop", "removerBot"].forEach((id) =>
  $(id).addEventListener("input", () => {
    if (removerRegionPreset !== "custom") setRemoverRegion("custom");
    const t = parseInt($("removerTop").value, 10);
    const b = parseInt($("removerBot").value, 10);
    if (b - t < 2) {
      if (id === "removerTop") $("removerBot").value = Math.min(100, t + 2);
      else $("removerTop").value = Math.max(0, b - 2);
    }
    updateRemoverRegionBox();
  }));
document.querySelectorAll("#removerModePicker .mode-card").forEach((c) =>
  c.addEventListener("click", () => {
    document.querySelectorAll("#removerModePicker .mode-card").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    removerMode = c.dataset.value;
    removerModeTouched = true;
  }));
$("removerPath").addEventListener("change", async () => {
  const p = $("removerPath").value.trim();
  if (!p) return;
  try {
    const r = await api.caption_remover_probe(p);
    if (r && r.ok && r.probed && r.width && r.height)
      $("removerStage").style.aspectRatio = r.width + " / " + r.height;
    if (r && r.ok && r.detected) {
      removerDetected = r.detected;
      if (removerRegionPreset === "auto") setRemoverRegion("auto");
      const stEl = $("removerAutoStatus");
      if (stEl) {
        stEl.textContent = r.detected.found
          ? "Auto found the captions: band " + Math.round(r.detected.top_pct) + "% – " + Math.round(r.detected.bottom_pct) + "% of the frame height. Press Remove Captions — nothing else to pick."
          : "Auto scanned this video and found no clear caption band — it will use the standard bottom band (82% – 100%). If the captions sit somewhere else, open Advanced below.";
      }
    }
  } catch (e) { /* stage sizing is a nicety */ }
});
async function startRemoverJob(kind) {
  const source = $("removerUrl").value.trim() || $("removerPath").value.trim();
  const errEl = $("removerError");
  errEl.classList.add("hidden");
  if (!source) {
    errEl.textContent = "Paste a video link or a local file path first.";
    errEl.classList.remove("hidden");
    return;
  }
  hide($("removerResult"));
  $("removerVideo").classList.add("hidden");
  $("removerRegionBox").classList.remove("hidden");
  $("removerStageLabel").textContent = "Region preview — the highlighted band is what gets cleaned";
  show($("removerProgress"));
  $("removerBar").style.width = "0%";
  $("removerRunBtn").disabled = true;
  $("removerPreviewBtn").disabled = true;
  removerLogRendered = 0; removerLogRid = null;
  $("removerLog").innerHTML = "";
  show($("removerLogWrap"));
  let started;
  try {
    started = kind === "preview"
      ? await api.caption_remover_preview(source, removerRegion(), removerMode)
      : await api.caption_remover_start(source, removerRegion(), removerMode);
  } catch (e) { started = { ok: false, error: String(e) }; }
  if (!started || !started.ok) {
    $("removerRunBtn").disabled = false;
    $("removerPreviewBtn").disabled = false;
    hide($("removerProgress"));
    errEl.textContent = (started && started.error) || "Could not start the removal.";
    errEl.classList.remove("hidden");
    return;
  }
  const rid = started.id;
  if (removerTimer) clearInterval(removerTimer);
  removerTimer = setInterval(async () => {
    let st;
    try { st = await api.caption_remover_status(rid); } catch (e) { return; }
    if (!st || !st.ok) return;
    $("removerBar").style.width = (st.pct || 0) + "%";
    $("removerStageText").textContent = st.stage || "";
    renderRemoverLog(rid, st.log || []);
    if (st.status === "done") {
      clearInterval(removerTimer); removerTimer = null;
      $("removerRunBtn").disabled = false;
      $("removerPreviewBtn").disabled = false;
      hide($("removerProgress"));
      const v = $("removerVideo");
      v.src = st.url;
      v.classList.remove("hidden");
      $("removerRegionBox").classList.add("hidden");
      const isPreview = st.kind === "preview";
      $("removerStageLabel").textContent = isPreview
        ? "Cleaned preview — the worst 5 seconds"
        : "Cleaned video preview";
      $("removerResultHint").textContent = isPreview
        ? "Preview section " + (st.section || "") + " — if this looks clean, press Remove Captions for the whole video."
        : "The preview above is the cleaned video.";
      $("removerDownload").href = st.url;
      show($("removerResult"));
    } else if (st.status === "error") {
      clearInterval(removerTimer); removerTimer = null;
      $("removerRunBtn").disabled = false;
      $("removerPreviewBtn").disabled = false;
      hide($("removerProgress"));
      errEl.textContent = st.error || "Removal failed.";
      errEl.classList.remove("hidden");
    }
  }, 1000);
}
$("removerRunBtn").addEventListener("click", () => startRemoverJob("full"));
$("removerPreviewBtn").addEventListener("click", () => startRemoverJob("preview"));

/* ---------------- remover log console ---------------- */
let removerLogRendered = 0, removerLogRid = null, removerLogFirstT = -1;
function renderRemoverLog(rid, log) {
  const box = $("removerLog");
  if (removerLogRid !== rid) {
    removerLogRid = rid; removerLogRendered = 0; removerLogFirstT = -1;
    box.innerHTML = "";
  }
  const firstT = log.length ? log[0].t : -1;
  if (log.length < removerLogRendered || firstT !== removerLogFirstT) {
    box.innerHTML = ""; removerLogRendered = 0;
  }
  removerLogFirstT = firstT;
  for (let i = removerLogRendered; i < log.length; i++) {
    const e = log[i];
    const div = document.createElement("div");
    if (String(e.text).startsWith("ERROR")) div.className = "err";
    div.innerHTML = `<span class="lt">[${fmtClock(e.t)}]</span>`
      + escapeHtml(e.text);
    box.appendChild(div);
  }
  removerLogRendered = log.length;
  box.scrollTop = box.scrollHeight;
}

/* ---------------- Caption Studio ---------------- */
let studioData = null;
let studioCtx = null;
let studioApplyingAll = false;
const STUDIO_SIZE_PX = { xs: 13, s: 16, m: 19, l: 23, xl: 28, xxl: 33 };
const STUDIO_FONT_CSS = { default: "Arial", anton: "'Anton'",
  "bebas-neue": "'Bebas Neue'", montserrat: "'Montserrat'",
  poppins: "'Poppins'", "archivo-black": "'Archivo Black'" };
const STUDIO_PRESET_LABELS = { default: "Default", "one-word": "One Word",
  clean: "Clean", pill: "Pill", "lime-box": "Lime Box", hormozi: "Hormozi",
  karaoke: "Karaoke", beast: "Beast", neon: "Neon", "gold-glow": "Gold Glow",
  cyber: "Cyber", boxed: "Boxed", minimal: "Minimal", classic: "Classic",
  "royal-blue": "Royal Blue", mint: "Mint", fire: "Fire", bubble: "Bubble",
  ghost: "Ghost", "shorts-pop": "Shorts Pop" };
const STYLE_TO_PRESET_JS = { "bold-pop": "default", "lime-punch": "lime-box",
  clean: "clean", "neon-glow": "neon", hollow: "ghost", boxed: "boxed" };

function blendHex(hex, dimPct) {
  const f = 1 - 0.8 * (Math.max(0, Math.min(100, dimPct || 0)) / 100);
  const n = parseInt(String(hex || "#FFFFFF").slice(1), 16);
  const r = Math.round(((n >> 16) & 255) * f);
  const g = Math.round(((n >> 8) & 255) * f);
  const b = Math.round((n & 255) * f);
  return `rgb(${r},${g},${b})`;
}

async function ensureStudioData() {
  if (!studioData) studioData = await api.caption_presets();
  return studioData;
}

async function openCaptionStudio(r) {
  if (!r) { toast("Clip not found", true); return; }
  const data = await ensureStudioData();
  if (!data || !data.ok) { toast("Caption Studio unavailable", true); return; }
  const moment = (lastMoments || [])[r.moment_index] || {};
  let params = null;
  const seed = r.caption_params || moment.caption_params;
  if (seed) params = JSON.parse(JSON.stringify(seed));
  if (!params) {
    const pid = STYLE_TO_PRESET_JS[captionStyle] || "default";
    params = { ...(data.presets[pid] || data.presets.default), preset: pid };
  }
  studioCtx = { momentIndex: r.moment_index, params, render: r };
  $("studioClipTitle").textContent = r.title || "Clip";
  const v = $("studioVideo");
  v.src = r.url || "";
  v.onloadedmetadata = () => {
    try { v.currentTime = (v.duration || 0) * 0.4; } catch (e) { /* noop */ }
  };
  try {
    const res = await api.get_caption_words(jobId, r.moment_index);
    $("studioWords").value = res && res.ok ? res.words : "";
  } catch (e) { $("studioWords").value = ""; }
  buildStudioControls();
  syncStudioControls();
  renderStudioPreview();
  show($("captionStudioModal"));
}

function buildStudioControls() {
  const data = studioData;
  const grid = $("studioPresets");
  grid.innerHTML = "";
  Object.keys(data.presets).forEach((pid) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset-chip";
    b.dataset.preset = pid;
    b.textContent = STUDIO_PRESET_LABELS[pid] || pid;
    b.addEventListener("click", () => {
      studioCtx.params = { ...data.presets[pid], preset: pid };
      syncStudioControls();
      renderStudioPreview();
    });
    grid.appendChild(b);
  });
  const fontSel = $("studioFont");
  fontSel.innerHTML = "";
  data.fonts.forEach((f) => {
    const o = document.createElement("option");
    o.value = f.id;
    o.textContent = f.name;
    fontSel.appendChild(o);
  });
  const sizeSeg = $("studioSizeSeg");
  sizeSeg.innerHTML = "";
  data.sizes.forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg";
    b.dataset.value = s;
    b.textContent = s.toUpperCase();
    b.addEventListener("click", () => {
      studioCtx.params.size = s;
      syncStudioControls();
      renderStudioPreview();
    });
    sizeSeg.appendChild(b);
  });
}

function syncStudioControls() {
  const p = studioCtx.params;
  document.querySelectorAll("#studioPresets .preset-chip").forEach((c) =>
    c.classList.toggle("on", c.dataset.preset === p.preset));
  $("studioFont").value = p.font;
  document.querySelectorAll("#studioSizeSeg .seg").forEach((b) =>
    b.classList.toggle("on", b.dataset.value === p.size));
  document.querySelectorAll("#studioPosSeg .seg").forEach((b) =>
    b.classList.toggle("on", b.dataset.value === p.position));
  document.querySelectorAll("#studioAnimSeg .seg").forEach((b) =>
    b.classList.toggle("on", b.dataset.value === p.animation));
  $("studioDim").value = p.dim_inactive;
  $("studioDimVal").textContent = p.dim_inactive + "%";
  $("studioBorder").value = p.border;
  $("studioBorderVal").textContent = p.border;
  $("studioUpper").checked = !!p.uppercase;
  $("studioOneWord").checked = !!p.one_word;
  $("studioTextColor").value = p.text_color;
  $("studioHlColor").value = p.highlight_color;
}

function renderStudioPreview() {
  if (!studioCtx) return;
  const p = studioCtx.params;
  const line = $("studioLine");
  const words = ($("studioWords").value || "Your caption words")
    .trim().split(/\s+/).filter(Boolean);
  let showWords, activeIdx;
  if (p.one_word) { showWords = [words[0] || "Word"]; activeIdx = 0; }
  else { showWords = words.slice(0, 3); activeIdx = Math.min(1, showWords.length - 1); }
  line.style.fontFamily = STUDIO_FONT_CSS[p.font] || "Arial";
  line.style.fontSize = (STUDIO_SIZE_PX[p.size] || 19) + "px";
  line.style.textTransform = p.uppercase ? "uppercase" : "none";
  line.style.webkitTextStroke =
    Math.max(0, p.border * 0.35) + "px " + (p.outline_color || "#000000");
  $("studioOverlay").className = "studio-overlay pos-" + p.position;
  line.innerHTML = showWords.map((w, i) => {
    const active = i === activeIdx;
    const col = active
      ? (p.one_word ? p.text_color : p.highlight_color)
      : blendHex(p.text_color, p.dim_inactive);
    let style = "color:" + col;
    let cls = "w" + (active ? " active" : "");
    if (active && p.animation === "glow")
      style += ";text-shadow:0 0 12px " + p.highlight_color
        + ",0 0 3px " + p.highlight_color;
    if (active && p.animation === "box")
      style += ";background:" + p.highlight_color
        + ";color:#10130A;border-radius:6px;padding:0 6px";
    if (active && p.animation === "pop") cls += " pop";
    return `<span class="${cls}" style="${style}">${escapeHtml(w)}</span>`;
  }).join(" ");
}

function closeStudio() { hide($("captionStudioModal")); }
$("studioClose").addEventListener("click", closeStudio);
$("studioCancel").addEventListener("click", closeStudio);
$("captionStudioModal").addEventListener("click", (e) => {
  if (e.target === $("captionStudioModal")) closeStudio();
});
$("studioFont").addEventListener("change", () => {
  studioCtx.params.font = $("studioFont").value; renderStudioPreview();
});
$("studioDim").addEventListener("input", () => {
  studioCtx.params.dim_inactive = parseInt($("studioDim").value, 10) || 0;
  $("studioDimVal").textContent = studioCtx.params.dim_inactive + "%";
  renderStudioPreview();
});
$("studioBorder").addEventListener("input", () => {
  studioCtx.params.border = parseInt($("studioBorder").value, 10) || 0;
  $("studioBorderVal").textContent = studioCtx.params.border;
  renderStudioPreview();
});
$("studioUpper").addEventListener("change", () => {
  studioCtx.params.uppercase = $("studioUpper").checked;
  renderStudioPreview();
});
$("studioOneWord").addEventListener("change", () => {
  studioCtx.params.one_word = $("studioOneWord").checked;
  renderStudioPreview();
});
$("studioTextColor").addEventListener("input", () => {
  studioCtx.params.text_color = $("studioTextColor").value;
  renderStudioPreview();
});
$("studioHlColor").addEventListener("input", () => {
  studioCtx.params.highlight_color = $("studioHlColor").value;
  renderStudioPreview();
});
$("studioWords").addEventListener("input", renderStudioPreview);
document.querySelectorAll("#studioPosSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    studioCtx.params.position = b.dataset.value;
    syncStudioControls(); renderStudioPreview();
  }));
document.querySelectorAll("#studioAnimSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    studioCtx.params.animation = b.dataset.value;
    syncStudioControls(); renderStudioPreview();
  }));
$("studioSaveWords").addEventListener("click", async () => {
  const res = await api.save_caption_words(
    jobId, studioCtx.momentIndex, $("studioWords").value);
  if (res && res.ok) toast("Caption text saved");
  else toast((res && res.error) || "Save failed", true);
});

async function studioRenderOne(momentIndex, params) {
  const opts = { ...collectRenderOptions(), caption_params: { ...params } };
  const res = await api.render(jobId, [momentIndex], opts);
  if (!res || !res.ok) {
    toast((res && (res.message || res.error)) || "Render failed", true);
    return false;
  }
  return true;
}

$("studioApply").addEventListener("click", async () => {
  const { momentIndex, params } = studioCtx;
  closeStudio();
  if (await studioRenderOne(momentIndex, params)) {
    reveal($("step3"));
    setStep(3);
    show($("renderProgress"));
    startPoll();
    toast("Re-rendering with new captions…");
  }
});

$("studioApplyAll").addEventListener("click", async () => {
  if (studioApplyingAll) return;
  const { params } = studioCtx;
  const mis = [...new Set(
    (renderRenders._list || []).map((r) => r.moment_index))];
  if (!mis.length) { toast("No clips yet", true); return; }
  studioApplyingAll = true;
  closeStudio();
  startPoll();
  let doneCount = 0;
  for (const mi of mis) {
    toast(`Applying captions to all clips (${doneCount + 1}/${mis.length})…`);
    if (!(await studioRenderOne(mi, params))) break;
    const t0 = Date.now();
    let okRender = false;
    while (Date.now() - t0 < 900000) {
      await new Promise((r2) => setTimeout(r2, 700));
      let st;
      try { st = await api.job_status(jobId); } catch (e) { continue; }
      if (st.render_error) { toast(st.render_error, true); break; }
      if (st.status === "done") { okRender = true; break; }
    }
    if (!okRender) break;
    doneCount++;
  }
  studioApplyingAll = false;
  if (doneCount === mis.length)
    toast(`Captions applied to all ${mis.length} clips`);
});

/* ---------------- Studio hub + tools ----------------
   The hub is home. Tool views follow the Library / Caption Remover
   overlay pattern: noteNav on entry, applyNav dispatches back. */
function backToStudio() {
  if (navStack.length) goBack();
  else applyNav({ view: "hub" });
}

function closeStudioViews() {
  hubOpen = false;
  hide($("hubView"));
  if (toolOpen) { hide($(TOOL_VIEWS[toolOpen])); toolOpen = null; }
  stopGenPoll();
  stopEnhancerPoll();
  const sb = $("studioBtn");
  if (sb) sb.classList.remove("on");
}

function closeLibraryRemoverRaw() {
  libraryOpen = false;
  removerOpen = false;
  hide($("libraryView"));
  hide($("removerView"));
  $("libraryBtn").classList.remove("on");
  $("removerBtn").classList.remove("on");
  if (removerTimer) { clearInterval(removerTimer); removerTimer = null; }
}

function showHub() {
  noteNav({ view: "hub" });
  closeLibraryRemoverRaw();
  if (toolOpen) { hide($(TOOL_VIEWS[toolOpen])); toolOpen = null; }
  stopGenPoll();
  stopEnhancerPoll();
  stopPoll();
  hubOpen = true;
  hide($("step1")); hide($("step2")); hide($("step3"));
  show($("hubView"));
  $("studioBtn").classList.add("on");
  refreshLocalAiStatus();
  startLocalAiPolling();
}

function showWizard(step) {
  step = step || 1;
  if (navNow.view === "wizard" && (navNow.step || 1) === step
      && !libraryOpen && !removerOpen && !hubOpen && !toolOpen) return;
  noteNav({ view: "wizard", step });
  applyNav({ view: "wizard", step });
}

function showTool(name) {
  noteNav({ view: name, step: lastWizardStep });
  closeLibraryRemoverRaw();
  hubOpen = false;
  hide($("hubView"));
  $("studioBtn").classList.remove("on");
  if (toolOpen && toolOpen !== name) hide($(TOOL_VIEWS[toolOpen]));
  stopPoll();
  stopGenPoll();
  stopEnhancerPoll();
  toolOpen = name;
  hide($("step1")); hide($("step2")); hide($("step3"));
  show($(TOOL_VIEWS[name]));
  if (name === "generator") initGenerator();
  else if (name === "titles")
    fillProjectSelect($("titlesProject"), "No project — topic only");
  else if (name === "transcript")
    fillProjectSelect($("transcriptProject"), "Pick a project…");
  else if (name === "enhancer")
    fillProjectSelect($("enhancerProject"), "Pick a project…");
}

$("studioBtn").addEventListener("click", goHome);
document.querySelectorAll(".tool-back").forEach((b) =>
  b.addEventListener("click", backToStudio));

/* hub: quick import mirrors the Step-1 controls exactly */
$("hubImportBtn").addEventListener("click", () => {
  const url = $("hubUrlInput").value.trim();
  if (!url) { toast("Paste a video link first", true); return; }
  $("urlInput").value = url;
  showWizard(1);
  $("importBtn").click();
});
$("hubFileBtn").addEventListener("click", () => {
  const p = $("hubPathInput").value.trim();
  if (!p) { toast("Paste a local video file path first", true); return; }
  $("localPathInput").value = p;
  showWizard(1);
  $("localFileBtn").click();
});

/* hub: tool cards */
document.querySelectorAll(".tool-card").forEach((card) => {
  card.addEventListener("click", () => {
    const tool = card.dataset.tool;
    if (tool === "clipmaker") showWizard(1);
    else if (tool === "remover") showRemover();
    else if (tool === "library") showLibrary();
    else if (tool === "captionstudio") {
      showLibrary();
      toast("Pick a clip in the Library, then open Caption Studio on it");
    } else if (tool === "settings") $("settingsBtn").click();
    else showTool(tool);
  });
});

/* hub: tool search filter */
$("hubSearch").addEventListener("input", () => {
  const q = $("hubSearch").value.trim().toLowerCase();
  let any = false;
  document.querySelectorAll(".tool-card").forEach((card) => {
    const hit = !q || (card.dataset.name || "").includes(q)
      || card.textContent.toLowerCase().includes(q);
    card.style.display = hit ? "" : "none";
    if (hit) any = true;
  });
  $("hubNoMatch").classList.toggle("hidden", any);
});

/* hub: Built-in AI download button (mirrors the Step-2 card) */
async function startAiDownload() {
  try {
    await fetch("/api/local-ai/download", { method: "POST" });
  } catch (e) { /* the status poll below will surface any problem */ }
  startLocalAiPolling();
  refreshLocalAiStatus();
}
$("hubAiDownloadBtn").addEventListener("click", startAiDownload);
document.querySelectorAll(".ai-download-btn").forEach((b) =>
  b.addEventListener("click", async () => {
    await startAiDownload();
    toast("Built-in AI download started — progress shows on the Studio home");
  }));

/* shared: fill a <select> with Library projects */
async function fillProjectSelect(sel, firstLabel) {
  if (!sel) return;
  const cur = sel.value;
  let projects = [];
  try {
    const res = await api.library();
    projects = res.projects || [];
  } catch (e) { /* keep the placeholder only */ }
  sel.innerHTML = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = firstLabel;
  sel.appendChild(first);
  projects.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.job_id;
    o.textContent = p.title || "Untitled video";
    sel.appendChild(o);
  });
  if (cur) sel.value = cur;
}

function copyText(text, okMsg) {
  const done = () => toast(okMsg || "Copied");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done)
      .catch(() => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); }
  catch (e) { toast("Copy failed — select and copy manually", true); }
  ta.remove();
}

/* ---------------- Studio: AI Video Generator (fal.ai) ---------------- */
let genModels = [];
let genTimer = null;
let genCurrentId = null;

function stopGenPoll() {
  if (genTimer) { clearInterval(genTimer); genTimer = null; }
}

async function initGenerator() {
  stopGenPoll();
  genCurrentId = null;
  hide($("genResult"));
  hide($("genError"));
  $("genStatusText").textContent = "";
  let res;
  try { res = await api.gen_models(); }
  catch (e) {
    $("genError").textContent = "Couldn't reach the local backend — is the app running?";
    show($("genError"));
    return;
  }
  genModels = res.models || [];
  const hasKey = !!res.has_key;
  $("genNoKey").classList.toggle("hidden", hasKey);
  $("genControls").classList.toggle("hidden", !hasKey);
  if (!hasKey) return;
  const sel = $("genModel");
  const cur = sel.value;
  sel.innerHTML = "";
  genModels.forEach((m) => {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = `${m.name} — ${m.price}`;
    sel.appendChild(o);
  });
  if (cur && genModels.some((m) => m.id === cur)) sel.value = cur;
  genModelChanged();
}

function genModelDef() {
  return genModels.find((m) => m.id === $("genModel").value)
    || genModels[0];
}

function fillGenSelect(sel, values, def) {
  sel.innerHTML = "";
  (values || []).forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v;
    sel.appendChild(o);
  });
  if (def) sel.value = def;
}

function genModelChanged() {
  const m = genModelDef();
  if (!m) return;
  $("genModelNote").textContent = `${m.note} Endpoint: ${m.endpoint} · billed by fal.ai to your account.`;
  fillGenSelect($("genAspect"), m.aspects || ["16:9"], m.def_aspect);
  $("genDurationWrap").style.display = m.durations ? "" : "none";
  if (m.durations) fillGenSelect($("genDuration"), m.durations, m.def_duration);
  $("genResolutionWrap").style.display = m.resolutions ? "" : "none";
  if (m.resolutions) fillGenSelect($("genResolution"), m.resolutions, m.def_resolution);
}
$("genModel").addEventListener("change", genModelChanged);
$("genOpenSettingsBtn").addEventListener("click", () => $("settingsBtn").click());

$("genRunBtn").addEventListener("click", async () => {
  const m = genModelDef();
  const prompt = $("genPrompt").value.trim();
  hide($("genError"));
  hide($("genResult"));
  if (!m) { toast("Pick a model first", true); return; }
  if (!prompt) { toast("Describe what to generate first", true); return; }
  $("genRunBtn").disabled = true;
  $("genStatusText").textContent = "Sending to fal.ai…";
  let res;
  try {
    res = await api.gen_video_start(
      m.id, prompt,
      $("genAspect").value,
      m.durations ? $("genDuration").value : "",
      m.resolutions ? $("genResolution").value : "");
  } catch (e) {
    res = { ok: false, error: "Couldn't reach the local backend." };
  }
  $("genRunBtn").disabled = false;
  if (!res.ok) {
    if (res.error === "no_key") {
      show($("genNoKey"));
      hide($("genControls"));
      $("genStatusText").textContent = "";
      return;
    }
    $("genError").textContent = res.message || res.error || "Generation failed.";
    show($("genError"));
    $("genStatusText").textContent = "";
    return;
  }
  genCurrentId = res.gen_id;
  show($("genProgress"));
  $("genStage").textContent = "In queue on fal.ai…";
  $("genStatusText").textContent = "Generating… this can take a few minutes.";
  stopGenPoll();
  genTimer = setInterval(pollGenStatus, 3000);
  pollGenStatus();
});

async function pollGenStatus() {
  if (!genCurrentId) { stopGenPoll(); return; }
  let st;
  try { st = await api.gen_video_status(genCurrentId); }
  catch (e) { return; }  // transient - next poll retries
  if (!st.ok) {
    stopGenPoll();
    hide($("genProgress"));
    $("genError").textContent = st.error || "Generation failed.";
    show($("genError"));
    $("genStatusText").textContent = "";
    return;
  }
  if (st.status === "queued" || st.status === "running") {
    $("genStage").textContent = st.stage || "Generating…";
    return;
  }
  stopGenPoll();
  hide($("genProgress"));
  $("genStatusText").textContent = "";
  if (st.status === "error") {
    $("genError").textContent = st.error || "fal.ai reported an error.";
    show($("genError"));
    return;
  }
  // done
  const isVideo = st.kind === "video";
  $("genVideo").classList.toggle("hidden", !isVideo);
  $("genImage").classList.toggle("hidden", isVideo);
  if (isVideo) $("genVideo").src = st.url || "";
  else $("genImage").src = st.url || "";
  $("genDownload").href = st.url || "#";
  $("genImportBtn").style.display = isVideo ? "" : "none";
  $("genResultHint").textContent = isVideo
    ? "Saved on this PC. Send it to the Library to clip it like any video."
    : "Generated image — download it for a thumbnail, poster or keyframe.";
  show($("genResult"));
  toast("Generation finished");
}

$("genImportBtn").addEventListener("click", async () => {
  if (!genCurrentId) return;
  $("genImportBtn").disabled = true;
  const res = await api.gen_video_import(genCurrentId);
  $("genImportBtn").disabled = false;
  if (!res.ok) {
    toast(res.error || "Couldn't send it to the Library", true);
    return;
  }
  toast("Added to your Library — transcribing now");
  showWizard(1);
  selectJob(res.job_id);
});

/* ---------------- Studio: Title & Hook Generator ---------------- */
function scoreChip(score) {
  const cls = score >= 70 ? "" : score >= 45 ? " mid" : " low";
  return `<span class="score-chip${cls}">${score}</span>`;
}
function renderTitleRows(items) {
  return (items || []).map((it) => `
    <div class="title-row">${scoreChip(it.score)}
      <span class="t-text">${escapeHtml(it.text)}</span>
      <button type="button" class="ghost-btn small t-copy">Copy</button>
    </div>`).join("");
}
$("titlesRunBtn").addEventListener("click", async () => {
  const topic = $("titlesTopic").value.trim();
  const job = $("titlesProject").value;
  hide($("titlesError"));
  hide($("titlesNeedModel"));
  $("titlesOut").innerHTML = "";
  $("titlesNote").textContent = "";
  if (!topic && !job) {
    toast("Type a topic or pick a Library project first", true);
    return;
  }
  $("titlesRunBtn").disabled = true;
  $("titlesStatus").textContent = "Writing… (the Built-in AI can take a minute)";
  let res;
  try { res = await api.ai_titles(topic, job); }
  catch (e) { res = { ok: false, error: "Couldn't reach the local backend." }; }
  $("titlesRunBtn").disabled = false;
  $("titlesStatus").textContent = "";
  if (!res.ok) {
    $("titlesError").textContent = res.error || "Title generation failed.";
    show($("titlesError"));
    return;
  }
  $("titlesNote").textContent = res.note || "";
  if (res.mode === "needs_model") {
    show($("titlesNeedModel"));
    return;
  }
  $("titlesOut").innerHTML =
    `<div class="field-label" style="margin-top:14px">Titles</div>`
    + renderTitleRows(res.titles)
    + `<div class="field-label" style="margin-top:16px">Hooks</div>`
    + renderTitleRows(res.hooks);
  $("titlesOut").querySelectorAll(".title-row").forEach((row, i) => {
    const items = [...(res.titles || []), ...(res.hooks || [])];
    row.querySelector(".t-copy").addEventListener("click", () =>
      copyText(items[i].text, "Copied"));
  });
});

/* ---------------- Studio: AI Scriptwriter ---------------- */
$("scriptRunBtn").addEventListener("click", async () => {
  const topic = $("scriptTopic").value.trim();
  hide($("scriptError"));
  hide($("scriptNeedModel"));
  hide($("scriptOutWrap"));
  if (!topic) { toast("Describe the video first", true); return; }
  $("scriptRunBtn").disabled = true;
  $("scriptStatus").textContent = "Writing… (the Built-in AI can take a minute)";
  let res;
  try {
    res = await api.ai_script(topic, $("scriptTone").value,
      parseInt($("scriptLen").value, 10) || 60);
  } catch (e) { res = { ok: false, error: "Couldn't reach the local backend." }; }
  $("scriptRunBtn").disabled = false;
  $("scriptStatus").textContent = "";
  if (!res.ok) {
    if (res.error === "needs_model") { show($("scriptNeedModel")); return; }
    $("scriptError").textContent = res.message || res.error || "Script failed.";
    show($("scriptError"));
    return;
  }
  $("scriptOut").textContent = res.script;
  show($("scriptOutWrap"));
});
$("scriptCopyBtn").addEventListener("click", () =>
  copyText($("scriptOut").textContent, "Script copied"));

/* ---------------- Studio: Transcript Tool ---------------- */
$("transcriptLoadBtn").addEventListener("click", async () => {
  const job = $("transcriptProject").value;
  hide($("transcriptError"));
  if (!job) { toast("Pick a Library project first", true); return; }
  const res = await api.get_transcript(job);
  if (!res.ok) {
    $("transcriptError").textContent = res.error || "No transcript.";
    show($("transcriptError"));
    hide($("transcriptOutWrap"));
    return;
  }
  $("transcriptText").value = (res.segments || [])
    .map((s) => s.text).join("\n");
  $("transcriptMeta").textContent =
    `${res.title || "Video"} · ${res.word_count} words · ${fmtTime(res.duration || 0)}`;
  show($("transcriptOutWrap"));
});
$("transcriptCopyBtn").addEventListener("click", () =>
  copyText($("transcriptText").value, "Transcript copied"));
document.querySelectorAll(".transcript-dl").forEach((b) =>
  b.addEventListener("click", async () => {
    const job = $("transcriptProject").value;
    if (!job) { toast("Load a transcript first", true); return; }
    const res = await api.export_transcript(job, b.dataset.fmt);
    if (!res.ok) { toast(res.error || "Export failed", true); return; }
    downloadUrl(res.url);
  }));
$("transcriptSrtBtn").addEventListener("click", async () => {
  const job = $("transcriptProject").value;
  const p = $("transcriptSrtPath").value.trim();
  if (!job) { toast("Pick a Library project first", true); return; }
  if (!p) { toast("Paste an .srt file path first", true); return; }
  const res = await api.import_srt_transcript(job, p);
  if (res.ok) {
    toast(`Transcript replaced (${res.word_count} words)`);
    $("transcriptLoadBtn").click();
  } else toast(res.error || "SRT failed", true);
});

/* ---------------- Studio: Video Enhancer ---------------- */
let enhancerTimer = null;
let enhancerCurrentId = null;

function stopEnhancerPoll() {
  if (enhancerTimer) { clearInterval(enhancerTimer); enhancerTimer = null; }
}
$("enhancerRunBtn").addEventListener("click", async () => {
  const path = $("enhancerPath").value.trim();
  const job = $("enhancerProject").value;
  hide($("enhancerError"));
  hide($("enhancerResult"));
  if (!path && !job) {
    toast("Pick a Library project or paste a file path first", true);
    return;
  }
  $("enhancerRunBtn").disabled = true;
  const res = await api.enhance_start(path, job);
  $("enhancerRunBtn").disabled = false;
  if (!res.ok) {
    $("enhancerError").textContent = res.error || "Enhance failed to start.";
    show($("enhancerError"));
    return;
  }
  enhancerCurrentId = res.id;
  show($("enhancerProgress"));
  $("enhancerBar").style.width = "0%";
  $("enhancerStage").textContent = "Starting…";
  $("enhancerStatusText").textContent = "Enhancing…";
  stopEnhancerPoll();
  enhancerTimer = setInterval(pollEnhancer, 1000);
  pollEnhancer();
});
async function pollEnhancer() {
  if (!enhancerCurrentId) { stopEnhancerPoll(); return; }
  let st;
  try { st = await api.enhance_status(enhancerCurrentId); }
  catch (e) { return; }
  if (!st.ok) {
    stopEnhancerPoll();
    hide($("enhancerProgress"));
    $("enhancerError").textContent = st.error || "Enhance failed.";
    show($("enhancerError"));
    $("enhancerStatusText").textContent = "";
    return;
  }
  if (st.status === "running") {
    $("enhancerBar").style.width = (st.pct || 0) + "%";
    $("enhancerStage").textContent = st.stage || "Enhancing…";
    return;
  }
  stopEnhancerPoll();
  hide($("enhancerProgress"));
  $("enhancerStatusText").textContent = "";
  if (st.status === "error") {
    $("enhancerError").textContent = st.error || "Enhance failed.";
    show($("enhancerError"));
    return;
  }
  $("enhancerVideo").src = st.url || "";
  $("enhancerDownload").href = st.url || "#";
  show($("enhancerResult"));
  toast("Enhance finished");
}
$("enhancerFolderBtn").addEventListener("click", async () => {
  if (enhancerCurrentId) await api.enhance_open_folder(enhancerCurrentId);
});

/* ---------------- settings ---------------- */
function openSettings() {
  show($("settingsModal"));
  $("settingsBtn").classList.add("on");
}
function closeSettings() {
  hide($("settingsModal"));
  $("settingsBtn").classList.remove("on");
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape"
      && !$("settingsModal").classList.contains("hidden"))
    closeSettings();
});
$("settingsModal").addEventListener("click", (e) => {
  if (e.target === $("settingsModal")) closeSettings();  // backdrop click
});
$("settingsBtn").addEventListener("click", async () => {
  try {
    const s = await api.get_settings();
    settings = s || {};
  } catch (e) { settings = settings || {}; }
  segSet("themeSeg", settings.theme === "light" ? "light" : "dark");
  segSet("windowSeg", settings.window_mode === "chrome" ? "chrome" : "own");
  $("accentPick").value = settings.accent || "#2effc8";
  // fal.ai key: the field always starts EMPTY (the key is never sent
  // back to the page) - the state line says whether one is saved.
  $("falKeyInput").value = "";
  $("falKeyState").textContent = settings.fal_key_set
    ? "A key is saved on this PC. Type a new one to replace it."
    : "No key saved.";
  openSettings();
});
$("closeSettingsBtn").addEventListener("click", closeSettings);
$("saveSettingsBtn").addEventListener("click", async () => {
  const fk = $("falKeyInput").value.trim();
  if (fk) {
    try {
      await api.save_settings({ fal_key: fk });
      settings.fal_key_set = true;
    } catch (e) { /* surfaced below as a generic save */ }
    $("falKeyInput").value = "";
  }
  closeSettings();
  toast("Settings saved");
});
$("falKeyClearBtn").addEventListener("click", async () => {
  try { await api.save_settings({ fal_key: "" }); } catch (e) { /* noop */ }
  settings.fal_key_set = false;
  $("falKeyInput").value = "";
  $("falKeyState").textContent = "No key saved.";
  toast("fal.ai key removed");
});

/* ---------------- theme (Dark default = web-app look) ---------------- */
function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return null;
  return [parseInt(m[1].slice(0, 2), 16), parseInt(m[1].slice(2, 4), 16),
          parseInt(m[1].slice(4, 6), 16)];
}
function applyTheme() {
  const theme = settings.theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  const accent = (settings.accent || "#2effc8").trim() || "#2effc8";
  const rgb = hexToRgb(accent) || [46, 255, 200];
  const root = document.documentElement.style;
  root.setProperty("--accent", accent);
  root.setProperty("--accent-rgb", rgb.join(", "));
  const d2 = rgb.map((c) => Math.round(c * 0.78));
  root.setProperty("--accent2", `rgb(${d2.join(", ")})`);
  root.setProperty("--accent-dim", `rgba(${rgb.join(", ")}, 0.10)`);
  root.setProperty("--accent-glow", `rgba(${rgb.join(", ")}, 0.22)`);
  markActiveTheme();
}
function markActiveTheme() {
  const cur = (settings.accent || "#2effc8").toLowerCase();
  document.querySelectorAll("#themeGrid .theme-swatch").forEach((b) =>
    b.classList.toggle("on", b.dataset.accent.toLowerCase() === cur));
}
document.querySelectorAll("#themeGrid .theme-swatch").forEach((b) =>
  b.addEventListener("click", async () => {
    settings.accent = b.dataset.accent;
    $("accentPick").value = b.dataset.accent;
    applyTheme();
    try { await api.save_settings({ accent: settings.accent }); } catch (e) { /* offline */ }
  }));
document.querySelectorAll("#themeSeg .seg").forEach((b) =>
  b.addEventListener("click", async () => {
    segSet("themeSeg", b.dataset.value);
    settings.theme = b.dataset.value;
    applyTheme();
    try { await api.save_settings({ theme: settings.theme }); } catch (e) { /* offline */ }
  }));
document.querySelectorAll("#windowSeg .seg").forEach((b) =>
  b.addEventListener("click", async () => {
    segSet("windowSeg", b.dataset.value);
    settings.window_mode = b.dataset.value;
    try { await api.save_settings({ window_mode: settings.window_mode }); } catch (e) { /* offline */ }
  }));
$("accentPick").addEventListener("input", async () => {
  settings.accent = $("accentPick").value;
  applyTheme();
  try { await api.save_settings({ accent: settings.accent }); } catch (e) { /* offline */ }
});
$("accentReset").addEventListener("click", async () => {
  settings.accent = "#2effc8";
  $("accentPick").value = "#2effc8";
  applyTheme();
  try { await api.save_settings({ accent: settings.accent }); } catch (e) { /* offline */ }
});

/* ---------------- modals: Esc + backdrop click ---------------- */
function closeModal(id) {
  if (id === "previewModal") {
    $("previewVideo").pause();
    $("previewVideo").removeAttribute("src");
  }
  hide($(id));
}
["captionModal", "previewModal", "settingsModal"].forEach((id) => {
  $(id).addEventListener("click", (e) => { if (e.target.id === id) closeModal(id); });
});
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  ["captionModal", "previewModal", "settingsModal"].forEach((id) => {
    if (!$(id).classList.contains("hidden")) closeModal(id);
  });
});

/* ---------------- init ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  setStep(1);
  try {
    const opts = await api.get_options();
    const sel = $("langSelect");
    (opts.languages || [["auto", "Auto-detect"]]).forEach(([code, label]) => {
      const o = document.createElement("option");
      o.value = code; o.textContent = label;
      sel.appendChild(o);
    });
    if (opts.genre_defaults) genreDefaults = opts.genre_defaults;
    settings = await api.get_settings();
    sel.value = settings.language || "auto";
    $("cookiesCheck").checked = !!settings.use_cookies;
  } catch (e) { /* offline-safe defaults below */ }
  applyTheme();
  updateBackBtn();
  refreshLocalAiStatus();
  startLocalAiPolling();
  setTimeout(() => {
    const sp = document.getElementById("splash");
    if (sp) { sp.classList.add("gone"); setTimeout(() => sp.remove(), 750); }
  }, 1550);
});
