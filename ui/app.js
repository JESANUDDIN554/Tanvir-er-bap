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
let lastLimitNote = "";
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
let category = "general";
let translateLangs = [["bn", "Bengali"], ["es", "Spanish"], ["hi", "Hindi"],
  ["ar", "Arabic"], ["pt", "Portuguese"], ["fr", "French"], ["de", "German"],
  ["ur", "Urdu"]];
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
  hookmaker: "hookmakerView", aimanager: "aiManagerView",
  publishing: "publishingView",
  admin: "adminView",
};

/* Opus-style shell: the sidebar active pill + topbar title follow the
   current view. Pure presentation - navigation itself is unchanged. */
const VIEW_TITLES = {
  hub: "Home", wizard: "Clip Maker", library: "Library",
  remover: "Caption Remover", generator: "AI Video Generator",
  titles: "Title & Hook Generator", script: "AI Scriptwriter",
  transcript: "Transcript Tool", enhancer: "Video Enhancer",
  hookmaker: "Hook Maker", captionstudio: "Caption Studio",
  aimanager: "AI Engine", admin: "Admin Panel",
  publishing: "Publishing",
};
function syncSidebar(view) {
  document.querySelectorAll(".side-item").forEach((b) =>
    b.classList.toggle("on", b.dataset.nav === view));
  const t = $("viewTitle");
  if (t && VIEW_TITLES[view]) t.textContent = VIEW_TITLES[view];
  document.body.dataset.view = view;
}

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
function show(el) {
  if (!el) return;
  const wasHidden = el.classList.contains("hidden");
  el.classList.remove("hidden");
  if (wasHidden) animateIn(el);
}
function hide(el) { el.classList.add("hidden"); }

/* Entrance animation for everything that opens: every view (.step-card),
   every modal, and the Studio hub's tool cards (staggered). Pure CSS
   transform/opacity (see .anim-in in style.css); re-triggered by
   re-adding the class, so it replays on every open and never blocks
   clicks or workflows. Honored by prefers-reduced-motion in the CSS. */
function animateIn(el) {
  if (el.classList.contains("step-card") || el.classList.contains("modal")) {
    el.classList.remove("anim-in");
    void el.offsetWidth;
    el.classList.add("anim-in");
  }
  if (el.id === "hubView") {
    el.querySelectorAll(".tool-card").forEach((card, i) => {
      card.style.animationDelay = Math.min(i * 45, 400) + "ms";
      card.classList.remove("anim-in");
      void card.offsetWidth;
      card.classList.add("anim-in");
    });
  }
}

/* Stagger the entrance of dynamically rendered card lists (Library grid,
   Home recents): the cards carry the CSS animation; this sets delays. */
function staggerCards(container, selector) {
  if (!container) return;
  container.querySelectorAll(selector).forEach((card, i) => {
    card.style.animationDelay = Math.min(i * 55, 440) + "ms";
  });
}

function reveal(el) { show(el); }

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
    if (st.category && $("categorySel")
        && document.activeElement !== $("categorySel"))
      $("categorySel").value = st.category;
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
  if (st.limit_note && st.limit_note !== lastLimitNote) {
    // A usage-limit outcome is not a render failure: show the
    // note itself (how many clips were made vs stopped) and
    // refresh the sidebar allowance chip.
    lastLimitNote = st.limit_note;
    hide($("renderProgress"));
    $("renderError").textContent = st.limit_note;
    show($("renderError"));
    toast(st.limit_note);
    refreshClipsChip();
  }
  if (!st.limit_note) lastLimitNote = "";
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
  const tr = st.translate || {};
  const sig = JSON.stringify(renders.map((r) =>
    [r.path, r.translations || null])) + "|tr:"
    + [tr.busy ? 1 : 0, tr.lang || "", tr.render_idx ?? "",
       tr.error || "", tr.done ? 1 : 0].join(":");
  if (sig === lastRailSig) return;
  lastRailSig = sig;
  const list = $("liveRailList");
  list.innerHTML = "";
  if (renders.length) hide($("liveRailEmpty")); else show($("liveRailEmpty"));
  const thumb = thumbLoadedFor === jobId
    ? $("videoThumb").getAttribute("src") : null;
  renders.forEach((r, ri) => {
    const div = document.createElement("div");
    div.className = "rail-clip";
    div._render = r;
    let dur = "";
    if (r.merged_duration) dur = fmtTime(r.merged_duration);
    else {
      const m = (lastMoments || [])[r.moment_index];
      if (m) dur = fmtTime(Math.max(0, Math.round(m.end - m.start)));
    }
    const trBusy = tr.busy && tr.render_idx === ri;
    const trLinks = Object.entries(r.translations || {}).map(([lg, t]) =>
      `<button class="ghost-btn small rc-tr-dl" data-lg="${lg}" data-kind="srt" type="button">${lg.toUpperCase()} SRT</button>
       <button class="ghost-btn small rc-tr-dl" data-lg="${lg}" data-kind="vtt" type="button">${lg.toUpperCase()} VTT</button>`).join("");
    div.innerHTML = `${thumb ? `<img src="${thumb}" alt="">` : ""}
      <div class="rc-text">
        <div class="rc-title">${escapeHtml(r.title || "Clip")}</div>
        <div class="rc-meta">${dur ? dur + " · " : ""}virality ${r.virality ?? "—"}${r.aspect ? " · " + escapeHtml(r.aspect) : ""}${r.reframe ? " · " + escapeHtml(r.reframe) : ""}${r.audio_polish ? " · 🔊 −14 LUFS" : ""}${r.encoder ? " · ⚙ " + escapeHtml(r.encoder) : ""}${r.broll ? ` · 🎞 B-roll: ${escapeHtml(r.broll.keyword || "stock")}` : ""}</div>
        <div class="rc-translate">
          <select class="rc-lang" aria-label="Caption language">${translateLangs.map(([c, l]) => `<option value="${c}">${l}</option>`).join("")}</select>
          <button class="ghost-btn small rc-tr" type="button" ${trBusy ? "disabled" : ""}>${trBusy ? "Translating…" : "🌐 Translate"}</button>
          ${trLinks}
        </div>
        ${tr.render_idx === ri && tr.error ? `<div class="rc-tr-err">${escapeHtml(tr.error)}</div>` : ""}
        ${r.teaser_note ? `<div class="rc-tr-note">${escapeHtml(r.teaser_note)}</div>` : ""}
      </div>
      <button class="ghost-btn small rc-dl" type="button">Download</button>
      <button class="ghost-btn small rc-cap" type="button">Captions</button>
      <button class="ghost-btn small rc-cover" type="button">Cover</button>`;
    div.querySelector(".rc-tr").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const lang = div.querySelector(".rc-lang").value;
      btn.disabled = true;
      btn.textContent = "Translating…";
      const res = await api.translate_captions(jobId, ri, lang);
      if (!res.ok) {
        btn.disabled = false;
        btn.textContent = "🌐 Translate";
        toast(res.message || res.error || "Translation failed", true);
        return;
      }
      toast("Translating captions with the Built-in AI…");
      watchTranslate();
    });
    div.querySelectorAll(".rc-tr-dl").forEach((b) =>
      b.addEventListener("click", () => {
        const t = (r.translations || {})[b.dataset.lg];
        if (t && t[b.dataset.kind]) downloadUrl(t[b.dataset.kind]);
      }));
    div.querySelector(".rc-dl").addEventListener("click", () => {
      if (r.url) downloadUrl(r.url);
    });
    div.querySelector(".rc-cover").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      const old = btn.textContent;
      btn.textContent = "Making…";
      const res = await api.make_cover(jobId, ri, r.title || "");
      btn.disabled = false;
      btn.textContent = old;
      if (res.ok && res.cover) {
        toast("Cover image saved — downloading");
        downloadUrl(res.cover.url);
      } else {
        toast(res.error || "Cover failed", true);
      }
    });
    div.querySelector(".rc-cap").addEventListener("click", () => {
      openCaptionStudio(r);
    });
    list.appendChild(div);
  });
}

/* Watch a caption translation to completion, then refresh the
   rail so the translated SRT/VTT buttons appear. */
async function watchTranslate() {
  for (let i = 0; i < 900; i++) {
    await new Promise((r) => setTimeout(r, 1200));
    let st;
    try { st = await api.job_status(jobId); } catch (e) { return; }
    const tr = st.translate || {};
    if (!tr.busy) {
      lastRailSig = "";
      renderLiveRail(st);
      if (tr.error) toast("Translation failed: " + tr.error, true);
      else if (tr.done) toast("Translated captions ready — use the SRT/VTT buttons on the clip");
      return;
    }
  }
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
  $("zoomBeatsCheck").checked = !!settings.emphasis_zoom;
  $("zoomLevelSel").value = settings.emphasis_level || "gentle";
  $("brollCheck").checked = !!settings.broll;
  $("enhanceCheck").checked = !!settings.enhance_clarity;
  $("audioPolishCheck").checked = !!settings.audio_polish;
  $("denoiseAudioCheck").checked = !!settings.denoise_audio;
  category = settings.category || "general";
  if ($("categorySel")) $("categorySel").value = category;
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
  // EVERY mirror (Step-2 card, hub card, Settings card, sidebar
  // mini-card) is updated for every state first; polling stops for
  // the ready state only at the very end. An early return in the
  // ready branch once skipped all the mirrors below, freezing them
  // at their placeholders - the sidebar sat on "Checking…" forever
  // when the first poll of a session resolved to ready.
  if (st.ready) {
    txt.textContent = "Built-in AI ready ✓ — the model is on your PC and works fully offline.";
    hide(btn); hide(barWrap); pct.textContent = "";
  } else if (st.downloading) {
    hide(btn); show(barWrap);
    bar.style.width = (st.pct || 0) + "%";
    pct.textContent = (st.stage || "Downloading…") + " — " + Math.round(st.pct || 0) + "%";
    // A new attempt replaces any stale failure message from the last one.
    txt.textContent = "First use needs a one-time download of the AI model (~9 GB). After that it works fully offline, forever.";
  } else {
    show(btn); hide(barWrap); pct.textContent = "";
    if (st.error) {
      txt.textContent = "Download failed: " + st.error + " Check your connection and try again.";
      if (st.pct > 0) {
        // The partial download is kept: say it stopped and the next
        // click resumes, rather than leaving the line blank or stale.
        pct.textContent = (st.stage && st.stage.indexOf("Download stopped") === 0)
          ? st.stage
          : "Download stopped at " + Math.round(st.pct) + "% - click Download to resume";
      }
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
      // A new attempt replaces any stale failure message from the last one.
      hTxt.textContent = "First use needs a one-time download of the AI model (~9 GB). After that it works fully offline, forever.";
    } else {
      show(hBtn); hide(hWrap); hPct.textContent = "";
      if (st.error) {
        hTxt.textContent = "Download failed: " + st.error + " Check your connection and try again.";
        if (st.pct > 0) {
          // The partial download is kept: say it stopped and the next
          // click resumes, rather than leaving the line blank or stale.
          hPct.textContent = (st.stage && st.stage.indexOf("Download stopped") === 0)
            ? st.stage
            : "Download stopped at " + Math.round(st.pct) + "% - click Download to resume";
        }
      }
    }
  }
  // Settings card + sidebar mini-card mirror the same Built-in AI state.
  const sBtn = $("settingsAiDownloadBtn"), sTxt = $("settingsAiText"),
    sWrap = $("settingsAiBarWrap"), sBar = $("settingsAiBar"),
    sPct = $("settingsAiPct");
  if (sBtn && sTxt) {
    if (st.ready) {
      sTxt.textContent = "Built-in AI ready ✓ — the model is on your PC and works fully offline.";
      hide(sBtn); hide(sWrap); sPct.textContent = "";
    } else if (st.downloading) {
      hide(sBtn); show(sWrap);
      sBar.style.width = (st.pct || 0) + "%";
      sPct.textContent = (st.stage || "Downloading…") + " — " + Math.round(st.pct || 0) + "%";
      sTxt.textContent = "First use needs a one-time download of the AI model (~9 GB). After that it works fully offline, forever.";
    } else {
      show(sBtn); hide(sWrap); sPct.textContent = "";
      if (st.error) {
        sTxt.textContent = "Download failed: " + st.error + " Check your connection and try again.";
        if (st.pct > 0) {
          sPct.textContent = (st.stage && st.stage.indexOf("Download stopped") === 0)
            ? st.stage
            : "Download stopped at " + Math.round(st.pct) + "% - click Download to resume";
        }
      }
    }
  }
  const sideTxt = $("sideAiText"), sideDot = $("sideAiDot");
  if (sideTxt && sideDot) {
    if (st.ready) {
      sideTxt.textContent = "Ready — works offline";
      sideDot.className = "side-ai-dot ready";
    } else if (st.downloading) {
      sideTxt.textContent = "Downloading… " + Math.round(st.pct || 0) + "%";
      sideDot.className = "side-ai-dot busy";
    } else if (st.error) {
      sideTxt.textContent = st.pct > 0
        ? "Stopped at " + Math.round(st.pct) + "% — resume in Settings"
        : "Download stopped — see Settings";
      sideDot.className = "side-ai-dot err";
    } else {
      sideTxt.textContent = "Not downloaded (~9 GB)";
      sideDot.className = "side-ai-dot";
    }
  }
  // All mirrors now show the current state; only then may the
  // ready state stop the polling that feeds them.
  if (st.ready) stopLocalAiPolling();
}

$("localAiDownloadBtn").addEventListener("click", async () => {
  try {
    await fetch("/api/local-ai/download", { method: "POST" });
  } catch (e) { /* the status poll below will surface any problem */ }
  startLocalAiPolling();
  refreshLocalAiStatus();
});

/* ---------------- license clip allowance chip ----------------
   Shown ONLY for keys with a signed clip limit: "Clips left: N"
   under the Built-in AI sidebar card. Unlimited keys (and the
   locked activation state) never see it. A tampered usage
   record shows its own honest label instead of a number. */
