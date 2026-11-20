# -*- coding: utf-8 -*-
"""
Fetch YouTube videos with yt-dlp.

Provides the subset of the pytubefix YouTube/Stream/Caption interface that the scheduler
and media controller use (title, description, thumbnail_url, captions, stream.download).

Runtime requirements:
  - yt-dlp (keep it current - YouTube changes often) and yt-dlp-ejs
  - node >= 22 or deno to solve YouTube's JS challenges. nodejs-wheel-binaries (a pytubefix
    dependency) bundles a suitable node and is used if present.
  - bgutil-ytdlp-pot-provider plugin plus its HTTP server (YT_POT_PROVIDER_URL) for PO tokens.
    Without it, stream downloads get HTTP 403 from YouTube.
  - ffmpeg (passed in by the scheduler via find_ffmpeg, else on the PATH) when video and audio
    are separate streams.
"""
import copy
import os
import time
import urllib.request
import xml.etree.ElementTree as ElementTree
from html import unescape

from yt_dlp import YoutubeDL
from yt_dlp.networking import HEADRequest

POT_PROVIDER_URL = os.environ.get("YT_POT_PROVIDER_URL", "http://127.0.0.1:4416")

# Single file 360p mp4 first (no merge needed), then 480p h264 + aac merged by ffmpeg,
# then any single mp4 at 480p or below. A higher mp4 is only used when nothing <=480 exists.
# Stick to h264/aac so the result plays in every browser.
VIDEO_FORMAT = (
    "18/bv*[ext=mp4][vcodec^=avc1][height<=480]+ba[ext=m4a]"
    "/b[ext=mp4][height<=480]/b[ext=mp4]"
)

RATE_LIMIT_MARKERS = ("http error 429", "too many requests", "try again later")
BOT_MARKERS = ("not a bot",)
REGION_MARKERS = ("in your country",)
PROXY_MARKERS = (
    "tunnel connection failed", "unable to connect to proxy", "cannot connect to proxy",
    "proxyerror", "connection refused", "timed out", "remote end closed connection",
)
# Some IPs pass the bot check but get 403 from the video servers
FORBIDDEN_MARKERS = ("http error 403",)
PERMANENT_MARKERS = (
    "private video", "video unavailable", "this video is unavailable", "has been removed",
    "members-only", "join this channel", "confirm your age", "age-restricted",
    "inappropriate for some users",
    "is not a valid url", "unsupported url", "incomplete youtube id", "no suitable extractor",
    "account associated with this video has been terminated", "live stream",
)
# Not aired yet. The video itself will become downloadable, so this is retried later.
UPCOMING_MARKERS = ("this live event will begin", "premieres in")


class YTVideoError(Exception):
    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


def classify_error(ex):
    """Returns one of: rate_limited, bot, region, proxy, forbidden, permanent, unavailable_now, unknown.

    rate_limited/bot/proxy/forbidden are problems with the proxy (try another one).
    region and permanent are about the video: stop, and do not mark the proxy.
    unavailable_now (live, premiere) will not work on any proxy today, but should be retried later.
    unknown means stop this run and try again later, without burning the rest of the proxy list.
    """
    if isinstance(ex, YTVideoError):
        return ex.kind
    msg = str(ex).lower()
    for kind, markers in (
        ("rate_limited", RATE_LIMIT_MARKERS),
        ("bot", BOT_MARKERS),
        ("region", REGION_MARKERS),
        ("proxy", PROXY_MARKERS),
        ("forbidden", FORBIDDEN_MARKERS),
        ("unavailable_now", UPCOMING_MARKERS),
        ("permanent", PERMANENT_MARKERS),
    ):
        if any(m in msg for m in markers):
            return kind
    return "unknown"


_pot_checked = {"at": 0.0, "ok": False}


def pot_provider_reachable(timeout=3):
    """True when the bgutil PO-token server answers GET /ping. Cached for 60s.

    A 403 while this is down is the missing token, not a bad proxy. Callers must not
    mark the proxy FAILED in that case or the whole pool gets backed off.
    """
    now = time.time()
    if now - _pot_checked["at"] < 60:
        return _pot_checked["ok"]
    ok = False
    try:
        req = urllib.request.Request(POT_PROVIDER_URL.rstrip("/") + "/ping")
        # Empty ProxyHandler so a process-global opener or HTTP_PROXY cannot hide a local server.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        ok = False
    _pot_checked["at"] = now
    _pot_checked["ok"] = ok
    return ok


def _js_runtimes():
    runtimes = {"deno": {}}
    try:
        import nodejs_wheel.executable
        root = nodejs_wheel.executable.ROOT_DIR
        # nodejs-wheel puts node.exe in ROOT_DIR on Windows and bin/node everywhere else.
        name = "node.exe" if os.name == "nt" else "node"
        bin_dir = root if os.name == "nt" else os.path.join(root, "bin")
        node_path = os.path.join(bin_dir, name)
        runtimes["node"] = {"path": node_path} if os.path.isfile(node_path) else {}
    except ImportError:
        runtimes["node"] = {}
    return runtimes


def _base_params(proxy):
    return {
        # "" forces a direct connection instead of picking up HTTP(S)_PROXY from the environment
        "proxy": proxy or "",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        # The video extractor only. Playlists, channels, and non-YouTube URLs are rejected
        # instead of being fully extracted (noplaylist does not cover a playlist or channel URL).
        "allowed_extractors": ["^youtube$"],
        "format": VIDEO_FORMAT,
        "js_runtimes": _js_runtimes(),
        # Only used if the yt-dlp-ejs package is missing
        "remote_components": ["ejs:github"],
        "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}},
        # Fail fast - the caller moves on to the next proxy. Downloads set their own timeout/retries.
        "socket_timeout": 15,
        "extractor_retries": 1,
    }


