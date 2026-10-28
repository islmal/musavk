"""VK Video backend: search, thumbnail loading and yt-dlp downloads.

yt-dlp ships no VK *search* extractor (only single videos / user feeds), so
search goes through the same anonymous web API that vkvideo.ru itself uses.
Every resulting video is a normal vkvideo.ru page URL that yt-dlp (and mpv's
ytdl hook) resolve for playback and downloading.
"""
from __future__ import annotations

import io
import queue
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
API_VERSION = "5.245"
CLIENT_ID = "52461373"          # vkvideo.ru web client
CLIENT_SECRET = "o557NLIkAErNhakXrQ7A"  # public value shipped in vkvideo.ru's web client, not a private secret
WEB_HEADERS = {"Origin": "https://vkvideo.ru", "Referer": "https://vkvideo.ru/", "User-Agent": UA}

# UI sort choices -> (VK API sort value, reverse by date locally)
SORT_OPTIONS = [
    ("Relevance", 2, False),
    ("Date: newest", 0, False),
    ("Date: oldest", 0, True),
    ("Duration: longest", 1, False),
]

# UI quality choices -> (yt-dlp download format, max height for playback / fallback)
# Pre-muxed streams come first: on VK they download ~90+ Mbit/s with chunking or
# parallel HLS fragments, while split DASH/HLS-fMP4 pairs measured 3-25 Mbit/s
# and the fMP4 audio tracks break with http_chunk_size ("Conflicting range").
QUALITY_OPTIONS = [
    ("Best (Max Quality)", "best/bestvideo+bestaudio", None),
    ("1080p", "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best", 1080),
    ("720p", "best[height<=720]/bestvideo[height<=720]+bestaudio/best", 720),
    ("480p (Small size)", "best[height<=480]/bestvideo[height<=480]+bestaudio/best", 480),
    ("360p (Data saver)", "best[height<=360]/bestvideo[height<=360]+bestaudio/best", 360),
]

CHUNK_SIZE = 10 * 1024 * 1024   # 10 MiB ranged requests dodge VK's per-request throttle


def playback_format(max_height: int | None) -> str:
    """Prefer a single combined stream (fast start, one connection); fall back
    to a separate video+audio pair, then to anything."""
    if max_height:
        h = f"[height<={max_height}]"
        return f"best{h}/bestvideo{h}+bestaudio/best"
    return "best/bestvideo+bestaudio"


@dataclass
class VideoItem:
    vid: str                 # "<owner_id>_<id>"
    title: str
    url: str
    channel: str
    channel_handle: str
    duration: int
    views: int
    date: int
    thumb_url: str
    playable: bool = True
    restriction: str = ""

    @property
    def duration_text(self) -> str:
        s = int(self.duration or 0)
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @property
    def views_text(self) -> str:
        v = int(self.views or 0)
        for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if v >= div:
                return f"{v / div:.1f}".rstrip("0").rstrip(".") + suf + " views"
        return f"{v} views"