async function refreshClipsChip() {
  const chip = $("clipsLeftChip"), num = $("clipsLeftNum");
  if (!chip || !num) return;
  let u;
  try { u = await api.usage_status(); } catch (e) { return; }
  if (!u || !u.ok || !u.capped) { hide(chip); return; }
  chip.classList.remove("low", "out");
  if (u.state === "tampered") {
    num.textContent = "usage data problem";
    chip.classList.add("out");
  } else {
    num.textContent = String(u.remaining ?? "–");
    if ((u.remaining ?? 0) <= 0) chip.classList.add("out");
    else if ((u.remaining ?? 0) <= 5) chip.classList.add("low");
  }
  chip.title = "Clips remaining on this license key"
    + (u.state === "tampered" ? "" :
      ` — ${u.used ?? 0} of ${u.max_clips} used`);
  show(chip);
}
refreshClipsChip();
setInterval(refreshClipsChip, 30000);

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
  const cat = $("categorySel") ? $("categorySel").value : "general";
  const res = mode === "offline"
    ? await api.detect_offline(jobId, clipCount, len, cat)
    : await api.detect_local(jobId, clipCount, len, cat);
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
/* Score-explanation chips: measured transcript signals behind a
   moment's scores (pace / energy / hooks / novelty + penalties). */
function sigChips(m) {
  const sigs = (m && m.signals) || [];
  if (!sigs.length) return "";
  return `<div class="sig-chips">` + sigs.map((s) =>
    `<span class="sig-chip${s.neg ? " neg" : ""}" title="${escapeHtml(s.detail || "")}">${escapeHtml(s.label)} <b>${s.score > 0 ? "+" : ""}${s.score}</b></span>`
  ).join("") + `</div>`;
}
function bar(label, v) {
  const cls = v >= 80 ? "" : v >= 60 ? "mid" : "low";
  return `<div class="vrow"><span>${label}</span>
    <div class="vbar"><div class="vfill ${cls}" data-w="${v}" style="width:0%"></div></div>
    <b>${v}</b></div>`;
}
/* Phase 1: multi-dimensional score bars + platform tag. Shown when
   the moment carries subscores (new detections); older projects
   keep the legacy Hook/Flow/Value bars. */
function scoreBars(m) {
  const s = m && m.scores;
  if (!s) return bar("Hook", m.hook_score ?? m.virality)
    + bar("Flow", m.flow_score ?? m.virality)
    + bar("Value", m.value_score ?? m.virality);
  const rs = m.score_reasons || {};
  const sbar = (label, v, why) =>
    `<div class="vrow" title="${escapeHtml(why || "")}"><span>${label}</span>
    <div class="vbar"><div class="vfill ${v >= 80 ? "" : v >= 60 ? "mid" : "low"}" data-w="${v}" style="width:0%"></div></div>
    <b>${v}</b></div>`;
  return sbar("Hook", s.hook, rs.hook) + sbar("Keep", s.retention, rs.retention)
    + sbar("Emot", s.emotion, rs.emotion) + sbar("Story", s.story, rs.story);
}
function platformTag(m) {
  const p = m && m.platform;
  if (!p || !p.name) return "";
  return `<span class="platform-tag" title="${escapeHtml(p.why || "")}">📱 ${escapeHtml(p.name)}</span>`;
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
      <div class="moment-thumb"><img src="/files/${jobId}/thumb.jpg" alt="" loading="lazy" onerror="this.remove()"><span class="moment-dur">${Math.round(m.end - m.start)}s</span></div>
      <div class="moment-body">
        <div class="moment-title">${escapeHtml(m.title)}${tags} ${platformTag(m)}</div>
        <div class="moment-meta">${fmtTime(m.start)} → ${fmtTime(m.end)} · ${Math.round(m.end - m.start)}s</div>
        <div class="moment-hook">${escapeHtml(m.hook)}</div>
        <div class="vbreak">
          ${scoreBars(m)}
        </div>
        <div class="moment-reason">${escapeHtml(m.reason)}</div>
        ${sigChips(m)}
        <button class="ghost-btn small editCapBtn" data-i="${i}">Edit captions / trim</button>
        <button class="ghost-btn small pkgBtn" data-i="${i}">📝 Posting package</button>
        <div class="pkg-panel hidden" data-i="${i}"></div>
      </div>
      <div class="vbadge ${badgeClass(m.virality)}" style="--p:${m.virality}">
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
    emphasis_zoom: $("zoomBeatsCheck").checked,
    emphasis_level: $("zoomLevelSel").value,
    broll: $("brollCheck").checked,
    enhance_clarity: $("enhanceCheck").checked,
    audio_polish: $("audioPolishCheck").checked,
    denoise_audio: $("denoiseAudioCheck").checked,
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
$("categorySel").addEventListener("change", async () => {
  category = $("categorySel").value;
  settings.category = category;
  try { await api.save_settings({ category }); } catch (e) { /* offline */ }
});

async function startRender(indices) {
  if (!indices.length) { toast("Select at least one moment", true); return; }
  hide($("renderError"));
  const res = await api.render(jobId, indices, collectRenderOptions());
  if (!res.ok) { toast(res.message || res.error || "Render failed", true); return; }
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
        <button class="ghost-btn small enhCardBtn" data-i="${i}">Enhance</button>
        <button class="ghost-btn small pubCardBtn" data-i="${i}">📤 Publish</button>
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
  box.querySelectorAll(".enhCardBtn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await refreshEnhancePanel();
      $("enhancePanel").scrollIntoView({ behavior: "smooth", block: "center" });
      startEnhanceBatch([parseInt(btn.dataset.i, 10)]);
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
  box.querySelectorAll(".pubCardBtn").forEach((btn) =>
    btn.addEventListener("click", () =>
      openPublishFor(jobId, parseInt(btn.dataset.i, 10))));
  renderRenders._list = renders;
  refreshPlanner();
  refreshEnhancePanel();
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
  staggerCards(grid, ".lib-card");
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
      syncSidebar("wizard");
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
  syncSidebar("library");
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
  syncSidebar("remover");
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
  $("studioEmoji").checked = !!p.auto_emoji;
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
$("studioEmoji").addEventListener("change", () => {
  studioCtx.params.auto_emoji = $("studioEmoji").checked;
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
  stopHookPoll();
  stopAiPoll();
  stopPubPoll();
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
  stopHookPoll();
  stopAiPoll();
  stopPubPoll();
  stopPoll();
  hubOpen = true;
  hide($("step1")); hide($("step2")); hide($("step3"));
  show($("hubView"));
  $("studioBtn").classList.add("on");
  refreshLocalAiStatus();
  startLocalAiPolling();
  syncSidebar("hub");
  refreshHomeRecents();
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
  stopHookPoll();
  stopAiPoll();
  stopPubPoll();
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
  else if (name === "hookmaker") {
    stopHookPoll();
    fillProjectSelect($("hookProject"), "Pick a project…").then(() => {
      if (jobId) $("hookProject").value = jobId;
    });
  }
  else if (name === "aimanager") initAiManager();
  else if (name === "publishing") initPublishing();
  else if (name === "admin") initAdmin();
  syncSidebar(name);
}

/* ---------------- AI Installation Manager ----------------
   One page over ai_components.manager_state(): component table,
   overall progress with speed + ETA, and the real actions -
   Install / Pause / Resume / Retry / Verify / Repair / Update /
   Diagnostic / Reinstall. The poll lives only while the page is
   open (stopAiPoll is called by every view switch). */
let aiPollTimer = null;

function stopAiPoll() {
  if (aiPollTimer) { clearInterval(aiPollTimer); aiPollTimer = null; }
}
function startAiPoll() {
  if (aiPollTimer) return;
  aiPollTimer = setInterval(refreshAiManager, 1500);
}
function initAiManager() {
  refreshAiManager();
  startAiPoll();
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + " KB";
  return n + " B";
}
function fmtEta(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  if (sec < 60) return "about " + sec + "s left";
  if (sec < 3600) return "about " + Math.round(sec / 60) + " min left";
  return "about " + (sec / 3600).toFixed(1) + " h left";
}
const AI_STATUS_LABEL = {
  installed: "Installed ✓", missing: "Missing",
  downloading: "Downloading…", verifying: "Verifying…",
  corrupt: "Damaged", update_available: "Update available",
};
const AI_STATUS_CLASS = {
  installed: "b-ok", missing: "b-bad", downloading: "b-info",
  verifying: "b-info", corrupt: "b-bad", update_available: "b-warn",
};

async function refreshAiManager() {
  if (toolOpen !== "aimanager") { stopAiPoll(); return; }
  let st;
  try { st = await api.ai_manager_status(); } catch (e) { return; }
  if (!st || !st.components) return;
  renderAiManager(st);
}

function renderAiManager(st) {
  const dl = st.download || {}, op = st.install || {},
    health = st.health || {}, hw = st.hardware || {},
    eng = st.engine || {};
  const comps = st.components.filter((c) => c.applicable);
  const allOk = comps.every((c) => c.healthy);

  /* component table */
  const body = $("aiCompBody");
  body.innerHTML = "";
  st.components.forEach((c) => {
    const tr = document.createElement("tr");
    if (!c.applicable) tr.className = "ai-comp-na";
    const ver = c.installed_version
      ? c.installed_version + (c.installed_version !== c.version
          ? " → " + c.version : "")
      : c.version;
    tr.innerHTML =
      "<td><div>" + c.name + "</div><div class='hint'>" + c.detail +
      (c.note ? " — " + c.note : "") + "</div></td>" +
      "<td>" + ver + "</td><td>" + fmtBytes(c.size) + "</td>" +
      "<td><span class='badge " + (AI_STATUS_CLASS[c.status] || "") +
      "'>" + (AI_STATUS_LABEL[c.status] || c.status) + "</span></td>";
    body.appendChild(tr);
  });

  /* storage + hardware lines */
  const needed = health.disk_needed_bytes || 0;
  $("aiStorageText").textContent = needed > 0
    ? "Storage: " + fmtBytes(needed) + " to download · " +
      fmtBytes(health.disk_free_bytes || 0) + " free on this PC"
    : "Storage: everything is installed · " +
      fmtBytes(health.disk_free_bytes || 0) + " free on this PC";
  const gpu = hw.gpu;
  $("aiHwText").textContent =
    (gpu ? "Graphics: " + gpu.name + " (" +
      fmtBytes(gpu.vram_bytes) + ")"
         : "Graphics: no NVIDIA card detected") +
    " · Memory: " + fmtBytes(hw.ram_bytes) +
    " · CPU cores: " + (hw.cpu_cores || "?") +
    " · Engine mode: " +
    (eng.flavor === "gpu" ? "GPU (CUDA)" : "CPU") +
    (eng.fallback ? " — " + eng.fallback : "");

  /* setup panel: stage, overall progress, speed, ETA */
  const busy = op.active || dl.active;
  let stage = "";
  if (op.active) stage = op.stage || "Setting up…";
  else if (dl.active) stage = (dl.stage || "Downloading…") +
    " — " + Math.round(dl.pct || 0) + "%";
  else if (allOk) stage = "The AI Engine is fully installed on this PC.";
  else if (dl.paused) stage = dl.stage ||
    "Paused — the partial download is kept.";
  else if (op.error) stage = "Setup stopped: " + op.error;
  else stage = "Some AI components are missing or damaged — " +
    "Install AI sets everything up.";
  $("aiSetupText").textContent = stage;
  $("aiOverallBar").style.width = (st.overall.pct || 0) + "%";
  $("aiOverallText").textContent = fmtBytes(st.overall.downloaded) +
    " of " + fmtBytes(st.overall.total) + " · " +
    (st.overall.pct || 0) + "%";
  $("aiSpeedText").textContent = dl.active && dl.rate_bps > 0
    ? "↓ " + (dl.rate_bps / 1e6).toFixed(1) + " MB/s" : "";
  $("aiEtaText").textContent = dl.active ? fmtEta(dl.eta_seconds) : "";
  const opErr = $("aiOpError");
  if (op.error) { opErr.textContent = op.error; show(opErr); }
  else if (dl.error && !dl.active) {
    opErr.textContent = "Download stopped: " + dl.error;
    show(opErr);
  } else hide(opErr);

  /* success banner - a real state: everything healthy AND an
     install finished this session or the diagnostic passed */
  const banner = $("aiSuccessBanner");
  if (st.success) {
    const diag = st.diagnostic || {};
    $("aiSuccessText").textContent = diag.done && diag.passed
      ? "Every component is installed, verified, and the diagnostic passed — the engine launches, the brain answers, and the speech model loads. You can start clipping right away."
      : "Every component is installed and verified. You can start clipping videos right away — the AI works fully offline.";
    show(banner);
  } else hide(banner);

  /* action buttons follow the state - no dead buttons */
  const btn = (id, on2, vis2) => {
    const b = $(id);
    b.disabled = !on2;
    b.classList.toggle("hidden", !vis2);
  };
  btn("aiInstallBtn", !busy && !allOk, true);
  btn("aiPauseBtn", dl.active, dl.active);
  btn("aiResumeBtn", !busy, !!dl.paused);
  btn("aiRetryBtn", !busy, !!(!dl.active && dl.error));
  btn("aiVerifyBtn", !busy && !st.verify.active, true);
  btn("aiRepairBtn", !busy && !allOk, true);
  btn("aiDiagBtn", !st.diagnostic.active, true);
  btn("aiReinstallBtn", !busy, true);
  $("aiActionText").textContent = st.verify.active
    ? "Verifying " + (st.verify.current || "") +
      " — the full model checksum takes a little while…"
    : st.diagnostic.active ? "Running the diagnostic…"
    : op.active ? (op.stage || "") : "";

  /* verify results */
  const vw = $("aiVerifyWrap"), vl = $("aiVerifyList");
  if (st.verify.done || st.verify.active) {
    show(vw);
    vl.innerHTML = "";
    Object.keys(st.verify.results || {}).forEach((cid) => {
      const r = st.verify.results[cid];
      const row = document.createElement("div");
      row.className = "ai-check-row";
      row.innerHTML = "<span class='badge " +
        (r.ok ? "b-ok" : "b-bad") + "'>" + (r.ok ? "✓" : "✗") +
        "</span> <b>" + cid + "</b> — " + r.detail;
      vl.appendChild(row);
    });
    if (st.verify.error) {
      const row = document.createElement("div");
      row.className = "ai-check-row";
      row.textContent = "Verification stopped: " + st.verify.error;
      vl.appendChild(row);
    }
  } else hide(vw);

  /* diagnostic results */
  const dw = $("aiDiagWrap"), dlList = $("aiDiagList");
  if ((st.diagnostic.items || []).length || st.diagnostic.active) {
    show(dw);
    dlList.innerHTML = "";
    (st.diagnostic.items || []).forEach((it) => {
      const row = document.createElement("div");
      row.className = "ai-check-row";
      const cls = it.status === "pass" ? "b-ok"
        : it.status === "fail" ? "b-bad" : "b-warn";
      const mark = it.status === "pass" ? "✓"
        : it.status === "fail" ? "✗" : "–";
      row.innerHTML = "<span class='badge " + cls + "'>" + mark +
        "</span> <b>" + it.label + "</b> — " + it.detail;
      dlList.appendChild(row);
    });
  } else hide(dw);

  /* update line (kept from the last check; refreshes on poll only
     while an update is actually running) */
  const upd = st.update || {};
  if (upd.active) {
    $("aiUpdateText").textContent = "Updating " + upd.component +
      " — the new files are verified before they replace the old " +
      "ones, and the old version is kept until the swap checks out.";
  } else if (upd.done) {
    $("aiUpdateText").textContent = "AI update installed for " +
      upd.component + " ✓";
  } else if (upd.error) {
    $("aiUpdateText").textContent = "Update failed: " + upd.error +
      " — the previous version is still in place.";
  }
}

async function aiAct(fn, busyMsg) {
  if (busyMsg) $("aiActionText").textContent = busyMsg;
  try { await fn(); } catch (e) { /* the poll re-renders the state */ }
  refreshAiManager();
  refreshLocalAiStatus();
}
$("aiInstallBtn").addEventListener("click", () =>
  aiAct(() => api.ai_install(), "Starting the AI installation…"));
$("aiPauseBtn").addEventListener("click", () =>
  aiAct(() => api.ai_pause(), "Pausing…"));
$("aiResumeBtn").addEventListener("click", () =>
  aiAct(() => api.ai_resume(), "Resuming…"));
$("aiRetryBtn").addEventListener("click", () =>
  aiAct(() => api.ai_resume(), "Retrying…"));
$("aiVerifyBtn").addEventListener("click", () =>
  aiAct(() => api.ai_verify(), "Verifying the AI files…"));
$("aiRepairBtn").addEventListener("click", () =>
  aiAct(() => api.ai_repair(), "Repairing the damaged components…"));
$("aiDiagBtn").addEventListener("click", () =>
  aiAct(() => api.ai_diagnostic(), "Running the diagnostic…"));
$("aiReinstallBtn").addEventListener("click", () => {
  if (!window.confirm("Reinstall the AI? This deletes the AI Brain " +
      "(~9 GB), the runtime and the speech model, then downloads " +
      "everything fresh.")) return;
  aiAct(() => api.ai_reinstall(), "Reinstalling the AI…");
});
$("aiUpdateBtn").addEventListener("click", async () => {
  let res;
  try { res = await api.ai_update_check(); } catch (e) { return; }
  const lines = (res.components || []).map((c) =>
    c.id + ": installed " + (c.installed_version || "—") +
    " · available " + c.available_version +
    (c.update_available ? " — UPDATE AVAILABLE" : " — up to date"));
  $("aiUpdateText").innerHTML = "";
  lines.forEach((ln) => {
    const div = document.createElement("div");
    div.textContent = ln;
    $("aiUpdateText").appendChild(div);
  });
  (res.components || []).filter((c) => c.update_available)
    .forEach((c) => {
      const b = document.createElement("button");
      b.className = "ghost-btn small";
      b.type = "button";
      b.textContent = "Update " + c.id + " to " + c.available_version;
      b.addEventListener("click", () =>
        aiAct(() => api.ai_update_apply(c.id), "Updating " + c.id + "…"));
      $("aiUpdateText").appendChild(b);
    });
  if (!res.update_available)
    toast("The AI is up to date ✓");
});
$("hubAiManageBtn").addEventListener("click", () =>
  showTool("aimanager"));
$("settingsAiManagerBtn").addEventListener("click", () => {
  closeSettings();
  showTool("aimanager");
});

/* Home: recent projects - same cards as the Library. Opening one is a
   plain navigation; project state lives outside this system. */
function openProjectFromHome(id) {
  closeStudioViews();
  hide($("libraryView"));
  hide($("removerView"));
  libraryOpen = false;
  removerOpen = false;
  openProject(id);
  syncSidebar("wizard");
}

async function refreshHomeRecents() {
  const grid = $("homeRecents");
  if (!grid) return;
  let projects = [];
  try {
    const res = await api.library();
    projects = res.projects || [];
  } catch (e) { /* offline: the empty state below stays honest */ }
  const recent = projects.slice(0, 6);
  grid.innerHTML = "";
  $("homeRecentsEmpty").classList.toggle("hidden", recent.length > 0);
  recent.forEach((j) => {
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
      () => openProjectFromHome(j.job_id));
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
        refreshHomeRecents();
        refreshLibrary();
        refreshQueue();
      } else {
        toast(res2.error || "Delete failed", true);
      }
    });
    grid.appendChild(card);
  });
  staggerCards(grid, ".lib-card");
}

