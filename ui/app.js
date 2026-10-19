/* ClipMaker PC v3 - full Clip Maker web-app parity UI.
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
let clipLen = "60s";
let ratio = "9:16";
let quality = "1080p";
let clipCount = 8;
let genre = "";

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
  refreshQueue();
}

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

function initControls() {
  // reflect saved settings into every control
  segSet("detectModeSeg", settings.api_key ? "ai" : "offline");
  syncDetectModeSub(segGet("detectModeSeg"));
  segSet("clipCountSeg", String(settings.clip_count || 8));
  clipCount = parseInt(segGet("clipCountSeg"), 10) || 8;
  ratio = settings.ratio || "9:16";
  document.querySelectorAll("#ratioPicker .ratio-card").forEach((c) =>
    c.classList.toggle("on", c.dataset.value === ratio));
  clipLen = settings.clip_length || "60s";
  if (clipLen.startsWith("custom:")) {
    $("customLenInput").value = clipLen.split(":")[1] || "";
    document.querySelectorAll("#clipLenPills .seg").forEach((p) => p.classList.remove("on"));
  } else {
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
  segSet("qualitySeg", settings.quality || "1080p");
  quality = segGet("qualitySeg");
}

function syncDetectModeSub(mode) {
  $("detectModeSub").textContent = mode === "offline"
    ? "Offline mode scans the transcript on your PC — no key, no internet. Scores are heuristic (energy + speech density), not AI."
    : "AI mode needs your free Gemini API key (Settings). Every clip is scored 0–100 for virality.";
}

function currentClipLen() {
  const custom = $("customLenInput").value.trim();
  if (custom && /^\d+$/.test(custom)) return "custom:" + custom;
  return clipLen;
}

document.querySelectorAll("#detectModeSeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("detectModeSeg", b.dataset.value);
    syncDetectModeSub(b.dataset.value);
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
  }));
$("customLenInput").addEventListener("input", () => {
  if ($("customLenInput").value.trim())
    document.querySelectorAll("#clipLenPills .seg").forEach((p) => p.classList.remove("on"));
});
document.querySelectorAll("#qualitySeg .seg").forEach((b) =>
  b.addEventListener("click", () => {
    segSet("qualitySeg", b.dataset.value);
    quality = b.dataset.value;
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
    : await api.detect(jobId, clipCount, len);
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
    ratio, quality,
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
  if (!jobId) return;
  toast("Building ZIP…");
  const res = await api.make_zip(jobId, true);
  if (res.ok && res.url) { downloadUrl(res.url); toast("ZIP downloaded"); }
  else toast(res.error || "ZIP failed", true);
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

/* ---------------- settings ---------------- */
function openSettings() { show($("settingsModal")); }
$("settingsBtn").addEventListener("click", async () => {
  try {
    const s = await api.get_settings();
    settings = s || {};
  } catch (e) { settings = settings || {}; }
  $("apiKeyInput").value = settings.api_key || "";
  $("apiKeyInput").type = "password";
  $("keyToggle").textContent = "Show";
  segSet("themeSeg", settings.theme === "light" ? "light" : "dark");
  $("accentPick").value = settings.accent || "#2effc8";
  openSettings();
});
$("closeSettingsBtn").addEventListener("click", () => hide($("settingsModal")));
$("saveSettingsBtn").addEventListener("click", async () => {
  await api.save_settings({ api_key: $("apiKeyInput").value.trim() });
  settings.api_key = $("apiKeyInput").value.trim();
  hide($("settingsModal"));
  toast("Settings saved");
});
$("keyToggle").addEventListener("click", () => {
  const inp = $("apiKeyInput");
  const showing = inp.type === "text";
  inp.type = showing ? "password" : "text";
  $("keyToggle").textContent = showing ? "Show" : "Hide";
});
$("keyHelp").addEventListener("click", (e) => {
  e.preventDefault();
  toast("Get a free key at aistudio.google.com/app/apikey");
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
}
document.querySelectorAll("#themeSeg .seg").forEach((b) =>
  b.addEventListener("click", async () => {
    segSet("themeSeg", b.dataset.value);
    settings.theme = b.dataset.value;
    applyTheme();
    try { await api.save_settings({ theme: settings.theme }); } catch (e) { /* offline */ }
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
  setTimeout(() => {
    const sp = document.getElementById("splash");
    if (sp) { sp.classList.add("gone"); setTimeout(() => sp.remove(), 600); }
  }, 900);
});
