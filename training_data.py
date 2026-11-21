"""Passive training-data collection for the Built-in AI.

Every detection run and every completed render batch is appended
to <data dir>/training/examples.jsonl, one JSON object per line:

  {"type": "detect", "ts": ..., "job_id": ..., "title": ...,
   "duration": ..., "category": ..., "source": "local"|"offline",
   "words": [{"start": .., "end": .., "word": ..}, ...],
   "moments": [{"start": .., "end": .., "title": ..,
                "virality": .., ...}, ...]}

  {"type": "render", "ts": ..., "job_id": ...,
   "picked": [moment indices rendered in this batch],
   "moments": [{"start": .., "end": .., "title": ..}, ...],
   "ratio": "9:16"}

Joined on job_id, a detect event plus its render events form one
labelled training example: the transcript is the input, the
moments the user actually rendered are the ground truth, and the
proposed moments they skipped are the negatives.
export_dataset() writes that join as chat-format JSONL ready for
a LoRA fine-tune of the brain.

Collection is ON unless Settings -> AI Training Data turns it
off. Everything stays on this PC; nothing is uploaded anywhere.
Every public function is fail-silent: a collection error must
never break a job.
"""

import json
import os
import threading
import time

_LOCK = threading.Lock()
_settings_loader = None

_MOMENT_FIELDS = ("start", "end", "title", "hook", "virality",
                  "hook_score", "flow_score", "value_score",
                  "reason")


def bind_settings(loader):
    """app.py hands over its settings loader (house pattern, same
    as publishing/studio/superadmin) so the Settings toggle is the
    single source of truth."""
    global _settings_loader
    _settings_loader = loader


def _enabled():
    try:
        if _settings_loader is not None:
            s = _settings_loader() or {}
            return bool(s.get("training_collect", True))
    except Exception:  # noqa: BLE001 - default ON if settings break
        pass
    return True


def _training_dir():
    import local_ai
    base = local_ai.custom_data_dir() or local_ai.DATA_DIR
    d = os.path.join(base, "training")
    os.makedirs(d, exist_ok=True)
    return d


def _events_path():
    return os.path.join(_training_dir(), "examples.jsonl")


def _append(event):
    try:
        line = json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    with _LOCK:
        try:
            with open(_events_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _moment_snapshot(moments, full=False):
    out = []
    for m in moments or []:
        if not isinstance(m, dict):
            continue
        if full:
            out.append({k: m.get(k) for k in _MOMENT_FIELDS
                        if k in m})
        else:
            out.append({"start": m.get("start"), "end": m.get("end"),
                        "title": m.get("title")})
    return out


def record_detect(job_id, job, source, category="general"):
    """Snapshot one detection run: transcript + proposed moments."""
    if not _enabled():
        return
    try:
        words = [{"start": w.get("start"), "end": w.get("end"),
                  "word": w.get("word")}
                 for w in (job.get("words") or [])
                 if isinstance(w, dict)]
        if not words:
            return
        _append({
            "type": "detect", "ts": time.time(), "job_id": job_id,
            "title": job.get("title") or "",
            "duration": job.get("duration"),
            "category": category or "general",
            "source": source,
            "words": words,
            "moments": _moment_snapshot(job.get("moments"), full=True),
        })
    except Exception:  # noqa: BLE001 - collection never breaks a job
        pass


def record_render(job_id, job, picked, opts=None):
    """Snapshot one render batch: which proposed moments the user
    actually turned into clips (the positive labels)."""
    if not _enabled():
        return
    try:
        picked = [int(i) for i in (picked or [])
                  if isinstance(i, int) or str(i).isdigit()]
        if not picked:
            return
        _append({
            "type": "render", "ts": time.time(), "job_id": job_id,
            "title": job.get("title") or "",
            "picked": picked,
            "moments": _moment_snapshot(job.get("moments")),
            "ratio": (opts or {}).get("ratio", "9:16"),
        })
    except Exception:  # noqa: BLE001 - collection never breaks a job
        pass


def _read_events():
    path = _events_path()
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def stats():
    """Counts for the Settings card: raw events, and how many
    complete labelled examples (detect + at least one render for
    the same job) are ready for training."""
    try:
        events = _read_events()
        detects = [e for e in events if e.get("type") == "detect"]
        renders = [e for e in events if e.get("type") == "render"]
        detected_jobs = {e.get("job_id") for e in detects}
        picked_jobs = {e.get("job_id") for e in renders}
        labelled = len(detected_jobs & picked_jobs)
        size = 0
        try:
            size = os.path.getsize(_events_path())
        except OSError:
            pass
        return {"ok": True, "events": len(events),
                "detects": len(detects), "renders": len(renders),
                "labelled_examples": labelled,
                "size_mb": round(size / 1048576.0, 2),
                "path": _events_path(),
                "enabled": _enabled()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def _transcript_text(words):
    lines = []
    for w in words or []:
        lines.append(f"[{w.get('start', 0):.1f}] {w.get('word', '')}")
    return "\n".join(lines)


def export_dataset():
    """Join detect + render events per job and write chat-format
    JSONL (dataset.jsonl in the training dir): system + user carry
    the timestamped transcript, the assistant reply is the JSON of
    the moments the user actually picked. Jobs whose picks can't be
    matched to a detect snapshot are skipped, never guessed."""
    try:
        events = _read_events()
        detects = {}
        picks = {}
        for e in events:
            jid = e.get("job_id")
            if e.get("type") == "detect":
                detects[jid] = e  # latest detect wins (re-detects)
            elif e.get("type") == "render":
                picks.setdefault(jid, set()).update(
                    e.get("picked") or [])
        rows = []
        for jid, det in detects.items():
            chosen = picks.get(jid)
            if not chosen:
                continue
            moments = det.get("moments") or []
            picked_moments = [
                {"start": moments[i].get("start"),
                 "end": moments[i].get("end"),
                 "title": moments[i].get("title")}
                for i in sorted(chosen)
                if isinstance(i, int) and 0 <= i < len(moments)]
            if not picked_moments:
                continue
            rows.append({"messages": [
                {"role": "system", "content":
                 "You are the moment-finding brain of an AI video "
                 "clipper. From a timestamped transcript, pick the "
                 "moments worth turning into short vertical clips. "
                 "Reply with JSON only."},
                {"role": "user", "content":
                 "Category: " + str(det.get("category") or "general")
                 + "\nTranscript (one word per line, [seconds]):\n"
                 + _transcript_text(det.get("words"))},
                {"role": "assistant", "content":
                 json.dumps({"moments": picked_moments},
                            ensure_ascii=False)},
            ]})
        out = os.path.join(_training_dir(), "dataset.jsonl")
        with _LOCK:
            with open(out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return {"ok": True, "examples": len(rows), "path": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