$("studioBtn").addEventListener("click", goHome);
$("navClipBtn").addEventListener("click", () => showWizard(1));
$("navHookBtn").addEventListener("click", () => showTool("hookmaker"));
$("navGenBtn").addEventListener("click", () => showTool("generator"));
$("navStudioBtn").addEventListener("click", () => {
  showLibrary();
  toast("Pick a clip in the Library, then open Caption Studio on it");
});
$("navTitlesBtn").addEventListener("click", () => showTool("titles"));
$("navScriptBtn").addEventListener("click", () => showTool("script"));
$("navTranscriptBtn").addEventListener("click", () => showTool("transcript"));
$("navEnhBtn").addEventListener("click", () => showTool("enhancer"));
$("navAiBtn").addEventListener("click", () => showTool("aimanager"));
$("navPubBtn").addEventListener("click", () => showTool("publishing"));
$("navAdminBtn").addEventListener("click", () => openAdmin());
$("sideAiCard").addEventListener("click", () => showTool("aimanager"));
$("homeSeeAllBtn").addEventListener("click", showLibrary);
document.querySelectorAll(".tool-back").forEach((b) =>
  b.addEventListener("click", backToStudio));

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
    else if (tool === "admin") openAdmin();
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
$("hubSearch").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const first = [...document.querySelectorAll(".tool-card")]
    .find((c) => c.style.display !== "none");
  if (first) first.click();
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
$("settingsAiDownloadBtn").addEventListener("click", startAiDownload);
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

/* ---------------- Post-clipping enhancement (Enhance All) ----------------
   The "Your Clips Are Ready!" panel in step 3: batch-enhances the
   project's renders with the built-in enhancer. Originals are never
   touched; results land beside them as <name>_enhanced.mp4. */
let enhBatchId = null;
let enhTimer = null;
let enhPanelData = [];       // last enhance_panel() renders
let enhChecked = new Set();  // idxs ticked for enhancement
let enhCapsLoaded = false;

function enhOptions() {
  const custom = $("enhModeSeg").querySelector(".seg.on") &&
    $("enhModeSeg").querySelector(".seg.on").dataset.value === "custom";
  return {
    mode: custom ? "custom" : "smart",
    target: parseInt($("enhTarget").value, 10) || 0,
    sharpen: $("enhSharpen").value,
    denoise: $("enhDenoise").value,
    colour: $("enhColour").checked,
    stabilize: $("enhStabilize").checked,
  };
}

$("enhModeSeg").querySelectorAll(".seg").forEach((seg) =>
  seg.addEventListener("click", () => {
    $("enhModeSeg").querySelectorAll(".seg").forEach((s) =>
      s.classList.toggle("on", s === seg));
    const custom = seg.dataset.value === "custom";
    $("enhCustom").classList.toggle("hidden", !custom);
    $("enhModeHint").textContent = custom
      ? "Your settings, applied to every ticked clip."
      : "Analyses each clip and picks its settings — a clip that is already clean at full size is skipped, not re-encoded.";
  }));

async function refreshEnhancePanel() {
  const panel = $("enhancePanel");
  if (!jobId) { hide(panel); return; }
  let res;
  try { res = await api.enhance_panel(jobId); } catch (e) { return; }
  if (!res || !res.ok || !res.renders || !res.renders.length) {
    hide(panel);
    return;
  }
  enhPanelData = res.renders;
  if (!enhChecked.size)
    res.renders.forEach((r) => enhChecked.add(r.idx));
  show(panel);
  const nEnh = res.renders.filter((r) => r.enhanced).length;
  $("enhSummary").textContent =
    `${res.renders.length} clip${res.renders.length > 1 ? "s" : ""}` +
    (nEnh ? ` · ${nEnh} enhanced` : "");
  $("enhZipBtn").classList.toggle("hidden", !nEnh);
  if (!enhCapsLoaded) {
    enhCapsLoaded = true;
    try {
      const caps = await api.enhance_caps();
      if (caps && caps.stabilize) show($("enhStabWrap"));
    } catch (e) { /* optional filters stay hidden */ }
  }
  renderEnhRows();
}

function enhRowStatus(r) {
  if (r.enhanced)
    return { text: `Enhanced ✓ ${r.enhanced.w}×${r.enhanced.h} · ` +
      `${fmtBytes(r.enhanced.size)} · ${r.enhanced.seconds}s` +
      (r.enhanced.why ? ` — ${r.enhanced.why}` : ""), cls: "done" };
  return { text: "Not enhanced yet", cls: "" };
}

function renderEnhRows() {
  const box = $("enhClips");
  box.innerHTML = "";
  enhPanelData.forEach((r) => {
    const row = document.createElement("div");
    row.className = "enh-row";
    row.dataset.idx = r.idx;
    const st = enhRowStatus(r);
    row.innerHTML = `
      <input type="checkbox" class="planner-check enh-check" data-i="${r.idx}"
        ${enhChecked.has(r.idx) ? "checked" : ""} title="Include in Enhance All">
      ${r.thumb_url
        ? `<img class="enh-thumb" src="${r.thumb_url}" alt="">`
        : `<div class="enh-thumb-ph"></div>`}
      <div class="enh-main">
        <div class="enh-name">${escapeHtml(r.title)}</div>
        <div class="enh-meta">${r.w}×${r.h} · ${r.duration}s · ${fmtBytes(r.size)}</div>
        <div class="enh-status" data-role="status">${escapeHtml(st.text)}</div>
      </div>
      <div class="enh-actions">
        <button class="ghost-btn small enh-one" data-i="${r.idx}">Enhance</button>
        ${r.enhanced ? `<button class="ghost-btn small enh-compare" data-i="${r.idx}">Compare</button>
        <button class="ghost-btn small enh-dl" data-i="${r.idx}">Download</button>` : ""}
      </div>`;
    box.appendChild(row);
  });
  box.querySelectorAll(".enh-check").forEach((cb) =>
    cb.addEventListener("change", () => {
      const i = parseInt(cb.dataset.i, 10);
      if (cb.checked) enhChecked.add(i); else enhChecked.delete(i);
    }));
  box.querySelectorAll(".enh-one").forEach((btn) =>
    btn.addEventListener("click", () =>
      startEnhanceBatch([parseInt(btn.dataset.i, 10)])));
  box.querySelectorAll(".enh-compare").forEach((btn) =>
    btn.addEventListener("click", () =>
      openCompare(parseInt(btn.dataset.i, 10))));
  box.querySelectorAll(".enh-dl").forEach((btn) =>
    btn.addEventListener("click", () => {
      const r = enhPanelData.find((x) =>
        x.idx === parseInt(btn.dataset.i, 10));
      if (r && r.enhanced) downloadUrl(r.enhanced.url);
    }));
}

async function startEnhanceBatch(indices) {
  hide($("enhError"));
  hide($("enhDoneBanner"));
  if (!indices.length) {
    toast("Tick at least one clip to enhance", true);
    return;
  }
  const res = await api.enhance_batch_start(jobId, indices, enhOptions());
  if (!res.ok) {
    $("enhError").textContent = res.error || "Enhance failed to start.";
    show($("enhError"));
    return;
  }
  enhBatchId = res.batch_id;
  show($("enhProgress"));
  $("enhBar").style.width = "0%";
  $("enhStage").textContent = "Enhancing…";
  show($("enhPauseBtn"));
  $("enhPauseBtn").textContent = "Pause queue";
  if (enhTimer) clearInterval(enhTimer);
  enhTimer = setInterval(pollEnhBatch, 1000);
  pollEnhBatch();
}

function enhSetRowState(idx, html, pct) {
  const row = $("enhClips").querySelector(`.enh-row[data-idx="${idx}"]`);
  if (!row) return;
  const st = row.querySelector('[data-role="status"]');
  if (st) st.innerHTML = html;
  const acts = row.querySelector(".enh-actions");
  if (acts && pct !== undefined) {
    // keep buttons; progress lives in the status line
  }
}

async function pollEnhBatch() {
  if (!enhBatchId) { stopEnhPoll(); return; }
  let st;
  try { st = await api.enhance_batch_status(enhBatchId); }
  catch (e) { return; }
  if (!st.ok) { stopEnhPoll(); return; }
  $("enhBar").style.width = (st.overall || 0) + "%";
  const c = st.counts || {};
  $("enhStage").textContent =
    `Enhancing — ${c.done || 0} done` +
    (c.skipped ? ` · ${c.skipped} skipped` : "") +
    (c.failed ? ` · ${c.failed} failed` : "") +
    ` · ${st.overall || 0}%`;
  $("enhPauseBtn").textContent = st.paused ? "Resume queue" : "Pause queue";
  (st.items || []).forEach((it) => {
    let html;
    if (it.state === "running")
      html = `${escapeHtml(it.stage || "Enhancing")} — ${it.pct || 0}%` +
        `<div class="bar"><div class="fill" style="width:${it.pct || 0}%"></div></div>`;
    else if (it.state === "queued") html = "Queued";
    else if (it.state === "done")
      html = `Enhanced ✓ ${it.new ? it.new.w + "×" + it.new.h : ""} · ` +
        `${it.seconds}s — ${escapeHtml(it.why || "")}`;
    else if (it.state === "skipped")
      html = escapeHtml(it.why || "Skipped");
    else if (it.state === "failed")
      html = `Failed — ${escapeHtml(it.error || "")} ` +
        `<button class="ghost-btn small enh-retry" data-i="${it.idx}">Retry</button>`;
    else if (it.state === "cancelled")
      html = `Cancelled <button class="ghost-btn small enh-retry" data-i="${it.idx}">Retry</button>`;
    else html = it.state;
    if (it.state === "running" || it.state === "queued")
      html += ` <button class="ghost-btn small enh-cancel" data-i="${it.idx}">Cancel</button>`;
    enhSetRowState(it.idx, html);
  });
  $("enhClips").querySelectorAll(".enh-cancel").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api.enhance_batch_cancel(enhBatchId,
        parseInt(btn.dataset.i, 10));
      pollEnhBatch();
    }));
  $("enhClips").querySelectorAll(".enh-retry").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api.enhance_batch_retry(enhBatchId,
        parseInt(btn.dataset.i, 10));
      if (!enhTimer) {
        show($("enhProgress"));
        enhTimer = setInterval(pollEnhBatch, 1000);
      }
      pollEnhBatch();
    }));
  const active = (st.items || []).some((it) =>
    it.state === "queued" || it.state === "running");
  if (!active) {
    stopEnhPoll();
    hide($("enhProgress"));
    hide($("enhPauseBtn"));
    const done = c.done || 0, skipped = c.skipped || 0,
      failed = c.failed || 0;
    if (done || skipped) {
      $("enhDoneText").textContent =
        `${done} enhanced · ${skipped} skipped · ${failed} failed`;
      show($("enhDoneBanner"));
    }
    if (failed) {
      $("enhError").textContent =
        `${failed} clip(s) failed — see the clip rows for the reason, then Retry.`;
      show($("enhError"));
    }
    refreshEnhancePanel();
  }
}

