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
  - ffmpeg on the PATH for videos that only have separate video/audio streams.
"""
import copy
import os
import xml.etree.ElementTree as ElementTree
from html import unescape

from yt_dlp import YoutubeDL
from yt_dlp.networking import HEADRequest

POT_PROVIDER_URL = os.environ.get("YT_POT_PROVIDER_URL", "http://127.0.0.1:4416")

# Single file 360p mp4 first (no merge needed), then 480p h264 + aac merged by ffmpeg.
# Stick to h264/aac so the result plays in every browser.
VIDEO_FORMAT = "18/bv*[ext=mp4][vcodec^=avc1][height<=480]+ba[ext=m4a]/b[ext=mp4]"

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
    "inappropriate for some users", "this live event will begin", "premieres in",
    "is not a valid url", "unsupported url", "incomplete youtube id",
    "account associated with this video has been terminated", "live stream",
)


class YTVideoError(Exception):
    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


def classify_error(ex):
    """Returns one of: rate_limited, bot, region, proxy, forbidden, permanent, unknown.

    rate_limited/bot/region/proxy/forbidden are problems with the proxy (try another one),
    permanent means no proxy will help, unknown means give up for now and try again later.
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
        ("permanent", PERMANENT_MARKERS),
    ):
        if any(m in msg for m in markers):
            return kind
    return "unknown"


def _js_runtimes():
    runtimes = {"deno": {}}
    try:
        import nodejs_wheel.executable
        runtimes["node"] = {"path": os.path.join(nodejs_wheel.executable.ROOT_DIR, "bin", "node")}
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

    def download(self, output_path, filename, max_retries=3, timeout=300):
        """Download to output_path/<filename without ext>.mp4 through the same proxy that
        extracted the info (stream urls are tied to that IP). Returns the file path."""
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
            self.info = ydl.extract_info(url, download=False)

            if self.info.get("is_live"):
                raise YTVideoError("Video is a live stream - can't download it until it ends", "permanent")

            self.video_id = self.info.get("id")
            self.title = self.info.get("title") or ""
            self.description = self.info.get("description") or ""
            self.thumbnail_url = self._pick_thumbnail(ydl)
        height = self.info.get("height")
        self.res = f"{height}p" if height else ""
        self.stream = YTStream(self)

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