def _srt_time(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _require_single_video(info):
    kind = info.get("_type") or "video"
    if kind != "video":
        raise YTVideoError(
            f"Only a single YouTube video can be pulled (got {kind})", "permanent")


def srv1_to_srt(xml_captions):
    """Convert YouTube's srv1 caption xml (<transcript><text start= dur=>) to SRT."""
    root = ElementTree.fromstring(xml_captions)
    segments = []
    for el in root.iter("text"):
        # srv1 text is html-escaped inside the xml escaping (e.g. &amp;#39;)
        text = unescape("".join(el.itertext())).replace("\n", " ").strip()
        if not text:
            continue
        start = float(el.get("start", 0))
        end = start + float(el.get("dur", 0))
        segments.append(f"{len(segments) + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(segments).strip()


class YTCaption:
    def __init__(self, video, code, formats):
        self.video = video
        self.code = code
        self.formats = formats

    def generate_srt_captions(self):
        fmt = next((f for f in self.formats if f.get("ext") == "srv1"), None)
        if fmt is None:
            return ""
        with YoutubeDL(self.video.params) as ydl:
            xml_captions = ydl.urlopen(fmt["url"]).read().decode("utf-8")
        return srv1_to_srt(xml_captions)

    def __repr__(self):
        return f"<YTCaption {self.code}>"


class YTStream:
    def __init__(self, video):
        self.video = video

    def download(self, output_path, filename, max_retries=1, timeout=60, ffmpeg_location=None):
        """Download to output_path/<filename without ext>.mp4 through the same proxy that
        extracted the info (stream urls are tied to that IP). Returns the file path.

        timeout is the per-socket idle timeout, not the whole download. Retries stay low
        because the caller switches proxies; a dead proxy should not sit here for minutes.
        """
        base = os.path.splitext(filename)[0]
        params = dict(
            self.video.params,
            outtmpl=os.path.join(output_path, base).replace("%", "%%") + ".%(ext)s",
            merge_output_format="mp4",
            retries=max_retries,
            fragment_retries=max_retries,
            socket_timeout=timeout,
            overwrites=True,
            continuedl=False,
        )
        if ffmpeg_location:
            params["ffmpeg_location"] = ffmpeg_location
        with YoutubeDL(params) as ydl:
            ydl.process_ie_result(copy.deepcopy(self.video.info), download=True)

        path = os.path.join(output_path, base + ".mp4")
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise YTVideoError(f"yt-dlp finished but {path} is missing or empty", "unknown")
        return path


class YTVideo:
    def __init__(self, url, proxy=None):
        self.proxy = proxy
        self.params = _base_params(proxy)
        with YoutubeDL(self.params) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            if not info:
                raise YTVideoError("yt-dlp returned no video info", "unknown")
            _require_single_video(info)
            self.info = ydl.process_ie_result(info, download=False)
            _require_single_video(self.info)

            live_status = self.info.get("live_status")
            if self.info.get("is_live") or live_status == "is_live":
                raise YTVideoError(
                    "Video is a live stream - can't download it until it ends", "unavailable_now")
            if live_status == "is_upcoming":
                raise YTVideoError(
                    "Video is scheduled and has not aired yet", "unavailable_now")

            self.video_id = self.info.get("id")
            self.title = self.info.get("title") or ""
            self.description = self.info.get("description") or ""
        # Thumbnail HEAD requests are deferred. The pull form only needs title and description.
        self._thumbnail_url = None
        height = self.info.get("height")
        self.res = f"{height}p" if height else ""
        self.stream = YTStream(self)

    @property
    def thumbnail_url(self):
        if self._thumbnail_url is None:
            with YoutubeDL(self.params) as ydl:
                self._thumbnail_url = self._pick_thumbnail(ydl)
        return self._thumbnail_url

    def _pick_thumbnail(self, ydl):
        # Prefer jpg - thumbnails get saved as-is and the webp ones confuse older players.
        # yt-dlp lists guessed urls (e.g. maxresdefault.jpg) that don't exist for every video.
        jpgs = [t["url"] for t in reversed(self.info.get("thumbnails") or []) if ".jpg" in (t.get("url") or "")]
        for url in jpgs[:3]:
            try:
                ydl.urlopen(HEADRequest(url)).close()
                return url
            except Exception:
                continue
        # hqdefault exists for every video
        return f"https://i.ytimg.com/vi/{self.video_id}/hqdefault.jpg"

    @property
    def captions(self):
        """Uploaded caption tracks plus the auto-generated track in the video's own language
        (code "a.<lang>", same as pytubefix). Auto-translated tracks are skipped."""
        tracks = []
        for lang, formats in (self.info.get("subtitles") or {}).items():
            if lang == "live_chat":
                continue
            tracks.append(YTCaption(self, lang, formats))

        auto = self.info.get("automatic_captions") or {}
        orig = [k for k in auto if k.endswith("-orig")]
        if orig:
            for k in orig:
                tracks.append(YTCaption(self, "a." + k[:-len("-orig")], auto[k]))
        elif self.info.get("language") in auto:
            lang = self.info["language"]
            tracks.append(YTCaption(self, "a." + lang, auto[lang]))
        return tracks