function stopEnhPoll() {
  if (enhTimer) { clearInterval(enhTimer); enhTimer = null; }
}

$("enhAllBtn").addEventListener("click", () =>
  startEnhanceBatch([...enhChecked].sort((a, b) => a - b)));
$("enhPubBtn").addEventListener("click", () => {
  if (jobId) openPublishFor(jobId, null);
});
$("enhPauseBtn").addEventListener("click", async () => {
  if (!enhBatchId) return;
  const st = await api.enhance_batch_status(enhBatchId);
  if (st && st.ok && st.paused) await api.enhance_batch_resume(enhBatchId);
  else await api.enhance_batch_pause(enhBatchId);
  pollEnhBatch();
});
$("enhDlAllBtn").addEventListener("click", async () => {
  const res = await api.make_zip(jobId, true);
  if (res.ok && res.url) { downloadUrl(res.url); toast("Clips ZIP downloaded"); }
  else toast(res.error || "ZIP failed", true);
});
$("enhZipBtn").addEventListener("click", async () => {
  const res = await api.enhance_zip(jobId);
  if (res.ok && res.url) { downloadUrl(res.url); toast("Enhanced ZIP downloaded"); }
  else toast(res.error || "ZIP failed", true);
});

/* before/after compare */
function openCompare(idx) {
  const r = enhPanelData.find((x) => x.idx === idx);
  if (!r || !r.enhanced) return;
  $("compareTitle").textContent = "Before / After — " + r.title;
  const stage = $("compareStage");
  stage.style.aspectRatio = `${r.w} / ${r.h}`;
  stage.style.width = r.w >= r.h ? "100%" : "min(400px, 100%)";
  stage.style.setProperty("--cmp", "50%");
  $("compareSlider").value = 50;
  const before = $("compareBefore"), after = $("compareAfter");
  before.src = r.url;
  after.src = r.enhanced.url;
  $("compareStats").textContent =
    `${r.w}×${r.h} · ${fmtBytes(r.size)} → ` +
    `${r.enhanced.w}×${r.enhanced.h} · ${fmtBytes(r.enhanced.size)} · ` +
    `took ${r.enhanced.seconds}s`;
  $("comparePlayBtn").textContent = "Play both";
  show($("compareModal"));
}
function closeCompare() {
  ["compareBefore", "compareAfter"].forEach((id) => {
    const v = $(id);
    try { v.pause(); } catch (e) { /* noop */ }
    v.removeAttribute("src");
    v.load();
  });
  hide($("compareModal"));
}
$("compareSlider").addEventListener("input", (e) => {
  $("compareStage").style.setProperty("--cmp", e.target.value + "%");
});
$("comparePlayBtn").addEventListener("click", () => {
  const b = $("compareBefore"), a = $("compareAfter");
  if (b.paused && a.paused) {
    b.currentTime = 0; a.currentTime = 0;
    b.play(); a.play();
    $("comparePlayBtn").textContent = "Pause both";
  } else {
    b.pause(); a.pause();
    $("comparePlayBtn").textContent = "Play both";
  }
});
$("compareCloseBtn").addEventListener("click", closeCompare);

/* ---------------- Studio: Hook Maker ---------------- */
let hookTimer = null;
let hookData = null;        // last hook_analyze result
let hookJobId = null;       // project the analysis belongs to
let hookSelText = "";       // chosen hook line
let hookSelCand = null;     // chosen ranked hook candidate (Hook v2)

function stopHookPoll() {
  if (hookTimer) { clearInterval(hookTimer); hookTimer = null; }
}

function hookWindow() {
  const s = parseFloat($("hookStartInput").value);
  const e = parseFloat($("hookEndInput").value);
  return [isNaN(s) ? 0 : s, isNaN(e) ? 0 : e];
}

function syncHookWindowMeta() {
  if (!hookData) return;
  let [s, e] = hookWindow();
  const dur = hookData.duration || 0;
  if (dur > 0) {
    s = Math.max(0, Math.min(s, Math.max(0, dur - 1)));
    e = Math.max(s + 1, Math.min(e, dur));
  }
  if (e - s > 15) e = s + 15;
  if (e - s < 1) e = s + 1;
  $("hookStartInput").value = s.toFixed(1);
  $("hookEndInput").value = e.toFixed(1);
  $("hookWindowMeta").textContent =
    `${(e - s).toFixed(1)}s window · ${fmtTime(s)} – ${fmtTime(e)}`;
}

document.querySelectorAll("[data-nudge]").forEach((b) =>
  b.addEventListener("click", () => {
    const [which, d] = b.dataset.nudge.split(",");
    const input = which === "start" ? $("hookStartInput")
                                    : $("hookEndInput");
    input.value = (parseFloat(input.value) || 0) + parseFloat(d);
    syncHookWindowMeta();
  }));
["hookStartInput", "hookEndInput"].forEach((id) =>
  $(id).addEventListener("change", syncHookWindowMeta));

$("hookImportBtn").addEventListener("click", () => showWizard(1));
$("hookAnalyzeBtn").addEventListener("click", async () => {
  const job = $("hookProject").value;
  hide($("hookError"));
  hide($("hookResults"));
  hide($("hookRenderError"));
  stopHookPoll();
  hookData = null;
  hookSelText = "";
  hookSelCand = null;
  if (!job) { toast("Pick a Library project first", true); return; }
  $("hookAnalyzeBtn").disabled = true;
  let res;
  try { res = await api.hook_analyze(job); }
  catch (e) { res = { ok: false, error: "Couldn't reach the local backend." }; }
  $("hookAnalyzeBtn").disabled = false;
  if (!res.ok) {
    $("hookError").textContent = res.error || "Analysis failed.";
    show($("hookError"));
    return;
  }
  hookData = res;
  hookJobId = job;
  $("hookModeNote").textContent = res.note || "";
  const list = $("hookList");
  list.innerHTML = "";
  (res.hooks || []).forEach((h, i) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "title-row hook-row" + (i === 0 ? " sel" : "");
    row.innerHTML = `${scoreChip(h.score)}<span class="t-text">${escapeHtml(h.text)}</span>`;
    row.addEventListener("click", () => {
      list.querySelectorAll(".hook-row").forEach((r) =>
        r.classList.remove("sel"));
      row.classList.add("sel");
      hookSelText = h.text;
    });
    list.appendChild(row);
    if (i === 0) hookSelText = h.text;
  });
  // Hook v2: ranked candidate hook moments across the whole video.
  // The top pick is marked; clicking any candidate makes it the
  // hook moment (window + overlay text follow the pick).
  const candList = $("hookCandList");
  candList.innerHTML = "";
  (res.candidates || []).forEach((c, i) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "title-row hook-row cand-row" + (i === 0 ? " sel" : "");
    row.innerHTML = `${scoreChip(c.score)}<span class="t-text">${escapeHtml(c.text)}</span>
      <span class="cand-meta">${fmtTime(c.start)} – ${fmtTime(c.end)} · ${escapeHtml(c.reason || "")}</span>`;
    row.addEventListener("click", () => {
      candList.querySelectorAll(".cand-row").forEach((r) =>
        r.classList.remove("sel"));
      row.classList.add("sel");
      hookSelCand = c;
      hookSelText = c.text;
      $("hookMomentText").textContent = "“" + (c.text || "") + "”";
      $("hookStartInput").value = Number(c.start).toFixed(1);
      $("hookEndInput").value = Number(c.end).toFixed(1);
      syncHookWindowMeta();
    });
    candList.appendChild(row);
    if (i === 0) hookSelCand = c;
  });
  const m = res.moment;
  if (m) {
    $("hookMomentText").textContent = "“" + (m.text || "") + "”";
    const srcLabel = m.source === "moment" ? "your top detected moment"
      : m.source === "detected" ? "found by detection"
      : "best transcript segment";
    $("hookMomentMeta").textContent =
      `${fmtTime(m.segment_start)} – ${fmtTime(m.segment_end)} · ${srcLabel} · score ${m.score}. ` +
      "The render window below is trimmed to its most intense seconds.";
    $("hookMomentSignals").innerHTML = sigChips(m);
    $("hookStartInput").value = Number(m.start).toFixed(1);
    $("hookEndInput").value = Number(m.end).toFixed(1);
  } else {
    $("hookMomentText").textContent =
      "No strong moment found in the transcript.";
    $("hookMomentMeta").textContent = "";
    $("hookMomentSignals").innerHTML = "";
    $("hookStartInput").value = "0";
    $("hookEndInput").value = "5";
  }
  syncHookWindowMeta();
  const sel = $("hookClipSelect");
  sel.innerHTML = "";
  (res.clips || []).forEach((c, i) => {
    const o = document.createElement("option");
    o.value = i;
    o.textContent = `${fmtTime(c.start)} – ${fmtTime(c.end)} · ${c.title || "Clip"} (score ${c.virality})`;
    sel.appendChild(o);
  });
  if (!(res.clips || []).length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "No clips detected — render the hook clip on its own";
    sel.appendChild(o);
  }
  $("hookComboBtn").disabled = !(res.clips || []).length;
  $("hookTeaserBtn").disabled = !(res.clips || []).length;
  hide($("hookOutWrap"));
  $("hookOut").innerHTML = "";
  show($("hookResults"));
});

async function hookStartRender(kind) {
  if (!hookData || !hookJobId) {
    toast("Analyze a video first", true);
    return;
  }
  if (!hookSelText) { toast("Pick a hook line first", true); return; }
  const [s, e] = hookWindow();
  hide($("hookRenderError"));
  let res;
  try {
    if (kind === "combo") {
      const c = (hookData.clips || [])[
        parseInt($("hookClipSelect").value, 10)];
      if (!c) { toast("No clip picked", true); return; }
      res = await api.hook_combo(hookJobId, hookSelText, s, e,
        c.index, c.start, c.end, c.title || "");
    } else if (kind === "teaser") {
      const c = (hookData.clips || [])[
        parseInt($("hookClipSelect").value, 10)];
      if (!c) { toast("No clip picked", true); return; }
      // the picked candidate's own window wins when one is chosen;
      // otherwise the (possibly hand-nudged) window inputs rule
      const cand = hookSelCand;
      const ts = cand ? cand.start : s;
      const te = cand ? cand.end : e;
      const tt = cand ? cand.text : hookSelText;
      res = await api.hook_teaser(hookJobId, tt, ts, te,
        c.index, c.start, c.end, c.title || "");
    } else {
      res = await api.hook_render(hookJobId, hookSelText, s, e);
    }
  } catch (err) {
    res = { ok: false, error: "Couldn't reach the local backend." };
  }
  if (!res.ok) {
    $("hookRenderError").textContent =
      res.message || res.error || "Render failed to start.";
    show($("hookRenderError"));
    return;
  }
  $("hookRenderBtn").disabled = true;
  $("hookComboBtn").disabled = true;
  $("hookTeaserBtn").disabled = true;
  show($("hookProgress"));
  $("hookBar").style.width = "0%";
  $("hookStage").textContent = "Rendering…";
  $("hookStatus").textContent = "Rendering…";
  stopHookPoll();
  hookTimer = setInterval(pollHook, 1000);
  pollHook();
}

async function pollHook() {
  if (!hookJobId) { stopHookPoll(); return; }
  let st;
  try { st = await api.job_status(hookJobId); }
  catch (e) { return; }
  if (st.status === "missing") { stopHookPoll(); return; }
  if (st.status === "working") {
    $("hookBar").style.width = (st.progress || 0) + "%";
    $("hookStage").textContent = st.stage || "Rendering…";
    return;
  }
  stopHookPoll();
  hide($("hookProgress"));
  $("hookStatus").textContent = "";
  $("hookRenderBtn").disabled = false;
  $("hookComboBtn").disabled =
    !(hookData && (hookData.clips || []).length);
  $("hookTeaserBtn").disabled =
    !(hookData && (hookData.clips || []).length);
  if (st.render_error) {
    $("hookRenderError").textContent = st.render_error;
    show($("hookRenderError"));
    return;
  }
  renderHookOut(st.renders || []);
  toast("Hook render finished");
  refreshClipsChip();
}

function renderHookOut(renders) {
  const mine = (renders || []).filter((r) =>
    r.hook_clip || r.teaser || r.title === "Hook + clip"
    || r.title === "Teaser");
  if (!mine.length) return;
  const wrap = $("hookOut");
  wrap.innerHTML = "";
  mine.forEach((r) => {
    const row = document.createElement("div");
    row.className = "hook-out-row";
    const label = document.createElement("span");
    label.className = "t-text";
    label.textContent = r.title || "Hook clip";
    row.appendChild(label);
    const a = document.createElement("a");
    a.className = "ghost-btn small";
    a.href = r.url || "#";
    a.setAttribute("download", "");
    a.textContent = "Download";
    row.appendChild(a);
    wrap.appendChild(row);
  });
  show($("hookOutWrap"));
}

