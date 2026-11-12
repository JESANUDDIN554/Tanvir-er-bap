# -*- coding: utf-8 -*-
"""YouTube Channel Monitor (Phase 2) - Data API v3 client.

Watches channels the user follows and reports NEW uploads since the
last check. Deliberately thin and honest:

- Official Data API only (channels.list / playlistItems.list /
  videos.list). NO scraping, no innertube, no cookies.
- The user's OWN YouTube Data API key is required; without one the
  monitor is inert and the UI shows the setup state.
- Every HTTP call goes through an injectable `opener` so the whole
  client is provable against recorded API fixtures (the exact JSON
  shapes the real API returns) with zero network.

Watermark model: per channel we persist `seen_ids` (the newest ~60
video ids from the last poll) plus `last_published` (ISO timestamp of
the newest item seen). A poll reports the items whose ids are not in
seen_ids; the FIRST poll only establishes the watermark (baseline)
and reports nothing as new - otherwise adding a channel would flood
the list with its whole back catalogue.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://www.googleapis.com/youtube/v3"


class ChannelError(Exception):
    """An honest monitor failure. `kind` is one of:
    no_key | bad_key | quota | network | api | not_found | bad_ref."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _default_opener(url):
    """GET `url` -> (status, parsed_json_or_None). Never raises for
    HTTP errors; transport failures raise ChannelError(network)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "ItrajiCliper/2.0 (channel monitor)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError) as e:
        raise ChannelError("network",
                           "Couldn't reach YouTube: " + str(e)[:140])


def _get(path, params, key, opener):
    if not (key or "").strip():
        raise ChannelError("no_key", "No YouTube Data API key saved.")
    q = dict(params or {})
    q["key"] = key.strip()
    url = API_BASE + path + "?" + urllib.parse.urlencode(q)
    status, data = (opener or _default_opener)(url)
    if status == 200 and isinstance(data, dict):
        return data
    # Map the API's structured error onto an honest kind.
    reason = ""
    if isinstance(data, dict):
        err = data.get("error") or {}
        reason = str(((err.get("errors") or [{}])[0].get("reason")
                      or err.get("status") or "")).lower()
        msg = str(err.get("message") or "")[:180]
    else:
        msg = ""
    if status in (400, 401, 403) and (
            "key" in reason or "api_key" in reason
            or reason in ("keyinvalid", "badrequest")
            or "api key" in msg.lower()):
        raise ChannelError(
            "bad_key",
            "YouTube rejected the API key. Check the key in "
            "Settings and that the YouTube Data API v3 is enabled "
            "for it." + ((" " + msg) if msg else ""))
    if status == 403 and ("quota" in reason or "limit" in reason
                          or "quota" in msg.lower()):
        raise ChannelError(
            "quota", "The YouTube Data API daily quota for this key "
                     "is used up. It resets at midnight Pacific time.")
    if status == 404:
        raise ChannelError("not_found",
                           "YouTube couldn't find that channel.")
    raise ChannelError(
        "api", f"YouTube API error (HTTP {status})"
               + ((": " + msg) if msg else ""))


_UC_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")


def parse_channel_ref(ref):
    """Classify a user-typed channel reference:
    -> ("id", UC...) | ("handle", "@name") | ("username", name).
    Raises ChannelError(bad_ref) for anything unrecognisable."""
    ref = (ref or "").strip()
    if not ref:
        raise ChannelError("bad_ref", "Paste a channel URL or handle.")
    m = _UC_RE.search(ref)
    if m:
        return ("id", m.group(0))
    m = re.search(r"/@([A-Za-z0-9._-]+)", ref)
    if m:
        return ("handle", "@" + m.group(1))
    if ref.startswith("@"):
        return ("handle", ref.split()[0])
    m = re.search(r"/(?:user|c)/([A-Za-z0-9._-]+)", ref)
    if m:
        return ("username", m.group(1))
    if re.fullmatch(r"[A-Za-z0-9._-]+", ref):
        return ("handle", "@" + ref)
    raise ChannelError(
        "bad_ref",
        "That doesn't look like a channel URL, @handle or channel ID.")


def resolve_channel(ref, key, opener=None):
    """Resolve a URL/handle/id to {channel_id, title, uploads} using
    channels.list (forHandle / forUsername / id)."""
    kind, value = parse_channel_ref(ref)
    params = {"part": "snippet,contentDetails"}
    if kind == "id":
        params["id"] = value
    elif kind == "handle":
        params["forHandle"] = value
    else:
        params["forUsername"] = value
    data = _get("/channels", params, key, opener)
    items = data.get("items") or []
    if not items:
        raise ChannelError("not_found",
                           "YouTube couldn't find that channel.")
    it = items[0]
    uploads = (((it.get("contentDetails") or {}).get("relatedPlaylists")
                or {}).get("uploads") or "")
    return {"channel_id": it.get("id") or value,
            "title": (it.get("snippet") or {}).get("title") or value,
            "uploads": uploads}


def parse_iso8601_duration(text):
    """PT#H#M#S (videos.list contentDetails.duration) -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                     (text or "").strip())
    if not m:
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fetch_uploads(channel, key, opener=None, max_results=25):
    """The newest uploads of a channel (playlistItems on its uploads
    playlist + videos.list for durations), newest first:
    [{video_id, title, published_at, duration_s, thumbnail, url}]."""
    uploads = (channel or {}).get("uploads") or ""
    if not uploads:
        raise ChannelError("api", "That channel has no uploads "
                                  "playlist to watch.")
    data = _get("/playlistItems",
                {"part": "snippet", "playlistId": uploads,
                 "maxResults": max(1, min(50, int(max_results)))},
                key, opener)
    items = []
    for it in data.get("items") or []:
        sn = it.get("snippet") or {}
        vid = ((sn.get("resourceId") or {}).get("videoId") or "")
        if not vid:
            continue
        thumbs = sn.get("thumbnails") or {}
        thumb = ((thumbs.get("medium") or thumbs.get("default") or {})
                 .get("url") or "")
        items.append({"video_id": vid,
                      "title": sn.get("title") or "(untitled)",
                      "published_at": sn.get("publishedAt") or "",
                      "duration_s": 0,
                      "thumbnail": thumb,
                      "url": "https://www.youtube.com/watch?v=" + vid})
    if items:
        vdata = _get("/videos",
                     {"part": "contentDetails",
                      "id": ",".join(i["video_id"] for i in items)},
                     key, opener)
        durs = {v.get("id"): parse_iso8601_duration(
                    (v.get("contentDetails") or {}).get("duration"))
                for v in (vdata.get("items") or [])}
        for i in items:
            i["duration_s"] = durs.get(i["video_id"], 0)
    return items


def poll_channel(channel, key, opener=None, max_results=25):
    """One monitor check. Returns {"new": [...], "latest": [...],
    "baseline": bool, "watermark": {...}} - `watermark` REPLACES the
    channel's stored one. Duration filters (min_dur/max_dur seconds,
    0 = off) apply to what counts as "new" (a filtered-out video is
    still marked seen, so it never resurfaces)."""
    items = fetch_uploads(channel, key, opener, max_results)
    seen = set(channel.get("seen_ids") or [])
    baseline = not seen and not channel.get("last_published")
    min_dur = int(channel.get("min_dur") or 0)
    max_dur = int(channel.get("max_dur") or 0)
    new = []
    for it in items:
        if it["video_id"] in seen:
            continue
        if baseline:
            continue
        if min_dur and it["duration_s"] and it["duration_s"] < min_dur:
            continue
        if max_dur and it["duration_s"] and it["duration_s"] > max_dur:
            continue
        new.append(it)
    watermark = {
        "seen_ids": [i["video_id"] for i in items][:60],
        "last_published": (items[0]["published_at"] if items else
                           channel.get("last_published") or ""),
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    return {"new": new, "latest": items, "baseline": baseline,
            "watermark": watermark}