class VKApiError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class VKClient:
    """Thread-safe minimal client for VK's anonymous video search API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(WEB_HEADERS)
        self._token = ""
        self._token_exp = 0.0
        self._lock = threading.Lock()

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_exp - 60:
                return self._token
            r = self.session.post(
                "https://login.vk.com/?act=get_anonym_token",
                data={
                    "client_secret": CLIENT_SECRET,
                    "client_id": CLIENT_ID,
                    "scopes": "audio_anonymous,video_anonymous,photos_anonymous,profile_anonymous",
                    "isApiOauthAnonymEnabled": "false",
                    "version": "1",
                    "app_id": "6287487",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("data") or {}
            if not data.get("access_token"):
                raise VKApiError("VK did not issue an anonymous token")
            self._token = data["access_token"]
            self._token_exp = float(data.get("expires") or time.time() + 3600)
            return self._token

    TRANSIENT = {6, 9, 10}   # too many requests/s, flood control, internal server error

    def call(self, method: str, **params) -> dict:
        refreshed = False
        for attempt in range(5):
            params["access_token"] = self._get_token(force=refreshed and attempt == 1)
            r = self.session.post(
                f"https://api.vk.com/method/{method}",
                params={"v": API_VERSION, "client_id": CLIENT_ID},
                data=params,
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            if "error" not in j:
                return j["response"]
            err = j["error"]
            code = err.get("error_code")
            if code == 5 and not refreshed:          # token expired -> refresh once
                refreshed = True
                self._token_exp = 0
                continue
            if code in self.TRANSIENT and attempt < 4:
                time.sleep(0.4 * (2 ** attempt))      # 0.4, 0.8, 1.6, 3.2 s
                continue
            raise VKApiError(f"VK API error {code}: {err.get('error_msg')}", code)
        raise VKApiError("VK API call failed")

    # ---- search -----------------------------------------------------------
    def search_page(self, query: str, sort: int, cursor: tuple[str, str] | None):
        """Returns (items, next_cursor). cursor = (section_id, next_from)."""
        if cursor is None:
            resp = self.call("catalog.getVideoSearchWeb2", q=query, sort=sort)
            sections = resp.get("catalog", {}).get("sections") or [{}]
            section = sections[0]
        else:
            resp = self.call("catalog.getSection", section_id=cursor[0], start_from=cursor[1])
            section = resp.get("section") or {}
        groups = {g["id"]: g for g in resp.get("groups", [])}
        profiles = {p["id"]: p for p in resp.get("profiles", [])}

        # Keep only the "large_list" video blocks (main results), in order.
        order: list[str] = []
        for block in section.get("blocks", []):
            if block.get("data_type") == "catalog_videos":
                order.extend(block.get("videos_ids", []))
        by_id = {}
        for cv in resp.get("catalog_videos", []):
            v = cv.get("video") or {}
            if "id" in v:
                by_id[f"{v['owner_id']}_{v['id']}"] = v
        if not order:
            order = list(by_id)

        items = []
        for key in order:
            v = by_id.get(key)
            if v:
                items.append(self._to_item(v, groups, profiles))
        nf = section.get("next_from")
        next_cursor = (section["id"], nf) if nf and section.get("id") else None
        return items, next_cursor

    @staticmethod
    def _to_item(v: dict, groups: dict, profiles: dict) -> VideoItem:
        owner = int(v.get("owner_id", 0))
        channel, handle = "", ""
        if owner < 0 and -owner in groups:
            channel = groups[-owner].get("name", "")
            handle = groups[-owner].get("screen_name", "")
        elif owner > 0 and owner in profiles:
            p = profiles[owner]
            channel = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            handle = p.get("screen_name", "")
        if not channel:
            btn = (v.get("action_button") or {}).get("snippet") or {}
            channel = btn.get("title") or f"id{owner}"

        thumb = ""
        imgs = [i for i in v.get("image", []) if i.get("width", 0) >= 320 and not i.get("with_padding")]
        if imgs:
            thumb = min(imgs, key=lambda i: abs(i["width"] - 480))["url"]
        elif v.get("image"):
            thumb = v["image"][-1]["url"]

        restriction = v.get("restriction") or {}
        vid = f"{owner}_{v['id']}"
        return VideoItem(
            vid=vid,
            title=v.get("title", "") or "(untitled)",
            url=v.get("direct_url") or f"https://vkvideo.ru/video{vid}",
            channel=channel,
            channel_handle=handle,
            duration=int(v.get("duration") or 0),
            views=int(v.get("views") or 0),
            date=int(v.get("date") or 0),
            thumb_url=thumb,
            playable=bool(restriction.get("can_play", 1)) if restriction else True,
            restriction=restriction.get("title", ""),
        )


_client: VKClient | None = None


def client() -> VKClient:
    global _client
    if _client is None:
        _client = VKClient()
    return _client


# ---- channels ----------------------------------------------------------------
# The anonymous web token can read a community's upload feed (video.get) but
# has no in-channel search: catalog.getVideoSearchWeb2 silently ignores
# owner_id, and video.get ignores q. Keyword search inside a channel therefore
# scans the channel's own feed (100 videos per request, several in parallel)
# and matches titles/descriptions -- every result really is from that channel.

@dataclass
class ChannelInfo:
    owner_id: int            # < 0 community, > 0 user
    name: str
    handle: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} (@{self.handle})" if self.handle else self.name


_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:vkvideo\.ru|vk\.com|vk\.ru)/", re.I)
_channel_cache: dict[str, ChannelInfo] = {}
_cache_lock = threading.Lock()


def _cache_key(text: str) -> str:
    return text.strip().casefold()


def remember_channel(text: str, info: ChannelInfo) -> None:
    """Seed the resolver (e.g. from a clicked card, whose owner_id is known)."""
    with _cache_lock:
        _channel_cache[_cache_key(text)] = info
        if info.handle:
            _channel_cache[_cache_key("@" + info.handle)] = info


def parse_channel_ref(text: str) -> tuple[str, str]:
    """-> ("group"|"user", "123") for numeric refs, ("screen", name) for handles,
    ("name", text) for anything that can only be a display name."""
    t = _URL_PREFIX.sub("", text.strip())
    t = re.sub(r"^video/", "", t, flags=re.I).lstrip("@")
    t = re.split(r"[/?#]", t, 1)[0]
    if m := re.fullmatch(r"(?:videos|club|public|event)?-?(\d+)", t, re.I):
        if not t.isdigit() or t.startswith("-"):
            return "group", m.group(1)
    if m := re.fullmatch(r"id(\d+)", t, re.I):
        return "user", m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_.]{2,}", t):
        return "screen", t
    return "name", text.strip()


def _norm(s: str) -> str:
    return s.casefold().replace("ё", "е")


def resolve_channel(text: str) -> ChannelInfo | None:
    key = _cache_key(text)
    with _cache_lock:
        if key in _channel_cache:
            return _channel_cache[key]
    c = client()
    kind, ref = parse_channel_ref(text)
    info = None
    if kind == "screen":
        r = c.call("utils.resolveScreenName", screen_name=ref)
        if isinstance(r, dict) and r.get("object_id"):
            kind = "user" if r.get("type") == "user" else "group"
            ref = str(r["object_id"])
    if kind == "group":
        r = c.call("groups.getById", group_ids=ref)
        g = (r.get("groups") or [None])[0] if isinstance(r, dict) else None
        if g:
            info = ChannelInfo(-int(g["id"]), g.get("name", ""), g.get("screen_name", ""))
    elif kind == "user":
        r = c.call("users.get", user_ids=ref, fields="screen_name")
        if r:
            u = r[0]
            info = ChannelInfo(int(u["id"]), f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                               u.get("screen_name", ""))
    if info is None:
        info = _find_channel_by_name(text)
    if info is not None:
        remember_channel(text, info)
    return info


def _find_channel_by_name(name: str) -> ChannelInfo | None:
    """Display-name lookup: global video search returns the communities behind
    its results; pick an exact name match, else the biggest partial match."""
    needle = _norm(name.strip().lstrip("@"))
    resp = client().call("catalog.getVideoSearchWeb2", q=name.strip().lstrip("@"))
    groups = resp.get("groups", [])
    exact = [g for g in groups if _norm(g.get("name", "")) == needle
             or _norm(g.get("screen_name", "")) == needle]
    partial = [g for g in groups if needle in _norm(g.get("name", ""))]
    pool = exact or partial
    if not pool:
        return None
    g = max(pool, key=lambda g: g.get("members_count") or 0)
    return ChannelInfo(-int(g["id"]), g.get("name", ""), g.get("screen_name", ""))


@dataclass
class ChannelCursor:
    owner_id: int
    position: int            # feed offset (in scan order) of the next page; always a page boundary
    total: int
    oldest_first: bool
    # Matches already fetched but not yet shown. VK may return fewer than
    # `count` videos per page (hidden ones are skipped), so resuming mid-page
    # by counting items would land on the wrong offset -> keep the surplus.
    pending: list = field(default_factory=list)


FEED_PAGE = 100              # video.get maximum


def _feed_page(owner_id: int, start: int, count: int) -> tuple[list[dict], dict, dict, int]:
    try:
        r = client().call("video.get", owner_id=owner_id, offset=start, count=count, extended=1)
    except VKApiError as e:
        if e.code == 15:
            raise VKApiError("VK only shows the videos of personal pages to logged-in users; "
                             "channel filtering works for communities/channels.", 15) from e
        raise
    groups = {g["id"]: g for g in r.get("groups", [])}
    profiles = {p["id"]: p for p in r.get("profiles", [])}
    return r.get("items", []), groups, profiles, int(r.get("count") or 0)


def _matches(v: dict, tokens: list[str]) -> bool:
    hay = _norm(f"{v.get('title', '')}\n{v.get('description', '')}")
    return all(t in hay for t in tokens)


def channel_scan(cursor: ChannelCursor, query: str, want: int, max_scan: int,
                 should_stop=lambda: False) -> tuple[list[VideoItem], ChannelCursor | None, int]:
    """Walk the channel feed from cursor.position (newest-first, or oldest-first
    via offsets counted from the end). Without a query every video is a
    result; with one, pages are fetched 8 at a time and filtered. Returns
    (items, next cursor or None at the end, videos scanned)."""
    tokens = [_norm(t) for t in query.split()]
    pos, total = cursor.position, cursor.total
    out: list[VideoItem] = list(cursor.pending)
    scanned = 0
    batch = 4 if tokens else 1
    page = FEED_PAGE if tokens else min(FEED_PAGE, max(want, 24))

    def window(p):  # scan position -> (api offset, count)
        if not cursor.oldest_first:
            return p, min(page, total - p)
        end = total - p
        start = max(0, end - page)
        return start, end - start

    with ThreadPoolExecutor(batch) as pool:
        while pos < total and len(out) < want and scanned < max_scan and not should_stop():
            wins = []
            p = pos
            for _ in range(batch):
                if p >= total:
                    break
                wins.append(window(p))
                p += wins[-1][1]
            pages = list(pool.map(lambda w: _feed_page(cursor.owner_id, *w), wins))
            for (_start, count), (vids, groups, profiles, _total) in zip(wins, pages):
                if cursor.oldest_first:
                    vids = list(reversed(vids))
                out.extend(VKClient._to_item(v, groups, profiles)
                           for v in vids if not tokens or _matches(v, tokens))
                pos += count
                scanned += count
            if not tokens:
                break                  # feed mode: one page per request
    shown, surplus = out[:want], out[want:]
    more = pos < total or surplus
    nxt = ChannelCursor(cursor.owner_id, pos, total, cursor.oldest_first, surplus) if more else None
    return shown, nxt, scanned


class SearchWorker(QThread):
    """Global VK search, or -- when a channel is given -- that channel's feed
    (no keyword) / a keyword search over that channel's uploads."""

    results = pyqtSignal(int, list, object)   # generation, items, next cursor
    channelInfo = pyqtSignal(int, object)     # generation, dict(channel, scanned, total)
    failed = pyqtSignal(int, str)

    def __init__(self, generation: int, query: str, sort_index: int, channel: str = "",
                 cursor=None, min_results: int = 24, parent=None):
        super().__init__(parent)
        self.generation = generation
        self.query = query.strip()
        self.sort_index = sort_index
        self.channel = channel.strip()
        self.cursor = cursor
        self.min_results = min_results

    def run(self):
        try:
            if self.channel:
                self._run_channel()
            else:
                self._run_global()
        except Exception as e:  # network / API errors are reported to the UI
            self.failed.emit(self.generation, str(e))

    def _run_global(self):
        _, api_sort, reverse = SORT_OPTIONS[self.sort_index]
        items, cursor = client().search_page(self.query, api_sort, self.cursor)
        if not items and cursor is None and self.cursor is None:
            # VK occasionally answers a fresh search with an empty catalog
            # (seen once for "cats"); retry once with a new anonymous token.
            time.sleep(0.5)
            client()._token_exp = 0
            items, cursor = client().search_page(self.query, api_sort, None)
        if reverse:
            items.sort(key=lambda i: i.date)
        self.results.emit(self.generation, items, cursor)

    def _run_channel(self):
        label, _, _ = SORT_OPTIONS[self.sort_index]
        oldest = label == "Date: oldest"
        cursor = self.cursor
        info = resolve_channel(self.channel)
        if info is None:
            raise VKApiError(f"Couldn't find a VK channel “{self.channel}”. "
                             "Try its @handle or the channel URL.")
        if cursor is None:
            # Learn the feed size first (needed to page from the oldest end).
            _, _, _, total = _feed_page(info.owner_id, 0, 1)
            cursor = ChannelCursor(info.owner_id, 0, total, oldest)
        # Keyword: scan up to ~2000 uploads per request (~2-3 s), then let
        # "Load more" continue from where the scan stopped.
        items, nxt, scanned = channel_scan(cursor, self.query, self.min_results, 2000,
                                           self.isInterruptionRequested)
        if label == "Duration: longest":
            items.sort(key=lambda i: i.duration, reverse=True)
        self.channelInfo.emit(self.generation, {
            "channel": info, "scanned_to": nxt.position if nxt else cursor.total,
            "total": cursor.total, "query": self.query})
        self.results.emit(self.generation, items, nxt)