$("hookRenderBtn").addEventListener("click", () => hookStartRender("single"));
$("hookComboBtn").addEventListener("click", () => hookStartRender("combo"));
$("hookTeaserBtn").addEventListener("click", () => hookStartRender("teaser"));

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
  // Performance (Speed pass): GPU mode, responsive toggle, thread
  // cap, and the server's honest encoder status line.
  segSet("gpuSeg", ["auto", "gpu", "cpu"].includes(settings.gpu_accel)
    ? settings.gpu_accel : "auto");
  segSet("responsiveSeg", settings.responsive_render === false
    ? "off" : "on");
  $("renderThreadsInput").value = settings.render_threads || 0;
  $("encoderStatus").textContent = settings.encoder_status
    ? settings.encoder_status.label
    : "Encoder status unavailable.";
  $("accentPick").value = settings.accent || "#2effc8";
  // fal.ai key: the field always starts EMPTY (the key is never sent
  // back to the page) - the state line says whether one is saved.
  $("falKeyInput").value = "";
  $("falKeyState").textContent = settings.fal_key_set
    ? "A key is saved on this PC. Type a new one to replace it."
    : "No key saved.";
  openSettings();
  refreshStorage();
  refreshUpdateInfo();
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

/* ---------------- storage location + update packs ---------------
   Storage: the server owns the truth (storage_status) - where the
   model effectively lives, where new projects go, and any running
   model move. Saving a new location can kick off a background
   move; this section polls while one runs.
   Updates: apply_update takes the pack's file PATH (the app's
   convention for local files) and the server verifies the pack's
   signature before touching anything. */
let storageTimer = null;

async function refreshStorage() {
  let st;
  try { st = await api.storage_status(); } catch (e) { return; }
  if (!st || !st.ok) return;
  const state = $("storageState");
  if (!state) return;
  const mv = st.move || {};
  if (mv.active) {
    state.textContent = "Moving the AI model to the new location — "
      + (mv.pct || 0) + "%… keep the app open.";
    show($("storageMoveBarWrap"));
    $("storageMoveBar").style.width = (mv.pct || 0) + "%";
    if (!storageTimer)
      storageTimer = setInterval(refreshStorage, 1200);
    return;
  }
  hide($("storageMoveBarWrap"));
  if (storageTimer) { clearInterval(storageTimer); storageTimer = null; }
  const where = st.model_location === "custom"
    ? "in your storage folder"
    : st.model_location === "default" ? "in the app folder"
    : "not downloaded yet";
  state.textContent = "AI model: " + where
    + " · New projects folder: " + (st.work_root || "");
  if (mv.error)
    $("storageResult").textContent = "The last move failed ("
      + mv.error + ") — the old location is still in use.";
  if (document.activeElement !== $("storageDirInput"))
    $("storageDirInput").value = st.data_dir || "";
}

$("storageSaveBtn").addEventListener("click", async () => {
  const btn = $("storageSaveBtn");
  const res = $("storageResult");
  btn.disabled = true;
  res.textContent = "Saving…";
  try {
    const r = await api.set_data_dir($("storageDirInput").value.trim());
    if (!r.ok) throw new Error(r.error || "could not save the location");
    res.textContent = r.message || "Saved.";
    refreshStorage();
  } catch (e) {
    res.textContent = e.message;
  }
  btn.disabled = false;
});

async function refreshUpdateInfo() {
  try {
    const r = await api.update_info();
    if (r && r.ok) $("updateVersion").textContent = "v" + r.version;
  } catch (e) { /* the static version text stays */ }
}

$("applyUpdateBtn").addEventListener("click", async () => {
  const btn = $("applyUpdateBtn");
  const res = $("updateResult");
  const path = $("updatePathInput").value.trim();
  if (!path) {
    res.textContent = "Paste the update pack's file path first.";
    return;
  }
  btn.disabled = true;
  res.textContent = "Checking the pack's signature and applying…";
  try {
    const r = await api.apply_update(path);
    if (!r.ok) throw new Error(r.error || "update failed");
    res.textContent = (r.message || "Update applied.")
      + " (pack version " + r.version + ")";
    toast("Update applied — restart the app");
  } catch (e) {
    res.textContent = e.message;
  }
  btn.disabled = false;
});

/* ---------------- old version folders (Settings cleanup) ----------
   Scan lists folders of OTHER copies of the app (loose old downloads
   and old installs). The server re-validates every delete target, so
   this UI only ever offers what the scan returned - and the copy
   being run is never among them. */
let oldCopiesFound = [];

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = n, u = -1;
  do { v /= 1024; u++; } while (v >= 1024 && u < units.length - 1);
  return (v >= 100 ? Math.round(v) : v.toFixed(1)) + " " + units[u];
}
function fmtDate(epochSecs) {
  if (!epochSecs) return "";
  try {
    return new Date(epochSecs * 1000).toLocaleDateString(undefined,
      { day: "numeric", month: "short", year: "numeric" });
  } catch (e) { return ""; }
}
function selectedOldCopies() {
  return [...document.querySelectorAll(
    "#oldCopiesList .old-copy-row input:checked")]
    .map((cb) => cb.dataset.path);
}
function syncOldCopiesDeleteBtn() {
  const btn = $("oldCopiesDeleteBtn");
  if (!btn) return;
  const n = selectedOldCopies().length;
  btn.disabled = n === 0;
  if (!btn.dataset.armed)
    btn.textContent = n ? `Delete selected (${n})` : "Delete selected";
}
function renderOldCopies() {
  const list = $("oldCopiesList");
  const actions = $("oldCopiesActions");
  list.innerHTML = "";
  if (!oldCopiesFound.length) {
    const d = document.createElement("div");
    d.className = "old-copies-empty";
    d.textContent = "No old copies found 🎉";
    list.appendChild(d);
    hide(actions);
  } else {
    oldCopiesFound.forEach((c) => {
      const row = document.createElement("label");
      row.className = "old-copy-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.dataset.path = c.path;
      cb.addEventListener("change", syncOldCopiesDeleteBtn);
      row.appendChild(cb);
      const main = document.createElement("span");
      main.className = "old-copy-main";
      const p = document.createElement("span");
      p.className = "old-copy-path";
      p.textContent = c.path;
      main.appendChild(p);
      const meta = document.createElement("span");
      meta.className = "old-copy-meta";
      const when = fmtDate(c.modified);
      meta.textContent = fmtBytes(c.size_bytes)
        + (when ? " · " + when : "");
      main.appendChild(meta);
      row.appendChild(main);
      if (c.has_uninstaller) {
        const tag = document.createElement("span");
        tag.className = "badge";
        tag.textContent = "has uninstaller";
        row.appendChild(tag);
      }
      list.appendChild(row);
    });
    show(actions);
  }
  show(list);
  syncOldCopiesDeleteBtn();
}
$("oldCopiesScanBtn").addEventListener("click", async () => {
  const btn = $("oldCopiesScanBtn");
  const state = $("oldCopiesState");
  btn.disabled = true;
  state.textContent = "Scanning…";
  $("oldCopiesResult").textContent = "";
  try {
    const r = await fetch("/api/old_copies");
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "scan failed");
    oldCopiesFound = data.copies || [];
    state.textContent = oldCopiesFound.length
      ? oldCopiesFound.length + " old "
        + (oldCopiesFound.length === 1 ? "copy" : "copies") + " found"
      : "";
    renderOldCopies();
  } catch (e) {
    state.textContent = "";
    toast("Old-copy scan failed: " + e.message, true);
  }
  btn.disabled = false;
});
$("oldCopiesDeleteBtn").addEventListener("click", async () => {
  const btn = $("oldCopiesDeleteBtn");
  const paths = selectedOldCopies();
  if (!paths.length) return;
  // same arm-to-confirm pattern as the Library delete button
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.textContent = "Sure? Delete " + paths.length
      + (paths.length === 1 ? " folder" : " folders");
    setTimeout(() => {
      delete btn.dataset.armed;
      syncOldCopiesDeleteBtn();
    }, 3000);
    return;
  }
  delete btn.dataset.armed;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  const resEl = $("oldCopiesResult");
  resEl.textContent = "";
  try {
    const res = await api.delete_old_copies(paths);
    const results = (res && res.results) || [];
    const gone = results.filter((r2) => r2.ok);
    const failed = results.filter((r2) => !r2.ok);
    if (failed.length) {
      resEl.textContent = "Deleted " + gone.length + " of "
        + results.length + " — failed: " + failed.map((r2) =>
          String(r2.path).split(/[\\/]/).pop()
          + (r2.error ? " (" + r2.error + ")" : "")).join("; ");
      toast("Some old copies could not be deleted", true);
    } else {
      resEl.textContent = "Deleted " + gone.length
        + (gone.length === 1 ? " folder" : " folders") + " ✅";
      toast("Old copies deleted");
    }
    const norm = (p) => String(p).replace(/\\/g, "/")
      .replace(/\/+$/, "").toLowerCase();
    oldCopiesFound = oldCopiesFound.filter(
      (c) => !gone.some((g) => norm(g.path) === norm(c.path)));
    renderOldCopies();
  } catch (e) {
    resEl.textContent = "Delete failed: " + e.message;
    toast("Delete failed", true);
  }
  btn.disabled = false;
  syncOldCopiesDeleteBtn();
});

/* ---------------- uninstall (Settings danger zone) ----------------
   The server writes a detached cleanup script and exits; the modal
   only collects the remove-data choice and shows the goodbye line.
   From a source checkout the server refuses, and the error is
   surfaced as a toast like any other failure. */
$("uninstallBtn").addEventListener("click", () => {
  $("uninstallRemoveData").checked = false;
  hide($("uninstallDoneText"));
  show($("uninstallActions"));
  const btn = $("uninstallConfirmBtn");
  btn.disabled = false;
  btn.textContent = "Yes, uninstall";
  show($("uninstallModal"));
});
$("uninstallCancelBtn").addEventListener("click",
  () => hide($("uninstallModal")));
$("uninstallConfirmBtn").addEventListener("click", async () => {
  const btn = $("uninstallConfirmBtn");
  btn.disabled = true;
  btn.textContent = "Uninstalling…";
  try {
    const res = await api.uninstall_self(
      $("uninstallRemoveData").checked);
    if (!res || !res.ok)
      throw new Error((res && res.error) || "uninstall refused");
    hide($("uninstallActions"));
    show($("uninstallDoneText"));
    // The app exits on its own; in Chrome mode the tab can linger
    // on a dead server, so try to close it too.
    setTimeout(() => { try { window.close(); } catch (e) { /* noop */ } },
               1800);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Yes, uninstall";
    toast("Uninstall failed: " + e.message, true);
  }
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
document.querySelectorAll("#gpuSeg .seg").forEach((b) =>
  b.addEventListener("click", async () => {
    segSet("gpuSeg", b.dataset.value);
    settings.gpu_accel = b.dataset.value;
    try {
      await api.save_settings({ gpu_accel: settings.gpu_accel });
      // the mode change re-probes server-side; show the fresh truth
      const s = await api.get_settings();
      if (s && s.encoder_status)
        $("encoderStatus").textContent = s.encoder_status.label;
    } catch (e) { /* offline */ }
  }));
document.querySelectorAll("#responsiveSeg .seg").forEach((b) =>
  b.addEventListener("click", async () => {
    segSet("responsiveSeg", b.dataset.value);
    settings.responsive_render = b.dataset.value === "on";
    try { await api.save_settings({ responsive_render: settings.responsive_render }); } catch (e) { /* offline */ }
  }));
$("renderThreadsInput").addEventListener("change", async () => {
  let n = parseInt($("renderThreadsInput").value, 10);
  if (isNaN(n) || n < 0) n = 0;
  if (n > 64) n = 64;
  $("renderThreadsInput").value = n;
  settings.render_threads = n;
  try { await api.save_settings({ render_threads: n }); } catch (e) { /* offline */ }
});
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
$("themeQuickBtn").addEventListener("click", async () => {
  settings.theme = settings.theme === "light" ? "dark" : "light";
  segSet("themeSeg", settings.theme);
  applyTheme();
  try { await api.save_settings({ theme: settings.theme }); } catch (e) { /* offline */ }
});

/* ---------------- Publishing ----------------
   Social Accounts + publish queue over publishing.py. Credentials
   are write-only from here: the server never sends a token back, so
   every secret field renders empty with a "saved" hint. */
let pubState = null;
let pubTimer = null;
let pubProjectsCache = [];
let pubMeta = {};
let pubMetaTab = "facebook";
let pubMetaClip = "";       // "jobId:idx" the loaded variants belong to
let pubFocus = null;        // {jobId, renderIndex} from a Publish btn
let adPubPaused = false;

const PUB_LABELS = { facebook: "Facebook", instagram: "Instagram",
                     youtube: "YouTube", tiktok: "TikTok" };
const PUB_ID_HINT = { facebook: "Page ID", instagram: "Instagram user ID",
                      youtube: "Channel name (label only)",
                      tiktok: "TikTok open_id (label only)" };
const PUB_TOKEN_HINT = {
  facebook: "Page access token", instagram: "User access token",
  youtube: "OAuth refresh token", tiktok: "Access token" };

function stopPubPoll() {
  if (pubTimer) { clearInterval(pubTimer); pubTimer = null; }
}

function openPublishFor(jid, renderIndex) {
  pubFocus = { jobId: jid, renderIndex };
  showTool("publishing");
}

async function initPublishing() {
  await refreshPubState();
  await refreshPubProjects();
  await refreshPubJobs();
  stopPubPoll();
  pubTimer = setInterval(() => refreshPubJobs(true), 4000);
}

async function refreshPubState() {
  let res;
  try { res = await api.pub_state(); } catch (e) { return; }
  if (!res || !res.ok) return;
  pubState = res;
  renderPubAccounts();
  renderPubDestinations();
  renderPubConfig();
  renderPubPresets();
  $("pubPausedBanner").classList.toggle("hidden", !res.config.pause_all);
  $("pubPauseBtn").textContent = res.config.pause_all
    ? "Resume all" : "Pause all";
}

function pubStateBadge(state) {
  if (state === "connected") return '<span class="badge b-ok">Connected</span>';
  if (state === "token_expired") return '<span class="badge b-bad">Token expired</span>';
  if (state === "error") return '<span class="badge b-bad">Error</span>';
  if (state === "not_connected") return '<span class="badge b-warn">Not connected</span>';
  return '<span class="badge b-warn">Not configured</span>';
}

function renderPubAccounts() {
  const box = $("pubAccounts");
  box.innerHTML = "";
  (pubState.platforms || []).forEach((p) => {
    const card = document.createElement("div");
    card.className = "find-box pub-acct";
    const fields = (p.app_fields || []).map((f) => `
      <label class="enh-opt">${escapeHtml(f.label)}${f.set ? ' <span class="hint">saved ✓</span>' : ""}
        <input type="${f.secret ? "password" : "text"}" class="input pub-app-field" data-key="${escapeHtml(f.key)}" placeholder="${f.set ? "Saved — type to replace" : "Not set"}" spellcheck="false"></label>`).join("");
    const accounts = (p.accounts || []).map((a) => `
      <div class="pub-acct-row">
        <span>${a.is_default ? "⭐ " : ""}<b>${escapeHtml(a.label || "Account")}</b>
          <span class="hint">${escapeHtml(a.external_id || "")}</span>
          ${a.has_token ? '<span class="badge b-ok">token saved</span>' : '<span class="badge b-warn">no token</span>'}
          ${a.last_test && a.last_test.detail ? `<div class="hint">Last test: ${escapeHtml(a.last_test.detail)}</div>` : ""}
        </span>
        <span class="row row-tight">
          <button type="button" class="ghost-btn small pub-test-btn" data-acct="${escapeHtml(a.id)}">Test connection</button>
          <button type="button" class="ghost-btn small pub-default-btn" data-acct="${escapeHtml(a.id)}">Make default</button>
          <button type="button" class="ghost-btn small pub-disconnect-btn" data-acct="${escapeHtml(a.id)}">Disconnect</button>
          <button type="button" class="ghost-btn small pub-delete-btn" data-acct="${escapeHtml(a.id)}">Remove</button>
        </span>
      </div>`).join("");
    card.innerHTML = `
      <div class="row-between"><div class="field-label" style="margin:0">${escapeHtml(p.label)}</div>${pubStateBadge(p.state)}</div>
      ${p.note ? `<p class="hint">⚠ ${escapeHtml(p.note)}</p>` : ""}
      ${p.missing && p.missing.length ? `<p class="hint">Missing: ${escapeHtml(p.missing.join(", "))}.</p>` : ""}
      <div class="row row-tight" style="margin-top:8px">${fields}
        <label class="enh-opt">API base URL <span class="hint">— default ${escapeHtml(p.base)}</span>
          <input type="text" class="input pub-base-field" placeholder="${escapeHtml(p.base)}" spellcheck="false"></label>
        <button type="button" class="ghost-btn pub-save-creds">Save app credentials</button>
      </div>
      <div style="margin-top:10px">${accounts || '<p class="hint">No account saved yet.</p>'}</div>
      <div class="row row-tight" style="margin-top:8px">
        <label class="enh-opt">Label <input type="text" class="input pub-acct-label" placeholder="My page" style="max-width:150px"></label>
        <label class="enh-opt">${escapeHtml(PUB_ID_HINT[p.platform])} <input type="text" class="input pub-acct-ext" spellcheck="false" style="max-width:170px"></label>
        <label class="enh-opt">${escapeHtml(PUB_TOKEN_HINT[p.platform])} <input type="password" class="input pub-acct-token" spellcheck="false" style="max-width:200px"></label>
        <button type="button" class="ghost-btn pub-add-acct">Add account</button>
      </div>
      <details style="margin-top:8px"><summary class="hint">How to connect ${escapeHtml(p.label)}</summary>
        <ol class="hint">${(p.setup_steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol></details>`;
    card.querySelector(".pub-save-creds").addEventListener("click", async () => {
      const out = {};
      card.querySelectorAll(".pub-app-field").forEach((inp) => {
        if (inp.value.trim()) out[inp.dataset.key] = inp.value.trim();
      });
      out.base = card.querySelector(".pub-base-field").value.trim();
      const baseKey = { facebook: "pub_fb_base", instagram: "pub_fb_base",
                        youtube: "pub_yt_base", tiktok: "pub_tt_base" }[p.platform];
      const payload = Object.assign({}, out);
      delete payload.base;
      payload[baseKey] = out.base || "";
      // base may legitimately be cleared back to the platform default
      const res = await api.pub_save_app_creds(p.platform, payload);
      if (res && res.ok) { pubState = res; renderPubAccounts(); renderPubDestinations(); renderPubConfig(); renderPubPresets(); toast(p.label + " credentials saved"); }
      else toast("Save failed", true);
    });
    card.querySelector(".pub-add-acct").addEventListener("click", async () => {
      const label = card.querySelector(".pub-acct-label").value.trim();
      const ext = card.querySelector(".pub-acct-ext").value.trim();
      const token = card.querySelector(".pub-acct-token").value.trim();
      if (!token && !ext && !label) { toast("Fill in the account fields first", true); return; }
      const res = await api.pub_save_account(p.platform,
        { label, external_id: ext, token });
      if (res && res.ok) { pubState = res; renderPubAccounts(); renderPubDestinations(); toast("Account saved"); }
      else toast("Save failed", true);
    });
    card.querySelectorAll(".pub-test-btn").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "Testing…";
        const res = await api.pub_test_connection(p.platform, b.dataset.acct);
        toast(res && res.ok ? ("Connected ✓ " + (res.detail || ""))
          : ("Not connected: " + ((res && (res.error || res.detail)) || "unknown error")), !(res && res.ok));
        await refreshPubState();
      }));
    card.querySelectorAll(".pub-default-btn").forEach((b) =>
      b.addEventListener("click", async () => {
        await api.pub_set_default_account(p.platform, b.dataset.acct);
        await refreshPubState();
      }));
    card.querySelectorAll(".pub-disconnect-btn").forEach((b) =>
      b.addEventListener("click", async () => {
        await api.pub_disconnect(p.platform, b.dataset.acct);
        await refreshPubState();
        toast("Disconnected — token wiped");
      }));
    card.querySelectorAll(".pub-delete-btn").forEach((b) =>
      b.addEventListener("click", async () => {
        await api.pub_delete_account(p.platform, b.dataset.acct);
        await refreshPubState();
      }));
    box.appendChild(card);
  });
}

async function refreshPubProjects() {
  let res;
  try { res = await api.pub_projects(); } catch (e) { return; }
  if (!res || !res.ok) return;
  pubProjectsCache = res.projects || [];
  const sel = $("pubProjectSel");
  const keep = pubFocus ? pubFocus.jobId : sel.value;
  sel.innerHTML = pubProjectsCache.map((p2) =>
    `<option value="${escapeHtml(p2.job_id)}">${escapeHtml(p2.title)} (${p2.renders.length} clips)</option>`).join("");
  if (keep && pubProjectsCache.some((p2) => p2.job_id === keep))
    sel.value = keep;
  renderPubClips();
}

function pubSelectedProject() {
  const jid = $("pubProjectSel").value;
  return pubProjectsCache.find((p2) => p2.job_id === jid) || null;
}

function renderPubClips() {
  const box = $("pubClipList");
  const proj = pubSelectedProject();
  box.innerHTML = "";
  if (!proj) {
    box.innerHTML = '<p class="hint">No projects with rendered clips yet — render clips in Clip Maker first.</p>';
    return;
  }
  proj.renders.forEach((r) => {
    const row = document.createElement("label");
    row.className = "pub-clip-row";
    const checked = pubFocus && pubFocus.jobId === proj.job_id
      && pubFocus.renderIndex != null
      ? r.idx === pubFocus.renderIndex : true;
    row.innerHTML = `
      <input type="checkbox" class="pub-clip-chk" data-idx="${r.idx}" ${checked ? "checked" : ""}>
      ${r.thumb_url ? `<img class="pub-thumb" src="${escapeHtml(r.thumb_url)}" alt="">` : ""}
      <span>${escapeHtml(r.title)}</span>`;
    box.appendChild(row);
  });
  if (pubFocus) pubFocus = null;
}

function renderPubDestinations() {
  const box = $("pubDestList");
  box.innerHTML = "";
  (pubState.platforms || []).forEach((p) => {
    if (!p.accounts || !p.accounts.length) {
      const row = document.createElement("div");
      row.className = "pub-dest-row";
      row.innerHTML = `<label class="pub-check"><input type="checkbox" disabled> ${escapeHtml(p.label)} <span class="hint">— no account connected</span></label>`;
      box.appendChild(row);
      return;
    }
    p.accounts.forEach((a) => {
      const row = document.createElement("div");
      row.className = "pub-dest-row";
      row.innerHTML = `<label class="pub-check"><input type="checkbox" class="pub-dest-chk" data-platform="${escapeHtml(p.platform)}" data-acct="${escapeHtml(a.id)}" ${a.is_default ? "checked" : ""}> ${escapeHtml(p.label)} — ${escapeHtml(a.label || a.external_id || "Account")}${a.has_token ? "" : ' <span class="hint">(no token — job will wait for connection)</span>'}</label>`;
      box.appendChild(row);
    });
  });
}

function pubCollectMeta() {
  pubMeta[pubMetaTab] = {
    title: $("pubMetaTitle").value,
    description: $("pubMetaDesc").value,
    caption: $("pubMetaCaption").value,
    hashtags: $("pubMetaTags").value.split(/[\s,]+/)
      .map((t) => t.trim()).filter(Boolean)
      .map((t) => (t.startsWith("#") ? t : "#" + t)),
  };
}

function pubShowMetaTab(platform) {
  pubCollectMeta();
  pubMetaTab = platform;
  document.querySelectorAll(".pub-tab").forEach((b) =>
    b.classList.toggle("on", b.dataset.pubtab === platform));
  const m = pubMeta[platform] || {};
  $("pubMetaTitle").value = m.title || "";
  $("pubMetaDesc").value = m.description || "";
  $("pubMetaCaption").value = m.caption || "";
  $("pubMetaTags").value = (m.hashtags || []).join(" ");
  $("pubMetaTitleHint").textContent = platform === "youtube"
    ? "— max 100 characters for Shorts/SEO" : "";
}

document.querySelectorAll(".pub-tab").forEach((b) =>
  b.addEventListener("click", () => pubShowMetaTab(b.dataset.pubtab)));

async function pubLoadAssistant() {
  const proj = pubSelectedProject();
  const first = document.querySelector(".pub-clip-chk:checked");
  if (!proj || !first) { toast("Pick a project and tick a clip first", true); return; }
  const idx = parseInt(first.dataset.idx, 10);
  const res = await api.pub_assistant(proj.job_id, idx);
  if (!res || !res.ok) { toast((res && res.error) || "Assistant failed", true); return; }
  pubMetaClip = proj.job_id + ":" + idx;
  Object.entries(res.variants || {}).forEach(([p, v]) => {
    pubMeta[p] = { title: v.title || "", description: v.description || "",
                   caption: v.caption || "", hashtags: v.hashtags || [] };
  });
  pubShowMetaTab(pubMetaTab);
  const a = res.assistant || {};
  $("pubAssistBox").classList.remove("hidden");
  $("pubAssistNote").textContent = a.note || "";
  const titles = $("pubAssistTitles");
  titles.innerHTML = "";
  (a.titles || []).forEach((t) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ghost-btn small";
    b.textContent = t;
    b.addEventListener("click", () => { $("pubMetaTitle").value = t; pubCollectMeta(); });
    titles.appendChild(b);
  });
  const caps = $("pubAssistCaptions");
  caps.innerHTML = "";
  (a.captions || []).forEach((c) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ghost-btn small";
    b.textContent = c.length > 90 ? c.slice(0, 90) + "…" : c;
    b.addEventListener("click", () => {
      if (pubMetaTab === "youtube") $("pubMetaDesc").value = c;
      else $("pubMetaCaption").value = c;
      pubCollectMeta();
    });
    caps.appendChild(b);
  });
  const slots = a.recommended_slots || {};
  $("pubAssistRec").textContent =
    "Suggested first platform: " + (PUB_LABELS[a.recommended_platform] || a.recommended_platform || "—")
    + " · Suggested times — " + Object.entries(slots)
      .map(([p, s]) => (PUB_LABELS[p] || p) + " " + s).join(" · ")
    + " (suggestions only)";
  const gates = res.per_platform_gates || {};
  const g = gates[pubMetaTab] || [];
  $("pubMetaGate").textContent = g.length
    ? "⚠ This clip can't go to " + PUB_LABELS[pubMetaTab] + ": " + g.join(" ")
    : "";
  toast("Assistant loaded — variants differ per platform tab");
}

$("pubAssistBtn").addEventListener("click", pubLoadAssistant);
$("pubProjectSel").addEventListener("change", renderPubClips);
$("pubReloadProjectsBtn").addEventListener("click", refreshPubProjects);

function pubSelectedPreset() {
  const id = $("pubPresetSel").value;
  if (!id || !pubState) return null;
  return (pubState.config.presets || []).find((p) => p.id === id) || null;
}

async function pubCreateJobs(scheduled) {
  pubCollectMeta();
  const proj = pubSelectedProject();
  if (!proj) { toast("Pick a project first", true); return; }
  const clips = [...document.querySelectorAll(".pub-clip-chk:checked")]
    .map((c) => parseInt(c.dataset.idx, 10));
  const dests = [...document.querySelectorAll(".pub-dest-chk:checked")]
    .map((c) => ({ platform: c.dataset.platform, account_id: c.dataset.acct }));
  if (!clips.length) { toast("Tick at least one clip", true); return; }
  if (!dests.length) { toast("Tick at least one destination", true); return; }
  let scheduledAt = null;
  if (scheduled) {
    const v = $("pubWhenInput").value;
    if (!v) { toast("Pick a date and time to schedule", true); return; }
    scheduledAt = Math.floor(new Date(v).getTime() / 1000);
    if (!(scheduledAt > Date.now() / 1000)) {
      toast("That time is in the past", true); return;
    }
  }
  const preset = pubSelectedPreset();
  const items = [];
  clips.forEach((idx, ci) => {
    dests.forEach((d) => {
      const item = { job_id: proj.job_id, render_index: idx,
                     platform: d.platform, account_id: d.account_id,
                     scheduled_at: scheduledAt,
                     override: $("pubOverrideChk").checked,
                     source: "manual",
                     approval: preset ? preset.approval : "" };
      // The hand-edited wording applies to the first clip; other
      // clips get their own server-generated per-platform text.
      if (ci === 0 && pubMetaClip === proj.job_id + ":" + idx
          && pubMeta[d.platform]) {
        item.metadata = Object.assign({}, pubMeta[d.platform],
          { visibility: preset ? preset.visibility : undefined });
        if (!item.metadata.visibility) delete item.metadata.visibility;
      }
      items.push(item);
    });
  });
  const res = await api.pub_create_jobs(items);
  if (!res || !res.ok) {
    $("pubCreateMsg").textContent = (res && res.error) || "Queueing failed.";
    return;
  }
  const parts = [];
  if (res.created.length) parts.push(res.created.length + " job(s) queued.");
  (res.refused || []).forEach((r) =>
    parts.push("Skipped (" + (PUB_LABELS[r.platform] || r.platform || "") + "): " + r.reason));
  $("pubCreateMsg").textContent = parts.join(" ");
  toast(res.created.length ? "Queued " + res.created.length + " job(s)" : "Nothing queued — see reasons below", !res.created.length);
  await refreshPubJobs();
}