class ThumbnailLoader(QThread):
    """Long-lived worker: downloads + scales thumbnails with a small pool."""

    loaded = pyqtSignal(int, str, QImage)   # generation, key, image

    def __init__(self, size=(320, 180), workers: int = 8, parent=None):
        super().__init__(parent)
        self.size = size
        self.workers = workers
        self.generation = 0
        self._q: queue.Queue = queue.Queue()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = UA
        adapter = requests.adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers)
        self._session.mount("https://", adapter)

    def request(self, generation: int, key: str, url: str):
        if url:
            self._q.put((generation, key, url))

    def reset(self) -> int:
        self.generation += 1
        return self.generation

    def stop(self):
        self.requestInterruption()
        self._q.put(None)
        self.wait(3000)

    def _fetch(self, gen: int, key: str, url: str):
        if gen != self.generation:
            return
        try:
            r = self._session.get(url, timeout=15)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            # Cover-crop to the card's aspect ratio.
            tw, th = self.size
            scale = max(tw / img.width, th / img.height)
            img = img.resize((max(tw, round(img.width * scale)), max(th, round(img.height * scale))),
                             Image.Resampling.LANCZOS)
            left, top = (img.width - tw) // 2, (img.height - th) // 2
            img = img.crop((left, top, left + tw, top + th))
            data = img.tobytes("raw", "RGB")
            qimg = QImage(data, tw, th, tw * 3, QImage.Format.Format_RGB888).copy()
            if gen == self.generation:
                self.loaded.emit(gen, key, qimg)
        except Exception:
            pass

    def run(self):
        with ThreadPoolExecutor(self.workers) as pool:
            while not self.isInterruptionRequested():
                job = self._q.get()
                if job is None:
                    break
                pool.submit(self._fetch, *job)