$("pubNowBtn").addEventListener("click", () => pubCreateJobs(false));
$("pubScheduleBtn").addEventListener("click", () => pubCreateJobs(true));

const PUB_STATUS_BADGE = {
  published: "b-ok", failed: "b-bad", uploading: "b-info",
  publishing: "b-info", ready: "b-info", scheduled: "b-warn",
  waiting: "b-warn", awaiting_approval: "b-warn",
  awaiting_connection: "b-warn", paused: "b-warn", cancelled: "b-warn",
};

async function refreshPubJobs(quiet) {
  let res;
  try {
    res = await api.pub_jobs($("pubFilterPlatform").value,
                             $("pubFilterStatus").value);
  } catch (e) { return; }
  if (!res || !res.ok) return;
  const box = $("pubJobsList");
  box.innerHTML = "";
  if (!res.jobs.length) {
    box.innerHTML = '<p class="hint">No jobs match. Queue one above — or finish a render and hit Publish on the clip.</p>';
    return;
  }
  res.jobs.forEach((j) => {
    const row = document.createElement("div");
    row.className = "pub-job-row";
    const acts = [];
    if (j.status === "awaiting_approval") acts.push(["approve", "Approve"]);
    if (!["published", "failed", "cancelled"].includes(j.status)) acts.push(["pause", "Pause"]);
    if (j.status === "paused") acts.push(["resume", "Resume"]);
    if (["failed", "awaiting_connection", "cancelled"].includes(j.status)) acts.push(["retry", "Retry"]);
    if (j.status !== "published") acts.push(["cancel", "Cancel"]);
    row.innerHTML = `
      ${j.thumb_url ? `<img class="pub-thumb" src="${escapeHtml(j.thumb_url)}" alt="">` : ""}
      <div class="pub-job-main">
        <div><b>${escapeHtml(j.title || "")}</b> <span class="badge ${PUB_STATUS_BADGE[j.status] || "b-warn"}">${escapeHtml(j.status.replace(/_/g, " "))}</span></div>
        <div class="hint">${escapeHtml(PUB_LABELS[j.platform] || j.platform)} · ${escapeHtml(j.account_label || "")} · attempts ${j.attempts || 0}${j.scheduled_at ? " · scheduled " + escapeHtml(fmtWhen(j.scheduled_at)) : ""}</div>
        ${j.published_url ? `<div><a href="${escapeHtml(j.published_url)}" target="_blank" rel="noopener">${escapeHtml(j.published_url)}</a></div>` : ""}
        ${j.last_error ? `<div class="hint" style="color:var(--bad,#e66)">⚠ ${escapeHtml(j.last_error)}</div>` : ""}
      </div>
      <div class="row row-tight">${acts.map(([a, label]) =>
        `<button type="button" class="ghost-btn small pub-job-act" data-act="${a}" data-id="${escapeHtml(j.id)}">${label}</button>`).join("")}</div>`;
    box.appendChild(row);
  });
  box.querySelectorAll(".pub-job-act").forEach((b) =>
    b.addEventListener("click", async () => {
      const r = await api.pub_job_action(b.dataset.id, b.dataset.act);
      if (r && !r.ok) toast(r.error || "Action failed", true);
      await refreshPubJobs();
    }));
  if (!quiet) { /* manual refresh also syncs the pause banner */ }
}

$("pubRefreshBtn").addEventListener("click", () => refreshPubJobs());
$("pubFilterPlatform").addEventListener("change", () => refreshPubJobs());
$("pubFilterStatus").addEventListener("change", () => refreshPubJobs());

async function pubTogglePauseAll() {
  const next = !(pubState && pubState.config.pause_all);
  const res = await api.pub_set_pause_all(next);
  if (res && res.ok) {
    toast(next ? "Publishing paused" : "Publishing resumed");
    await refreshPubState();
  }
}
$("pubPauseBtn").addEventListener("click", pubTogglePauseAll);
$("pubResumeAllBtn").addEventListener("click", pubTogglePauseAll);

function renderPubConfig() {
  const c = pubState.config;
  $("pubEnabledChk").checked = !!c.enabled;
  $("pubApprovalSel").value = c.approval_mode || "manual";
  $("pubMaxBatchInput").value = c.max_batch ?? 10;
  $("pubDailyLimitInput").value = c.daily_limit ?? 0;
  $("pubDelayInput").value = c.delay_s ?? 0;
  $("pubVisibilitySel").value = c.default_visibility || "public";
  $("pubTtAuditedChk").checked = !!c.tt_audited;
  $("pubWebhookUrl").value = c.webhook_url || "";
  $("pubWebhookSecretState").textContent = c.webhook_secret_set
    ? "— a secret is saved; type to replace" : "— none saved";
}

$("pubSaveConfigBtn").addEventListener("click", async () => {
  const patch = {
    enabled: $("pubEnabledChk").checked,
    approval_mode: $("pubApprovalSel").value,
    max_batch: parseInt($("pubMaxBatchInput").value, 10) || 10,
    daily_limit: parseInt($("pubDailyLimitInput").value, 10) || 0,
    delay_s: parseInt($("pubDelayInput").value, 10) || 0,
    default_visibility: $("pubVisibilitySel").value,
    tt_audited: $("pubTtAuditedChk").checked,
    webhook_url: $("pubWebhookUrl").value.trim(),
    default_preset: $("pubDefaultPresetSel").value,
  };
  const secret = $("pubWebhookSecret").value;
  if (secret) patch.webhook_secret = secret;
  const res = await api.pub_save_config(patch);
  if (res && res.ok) {
    pubState = res;
    $("pubWebhookSecret").value = "";
    renderPubAccounts(); renderPubDestinations(); renderPubConfig(); renderPubPresets();
    toast("Publishing settings saved");
  } else toast("Save failed", true);
});

function renderPubPresets() {
  const presets = (pubState.config && pubState.config.presets) || [];
  const mk = (sel, blankLabel) => {
    const keep = sel.value;
    sel.innerHTML = `<option value="">${blankLabel}</option>` + presets.map((p) =>
      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join("");
    if (presets.some((p) => p.id === keep)) sel.value = keep;
  };
  mk($("pubPresetSel"), "No preset (use settings)");
  mk($("pubDefaultPresetSel"), "None — no auto-publish");
  if (pubState.config.default_preset)
    $("pubDefaultPresetSel").value = pubState.config.default_preset;
  const list = $("pubPresetList");
  list.innerHTML = presets.length ? "" : '<p class="hint">No presets yet.</p>';
  presets.forEach((p) => {
    const row = document.createElement("div");
    row.className = "pub-acct-row";
    row.innerHTML = `<span><b>${escapeHtml(p.name)}</b> <span class="hint">${(p.platforms || []).map((x) => PUB_LABELS[x] || x).join(", ")} · ${escapeHtml(p.approval)} · ${escapeHtml(p.visibility)}${p.delay_minutes ? " · +" + p.delay_minutes + " min" : ""}</span></span>`;
    const del = document.createElement("button");
    del.type = "button"; del.className = "ghost-btn small"; del.textContent = "Delete";
    del.addEventListener("click", async () => {
      const left = presets.filter((x) => x.id !== p.id);
      const res = await api.pub_save_config({ presets: left });
      if (res && res.ok) { pubState = res; renderPubPresets(); }
    });
    row.appendChild(del);
    list.appendChild(row);
  });
}

$("pubPresetAddBtn").addEventListener("click", async () => {
  const name = $("pubPresetName").value.trim();
  if (!name) { toast("Name the preset first", true); return; }
  const platforms = [...document.querySelectorAll(
    "#pubPresetPlatforms input:checked")].map((c) => c.value);
  if (!platforms.length) { toast("Tick at least one platform", true); return; }
  const presets = [...((pubState.config && pubState.config.presets) || []),
    { name, platforms, approval: $("pubPresetApproval").value,
      visibility: "public",
      delay_minutes: parseInt($("pubPresetDelay").value, 10) || 0 }];
  const res = await api.pub_save_config({ presets });
  if (res && res.ok) {
    pubState = res;
    $("pubPresetName").value = "";
    renderPubPresets();
    toast("Preset saved");
  } else toast("Save failed", true);
});

/* Admin Panel — the Publishing pause-all kill switch (owner session
   enforced server-side on /api/admin/*). */
$("adPubPauseBtn").addEventListener("click", async () => {
  const r = await adminFetch("/api/admin/pub-pause",
    { method: "POST", body: JSON.stringify({ paused: !adPubPaused }) });
  if (r.status === 200 && r.data && r.data.ok) {
    toast(adPubPaused ? "Publishing resumed" : "All publishing paused");
    refreshAdmin();
  } else {
    toast("Admin session ended — unlock again.", true);
  }
});

/* ---------------- Admin Panel (owner only) ----------------
   The server is the gatekeeper: /api/admin/* answers 403 without
   a live server-side session, and only a license key carrying the
   signed owner claim can start one. Everything here is display. */
let adminStatsCache = null;
let adminRange = "daily";

async function adminFetch(path, opts) {
  const r = await fetch(path, Object.assign(
    { headers: { "Content-Type": "application/json" } }, opts || {}));
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-JSON reply */ }
  return { status: r.status, data };
}

async function openAdmin() {
  try {
    const s = await adminFetch("/api/admin/session");
    if (s.status === 200 && s.data.active) { enterAdmin(); return; }
  } catch (e) { /* fall through to the unlock dialog */ }
  hide($("adminUnlockError"));
  $("adminKeyInput").value = "";
  show($("adminUnlockModal"));
  setTimeout(() => $("adminKeyInput").focus(), 60);
}

function enterAdmin() {
  show($("navAdminBtn"));
  showTool("admin");
}

function adminLockedUi(msg) {
  hide($("navAdminBtn"));
  if (toolOpen === "admin") goHome();
  if (msg) toast(msg);
}

async function checkAdminSession() {
  try {
    const s = await adminFetch("/api/admin/session");
    if (s.status === 200 && s.data.active) show($("navAdminBtn"));
  } catch (e) { /* panel simply stays hidden */ }
}

async function submitAdminUnlock() {
  const key = $("adminKeyInput").value.trim();
  const err = $("adminUnlockError");
  if (!key) {
    err.textContent = "Paste your owner key first.";
    show(err);
    return;
  }
  const btn = $("adminUnlockBtn");
  btn.disabled = true;
  try {
    const r = await adminFetch("/api/admin/unlock", {
      method: "POST", body: JSON.stringify({ key }) });
    if (r.status === 200 && r.data.ok) {
      hide($("adminUnlockModal"));
      toast("Admin Panel unlocked — welcome, "
        + (r.data.holder || "owner") + ".");
      enterAdmin();
    } else {
      err.textContent = (r.data && r.data.error)
        || "That key doesn't unlock the Admin Panel.";
      show(err);
    }
  } catch (e) {
    err.textContent = "Couldn't reach the app to check that key.";
    show(err);
  } finally {
    btn.disabled = false;
  }
}

$("adminUnlockBtn").addEventListener("click", submitAdminUnlock);
$("adminUnlockCancelBtn").addEventListener("click",
  () => hide($("adminUnlockModal")));
$("adminKeyInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitAdminUnlock();
});
$("settingsAdminBtn").addEventListener("click", () => {
  closeModal("settingsModal");
  openAdmin();
});
$("adminRefreshBtn").addEventListener("click", () => refreshAdmin());
$("adminLockBtn").addEventListener("click", async () => {
  try {
    await adminFetch("/api/admin/logout", { method: "POST", body: "{}" });
  } catch (e) { /* the UI locks either way */ }
  adminLockedUi("Admin Panel locked.");
});
document.querySelectorAll("#adChartSeg button").forEach((b) =>
  b.addEventListener("click", () => {
    adminRange = b.dataset.range;
    document.querySelectorAll("#adChartSeg button").forEach((x) =>
      x.classList.toggle("seg-on", x === b));
    renderAdminChart();
  }));

function initAdmin() { refreshAdmin(); }

function fmtUptime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60);
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + m + "m";
  return m + "m";
}

function fmtWhen(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtLatency(ms) {
  if (ms == null) return "—";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + " s";
  return Math.round(ms) + " ms";
}

function statusBadge(status) {
  const s = String(status || "").toLowerCase();
  if (["done", "ok", "completed"].includes(s)) return "b-ok";
  if (["error", "failed"].includes(s)) return "b-bad";
  if (["running", "working", "queued", "downloading"].includes(s))
    return "b-info";
  return "b-warn";
}

async function refreshAdmin() {
  let statsRes, logRes;
  try {
    [statsRes, logRes] = await Promise.all([
      adminFetch("/api/admin/stats"),
      adminFetch("/api/admin/api-log")]);
  } catch (e) {
    toast("Couldn't load the Admin Panel data.");
    return;
  }
  if (statsRes.status === 403 || logRes.status === 403) {
    adminLockedUi("Admin session ended — unlock again to reopen "
      + "the panel.");
    return;
  }
  adminStatsCache = statsRes.data;
  renderAdminStats(statsRes.data);
  renderAdminLog(logRes.data);
}

function dlRows(pairs) {
  return pairs.map(([k, v]) =>
    "<dt>" + escapeHtml(k) + "</dt><dd>" + escapeHtml(String(v))
    + "</dd>").join("");
}

function renderAdminStats(d) {
  const st = d.stats || {}, lic = d.license || {},
    sys = d.system || {}, store = d.storage || {};
  $("adStatVideos").textContent = st.videos_processed ?? "–";
  $("adStatClips").textContent = st.clips_generated ?? "–";
  $("adStatDownloads").textContent = st.downloads ?? "–";
  $("adStatDownloadsNote").textContent = st.counters_since
    ? "(tracked since the Admin Panel was added)" : "";
  const kinds = st.ai_jobs || {};
  const aiTotal = Object.values(kinds).reduce((a, b) => a + b, 0);
  $("adStatAiJobs").textContent = aiTotal;
  $("adStatFailed").textContent = st.failed_count ?? "–";
  $("adStatStorage").textContent = fmtBytes(store.work_bytes || 0);
  $("adStatUptime").textContent = fmtUptime(d.uptime_s);
  const sinceTxt = st.counters_since
    ? new Date(st.counters_since * 1000).toLocaleDateString() : "—";
  $("adminSessionNote").textContent =
    "Run counters are tracked since " + sinceTxt + ": Built-in AI "
    + "detections " + (kinds.detect_local || 0) + " · Offline "
    + "detections " + (kinds.detect_offline || 0) + " · Generator "
    + (kinds.generator || 0) + " · Enhancer " + (kinds.enhancer || 0)
    + " · Caption Remover " + (kinds.remover || 0) + ".";

  $("adLicenseList").innerHTML = dlRows([
    ["License holder", lic.holder || "—"],
    ["Role", lic.role === "owner" ? "Owner" : "Customer"],
    ["Key type", lic.key_type || "—"],
    ["Works on", lic.machine_claim === "ANY"
      ? "Any PC (universal key)" : "One PC (machine-bound)"],
    ["This PC's code", lic.machine_code || "—"],
    ["Expires", !lic.expires ? "—"
      : (lic.expires === "never" ? "Never" : lic.expires)],
    ["Activation", lic.unlocked ? "Unlocked" : "Locked"],
    ["App version", "v" + (lic.app_version || "—")],
    ["Built-in AI model", lic.model_ready ? "Downloaded ✓"
      : (lic.model_downloading ? "Downloading…" : "Not downloaded")],
    ["fal.ai key", lic.fal_key_set ? "Saved ✓" : "Not set"],
  ]);
  const usageStateTxt = {
    unlimited: "Unlimited key — no counting",
    ok: "Counting normally",
    limit: "Limit reached — new clip renders paused",
    tampered: "Usage data problem — record failed its integrity check",
  }[lic.usage_state || "unlimited"];
  $("adLimitsList").innerHTML = dlRows([
    ["Key holder", lic.holder || "—"],
    ["Key type", (lic.key_type || "—")
      + (lic.role === "owner" ? " · Owner" : "")],
    ["Clip allowance", lic.clip_allowance == null
      ? "Unlimited" : lic.clip_allowance + " clips"],
    ["Clips used (this PC)", lic.clips_used == null
      ? "—" : String(lic.clips_used)],
    ["Clips remaining", lic.clips_remaining == null
      ? (lic.clip_allowance == null ? "Unlimited" : "—")
      : String(lic.clips_remaining)],
    ["Usage record", usageStateTxt],
  ]);
  const pub = d.publishing || {};
  const pubByStatus = Object.entries(pub.by_status || {})
    .map(([k, v]) => k + " " + v).join(" · ") || "—";
  const pubByPlatform = Object.entries(pub.by_platform || {})
    .map(([k, v]) => k + ": " + (v.published || 0) + "/"
      + (v.total || 0) + " published").join(" · ") || "—";
  $("adPubList").innerHTML = dlRows([
    ["Total jobs", String(pub.total ?? 0)],
    ["Published", String(pub.published ?? 0)],
    ["Failed", String(pub.failed ?? 0)],
    ["By status", pubByStatus],
    ["By platform", pubByPlatform],
    ["Worker", pub.pause_all ? "PAUSED (all publishing)"
      : "Running"],
    ["Completion outbox", String(pub.outbox ?? 0) + " events"],
  ]);
  adPubPaused = !!pub.pause_all;
  $("adPubPauseBtn").textContent = adPubPaused
    ? "Resume all publishing" : "Pause all publishing";
  $("adPubErrors").innerHTML = (pub.recent_errors || []).length
    ? (pub.recent_errors || []).map((e) =>
      `<div class="prov-row"><span class="prov-name">${escapeHtml(e.platform || "")} — ${escapeHtml(e.title || "")}</span><span class="prov-note">${escapeHtml(e.error || "")}</span></div>`).join("")
    : '<p class="hint">No publishing errors recorded.</p>';
  const ram = (sys.ram_total
    ? fmtBytes(sys.ram_free || 0) + " free of "
      + fmtBytes(sys.ram_total) : "—");
  const disk = (sys.disk_total
    ? fmtBytes(sys.disk_free || 0) + " free of "
      + fmtBytes(sys.disk_total) : "—");
  $("adSystemList").innerHTML = dlRows([
    ["System", sys.os || "—"],
    ["CPU cores", sys.cpu_count ?? "—"],
    ["RAM", ram],
    ["Disk", disk],
    ["Projects storage", fmtBytes(store.work_bytes || 0)],
    ["Projects folder", (store.work_roots || []).join("  ·  ") || "—"],
    ["AI models storage", fmtBytes(store.models_bytes || 0)],
    ["Models folder", store.models_dir || "—"],
    ["App folder size", fmtBytes(store.app_bytes || 0)],
  ]);
  renderAdminChart();
  const tb = $("adActivityBody");
  const rows = st.activity || [];
  tb.innerHTML = rows.length ? rows.map((r) =>
    "<tr><td>" + escapeHtml(fmtWhen(r.ts)) + "</td><td>"
    + escapeHtml(r.tool || "") + "</td><td>"
    + escapeHtml(String(r.title || "").slice(0, 60))
    + (r.error ? "<div class=\"hint\">"
      + escapeHtml(String(r.error).slice(0, 120)) + "</div>" : "")
    + "</td><td><span class=\"badge " + statusBadge(r.status)
    + "\">" + escapeHtml(r.status || "—") + "</span></td></tr>"
  ).join("") : "<tr><td colspan=\"4\" class=\"hint\">No activity "
    + "recorded yet.</td></tr>";
}

function providerCard(name, statusHtml, lines) {
  return "<div class=\"admin-provider\"><h3>" + escapeHtml(name)
    + "</h3>" + statusHtml
    + lines.map((l) => "<p class=\"sub\">" + l + "</p>").join("")
    + "</div>";
}

function renderAdminLog(d) {
  const providers = d.providers || [];
  const byName = {};
  providers.forEach((p) => { byName[p.provider] = p; });
  const totalCalls = providers.reduce((a, p) => a + (p.calls || 0), 0);
  $("adStatLogCalls").textContent = totalCalls;

  const cards = [];
  const fal = byName["fal.ai"];
  const lic = (adminStatsCache && adminStatsCache.license) || {};
  cards.push(providerCard("fal.ai — AI Video Generator",
    "<p class=\"sub\">API key: <b>" + (lic.fal_key_set
      ? "saved on this PC" : "not set") + "</b></p>",
    fal ? [
      "Calls logged: <b>" + fal.calls + "</b> · success rate <b>"
        + (fal.success_rate ?? "—") + "%</b>",
      "Avg response: <b>" + fmtLatency(fal.avg_latency_ms) + "</b>"
        + " · last call " + fmtWhen(fal.last_ts) + " ("
        + escapeHtml(fal.last_status || "—") + ")",
      fal.last_error ? "Last error: "
        + escapeHtml(String(fal.last_error).slice(0, 140)) : "",
      "Credits: <b>" + (fal.credits_reported
        ? "reported by provider" : "not reported by provider")
        + "</b>",
    ].filter(Boolean) : ["No fal.ai calls logged yet."]));
  const local = byName["Built-in AI (local)"];
  cards.push(providerCard("Built-in AI (local model)",
    "<p class=\"sub\">Model: <b>" + (lic.model_ready
      ? "downloaded and ready" : "not downloaded") + "</b> · runs "
      + "fully offline on this PC</p>",
    local ? [
      "Calls logged: <b>" + local.calls + "</b> · success rate <b>"
        + (local.success_rate ?? "—") + "%</b>",
      "Avg response: <b>" + fmtLatency(local.avg_latency_ms) + "</b>"
        + " · last call " + fmtWhen(local.last_ts),
      local.last_error ? "Last error: "
        + escapeHtml(String(local.last_error).slice(0, 140)) : "",
      "Credits: <b>none — local compute</b>",
    ].filter(Boolean) : ["No Built-in AI calls logged yet."]));
  $("adProviders").innerHTML = cards.join("");

  const entries = d.entries || [];
  $("adApiLogBody").innerHTML = entries.length ? entries.map((e) =>
    "<tr><td>" + escapeHtml(fmtWhen(e.ts)) + "</td><td>"
    + escapeHtml(e.provider || "") + "</td><td>"
    + escapeHtml(e.model || "") + (e.kind
      ? " <span class=\"hint\">(" + escapeHtml(e.kind) + ")</span>"
      : "")
    + "</td><td><span class=\"badge " + statusBadge(e.status)
    + "\">" + escapeHtml(e.status || "—") + "</span></td><td>"
    + escapeHtml(fmtLatency(e.latency_ms)) + "</td><td>"
    + (e.credits == null ? "not reported"
      : escapeHtml(String(e.credits)))
    + "</td></tr>").join("")
    : "<tr><td colspan=\"6\" class=\"hint\">No API calls logged "
      + "yet — use the Generator or the Built-in AI and they'll "
      + "appear here.</td></tr>";

  const fails = d.failures || [];
  $("adApiFailures").innerHTML = fails.length ? fails.map((e) =>
    "<div class=\"admin-fail\"><b>" + escapeHtml(e.provider || "")
    + "</b> · " + escapeHtml(e.model || "")
    + "<span class=\"hint\">" + escapeHtml(fmtWhen(e.ts)) + " — "
    + escapeHtml(String(e.error || "unknown error").slice(0, 200))
    + "</span></div>").join("")
    : "<p class=\"hint\">No failed API requests logged. 🎉</p>";
}

function renderAdminChart() {
  const host = $("adChart");
  if (!host || !adminStatsCache) return;
  const series = (adminStatsCache.stats || {}).series || {};
  const pts = series[adminRange] || [];
  const notes = { daily: "Last 30 days", weekly: "Last 12 weeks",
    monthly: "Last 12 months" };
  $("adChartNote").textContent = notes[adminRange] || "";
  const W = 660, H = 216, padL = 30, padB = 24, padT = 16;
  const plotW = W - padL - 8, plotH = H - padB - padT;
  const max = Math.max(1, ...pts.map((p) =>
    Math.max(p.videos || 0, p.clips || 0)));
  let svg = "<svg viewBox=\"0 0 " + W + " " + H + "\" role=\"img\">";
  for (let i = 0; i <= 3; i++) {
    const y = padT + plotH - (plotH * i / 3);
    const v = Math.round(max * i / 3);
    svg += "<line class=\"admin-chart-grid\" x1=\"" + padL
      + "\" y1=\"" + y + "\" x2=\"" + (padL + plotW) + "\" y2=\"" + y
      + "\"/><text class=\"admin-chart-axis\" x=\"" + (padL - 5)
      + "\" y=\"" + (y + 3) + "\" text-anchor=\"end\">" + v
      + "</text>";
  }
  const groupW = pts.length ? plotW / pts.length : plotW;
  const barW = Math.max(2, Math.min(15, groupW / 2 - 2));
  pts.forEach((p, i) => {
    const cx = padL + groupW * i + groupW / 2;
    const vh = plotH * (p.videos || 0) / max;
    const ch = plotH * (p.clips || 0) / max;
    const base = padT + plotH;
    svg += "<rect class=\"admin-bar-videos\" x=\"" + (cx - barW - 1)
      + "\" y=\"" + (base - vh) + "\" width=\"" + barW
      + "\" height=\"" + vh + "\" rx=\"2\"><title>"
      + escapeHtml(p.label) + " — " + (p.videos || 0)
      + " videos</title></rect>";
    svg += "<rect class=\"admin-bar-clips\" x=\"" + (cx + 1)
      + "\" y=\"" + (base - ch) + "\" width=\"" + barW
      + "\" height=\"" + ch + "\" rx=\"2\"><title>"
      + escapeHtml(p.label) + " — " + (p.clips || 0)
      + " clips</title></rect>";
    const top = Math.max(p.videos || 0, p.clips || 0);
    if (top > 0) {
      svg += "<text class=\"admin-chart-val\" x=\"" + cx
        + "\" y=\"" + (base - Math.max(vh, ch) - 4)
        + "\" text-anchor=\"middle\">" + top + "</text>";
    }
    const every = pts.length > 14 ? 5 : (pts.length > 7 ? 2 : 1);
    if (i % every === 0 || i === pts.length - 1) {
      let lbl = String(p.label || "");
      if (adminRange === "daily" || adminRange === "weekly")
        lbl = lbl.slice(5);
      else lbl = lbl.slice(2);
      svg += "<text class=\"admin-chart-axis\" x=\"" + cx
        + "\" y=\"" + (H - 8) + "\" text-anchor=\"middle\">"
        + escapeHtml(lbl) + "</text>";
    }
  });
  svg += "</svg>";
  host.innerHTML = svg;
}

/* ---------------- modals: Esc + backdrop click ---------------- */
function closeModal(id) {
  if (id === "previewModal") {
    $("previewVideo").pause();
    $("previewVideo").removeAttribute("src");
  }
  hide($(id));
}
["captionModal", "previewModal", "settingsModal", "uninstallModal", "adminUnlockModal"].forEach((id) => {
  $(id).addEventListener("click", (e) => { if (e.target.id === id) closeModal(id); });
});
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  ["captionModal", "previewModal", "settingsModal", "uninstallModal", "adminUnlockModal"].forEach((id) => {
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
    if (opts.translate_langs && opts.translate_langs.length)
      translateLangs = opts.translate_langs.map((l) => [l.key, l.label]);
    settings = await api.get_settings();
    sel.value = settings.language || "auto";
    $("cookiesCheck").checked = !!settings.use_cookies;
  } catch (e) { /* offline-safe defaults below */ }
  applyTheme();
  updateBackBtn();
  syncSidebar("hub");
  checkAdminSession();
  refreshLocalAiStatus();
  refreshHomeRecents();
  startLocalAiPolling();
  setTimeout(() => {
    const sp = document.getElementById("splash");
    if (sp) { sp.classList.add("gone"); setTimeout(() => sp.remove(), 750); }
  }, 1550);
});