# ---- stream resolving --------------------------------------------------------
@dataclass
class ResolvedStream:
    url: str
    audio_url: str = ""
    headers: dict = field(default_factory=dict)
    title: str = ""
    format_desc: str = ""


def _patch_yt_dlp_vk() -> None:
    # shortcut: works around yt-dlp VK extractor crashing on subtitles with "title": null
    # (vk.py sub.get('title', '.srt')); drop once yt-dlp fixes it.
    from yt_dlp.extractor.vk import VKIE
    if getattr(VKIE, "_musavk_patched", False):
        return
    orig = VKIE._parse_json

    def fix(o):
        if isinstance(o, dict):
            for s in o.get("subs") or []:
                if isinstance(s, dict) and s.get("title") is None:
                    s.pop("title", None)
            for v in o.values():
                fix(v)
        elif isinstance(o, list):
            for v in o:
                fix(v)

    def _parse_json(self, *args, **kwargs):
        res = orig(self, *args, **kwargs)
        fix(res)
        return res

    VKIE._parse_json = _parse_json
    VKIE._musavk_patched = True


class StreamResolver(QThread):
    """Turns a vkvideo.ru page URL into direct media URL(s) + HTTP headers."""

    resolved = pyqtSignal(int, object)   # token, ResolvedStream
    failed = pyqtSignal(int, str)

    def __init__(self, token: int, url: str, fmt: str, parent=None):
        super().__init__(parent)
        self.token = token
        self.url = url
        self.fmt = fmt

    def run(self):
        import yt_dlp
        _patch_yt_dlp_vk()

        try:
            opts = {"format": self.fmt, "quiet": True, "no_warnings": True, "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            req = info.get("requested_formats")
            if req:
                video = next((f for f in req if f.get("vcodec") != "none"), req[0])
                audio = next((f for f in req if f is not video), None)
            else:
                video, audio = info, None
            if not video.get("url"):
                raise RuntimeError("yt-dlp returned no stream URL")
            headers = dict(video.get("http_headers") or info.get("http_headers") or {})
            desc = info.get("format_id") or video.get("format_id", "")
            if video.get("height"):
                desc = f"{video['height']}p · {desc}"
            self.resolved.emit(self.token, ResolvedStream(
                url=video["url"], audio_url=(audio or {}).get("url", ""),
                headers=headers, title=info.get("title", ""), format_desc=desc))
        except Exception as e:
            self.failed.emit(self.token, str(e).replace("ERROR: ", ""))


# ---- downloads ---------------------------------------------------------------
class DownloadCancelled(Exception):
    pass


class DownloadWorker(QThread):
    progress = pyqtSignal(str, float, str, str)   # job id, percent, speed, eta
    status = pyqtSignal(str, str)                 # job id, message
    finished_ok = pyqtSignal(str, str)            # job id, file path
    failed = pyqtSignal(str, str)                 # job id, error

    def __init__(self, job_id: str, url: str, title: str, out_dir: str,
                 quality_index: int = 0, parent=None):
        super().__init__(parent)
        self.job_id = job_id
        self.url = url
        self.title = title
        self.out_dir = out_dir
        self.quality_index = quality_index
        self._cancel = threading.Event()
        self._last_emit = 0.0
        self._filename = ""

    def cancel(self):
        self._cancel.set()

    @staticmethod
    def aria2c_available() -> bool:
        return shutil.which("aria2c") is not None

    def build_options(self) -> dict:
        has_ffmpeg = shutil.which("ffmpeg") is not None
        _, fmt, max_h = QUALITY_OPTIONS[self.quality_index]
        if not has_ffmpeg:
            # Separate video+audio can't be merged without ffmpeg: single files only.
            fmt = f"best[height<={max_h}]/best" if max_h else "best"
        opts = {
            "outtmpl": {"default": f"{self.out_dir}/%(title)s [%(id)s].%(ext)s"},
            "windowsfilenames": True,
            "concurrent_fragment_downloads": 16,
            "http_chunk_size": CHUNK_SIZE,
            "buffersize": 1024 * 1024 * 16,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "format_sort": ["hasvid", "hasaud", "res"],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            # Resolve only what the chosen format needs: no format probing,
            # subtitles or comments.
            "skip_download": False,
            "check_formats": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "getcomments": False,
            "extract_flat": False,
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
            "format": fmt,
            "merge_output_format": "mp4",
        }
        if self.aria2c_available():
            # Only for fragmented DASH, where yt-dlp drives aria2c over RPC and
            # gets live progress. For single files yt-dlp reports no progress
            # from aria2c until it finishes (UI stuck on "Starting…"), and the
            # native chunked downloader measured just as fast (~94 Mbit/s).
            opts["external_downloader"] = {"dash": "aria2c", "default": "native"}
            opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M", "-j", "16"]}
        return opts

    def _hook(self, d: dict):
        if self._cancel.is_set():
            raise DownloadCancelled()
        st = d.get("status")
        if st == "downloading":
            now = time.monotonic()
            if now - self._last_emit < 0.2:
                return
            self._last_emit = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = done * 100.0 / total
            elif d.get("fragment_count"):
                pct = (d.get("fragment_index") or 0) * 100.0 / d["fragment_count"]
            else:
                pct = -1.0
            speed = d.get("speed")
            speed_s = f"{_fmt_bytes(speed)}/s ({speed * 8 / 1e6:.0f} Mbit/s)" if speed else ""
            eta = d.get("eta")
            eta_s = time.strftime("%H:%M:%S" if eta and eta >= 3600 else "%M:%S", time.gmtime(eta)) if eta else ""
            self.progress.emit(self.job_id, pct, speed_s, eta_s)
        elif st == "finished":
            self._filename = d.get("filename") or self._filename
            self.progress.emit(self.job_id, 100.0, "", "")

    def _pp_hook(self, d: dict):
        if d.get("status") == "started" and d.get("postprocessor") in ("Merger", "FFmpegMerger"):
            self.status.emit(self.job_id, "Merging…")
        info = d.get("info_dict") or {}
        if info.get("filepath"):
            self._filename = info["filepath"]

    def run(self):
        import yt_dlp
        _patch_yt_dlp_vk()

        try:
            self.status.emit(self.job_id, "Resolving formats…")
            with yt_dlp.YoutubeDL(self.build_options()) as ydl:
                info = ydl.extract_info(self.url, download=False)
                self.status.emit(self.job_id, f"Connecting… ({info.get('format_id', '')})")
                if self._cancel.is_set():
                    raise DownloadCancelled()
                # Chunking a byte-range fragment (HLS fMP4 / DASH pieces) makes
                # yt-dlp request start > end -> "Conflicting range". Only chunk
                # plain progressive files.
                fmts = info.get("requested_formats") or [info]
                if any(f.get("protocol") != "https" or f.get("fragments") for f in fmts):
                    ydl.params["http_chunk_size"] = None
                info = ydl.process_ie_result(info, download=True)
                path = (info or {}).get("filepath") or ""
                if not path:
                    reqs = (info or {}).get("requested_downloads") or []
                    path = reqs[0].get("filepath", "") if reqs else self._filename
            self.finished_ok.emit(self.job_id, path or self._filename)
        except DownloadCancelled:
            self.failed.emit(self.job_id, "Cancelled")
        except Exception as e:
            msg = str(e)
            if isinstance(e.__cause__, DownloadCancelled) or "DownloadCancelled" in msg or self._cancel.is_set():
                msg = "Cancelled"
            self.failed.emit(self.job_id, msg.replace("ERROR: ", ""))


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
