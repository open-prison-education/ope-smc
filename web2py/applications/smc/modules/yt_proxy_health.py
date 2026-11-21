# -*- coding: utf-8 -*-
"""
Cheap health checks for YouTube proxies.

Two checks, both against YouTube:
  - alive: GET youtube.com/generate_204 through the proxy. Only proves the proxy tunnel works.
    A plain TCP connect isn't enough. A proxy gateway accepts the connection
    even when the port behind it is dead.
  - bot: one WEB client player request. A flagged IP gets "Sign in to confirm you're not a bot";
    a clean IP gets another answer (usually "The page needs to be reloaded" because we don't
    send a PO token). This is the same bot wall a real download hits first.
"""
import datetime
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

# Statuses stored in youtube_proxy_list.status
OK = "ok"
UNKNOWN = "unknown"
BOT = "bot"
RATE_LIMITED = "rate_limited"
DEAD = "dead"
FAILED = "failed"
BAD_STATUSES = (BOT, RATE_LIMITED, DEAD, FAILED)

OK_RECHECK = datetime.timedelta(minutes=30)
FIRST_BACKOFF = datetime.timedelta(minutes=15)
MAX_BACKOFF = datetime.timedelta(hours=6)

PROBE_VIDEO_ID = "jNQXAC9IVRw"  # "Me at the zoo" - first video on YouTube, not going anywhere
FALLBACK_WEB_CLIENT = {"clientName": "WEB", "clientVersion": "2.20260708.00.00", "hl": "en"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def _web_client_context():
    # yt-dlp keeps the WEB client version current - use it when available
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        return dict(INNERTUBE_CLIENTS["web"]["INNERTUBE_CONTEXT"]["client"])
    except Exception:
        return dict(FALLBACK_WEB_CLIENT)


def urllib_can_probe(proxy_url):
    """urllib speaks http proxies. socks and https-to-the-proxy schemes fail the probe
    even when yt-dlp can use them, which would mark a working proxy dead."""
    scheme = (urlparse(proxy_url or "").scheme or "").lower()
    return scheme in ("http", "")


def _opener(proxy_url):
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))


def urlopen_through_proxy(url, proxy_url, timeout=30):
    """GET url through proxy_url without touching the process-global urllib opener."""
    if proxy_url and not urllib_can_probe(proxy_url):
        raise OSError(f"urllib cannot use this proxy scheme: {urlparse(proxy_url).scheme}")
    if proxy_url:
        opener = _opener(proxy_url)
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return opener.open(req, timeout=timeout)


_reach_cache = {"at": 0.0, "ok": False}


def youtube_reachable(timeout=5):
    """Direct generate_204. False means this machine cannot reach YouTube, so a failed
    proxy probe says nothing about the proxy. Cached for 30s."""
    now = time.time()
    if now - _reach_cache["at"] < 30:
        return _reach_cache["ok"]
    ok = False
    req = urllib.request.Request("https://www.youtube.com/generate_204", headers={"User-Agent": USER_AGENT})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            ok = resp.status == 204
    except Exception:
        ok = False
    _reach_cache["at"] = now
    _reach_cache["ok"] = ok
    return ok


def check_alive(proxy_url, timeout=10):
    """Returns (ok, msg). Working proxies can take 5+ seconds; dead ports never answer."""
    req = urllib.request.Request("https://www.youtube.com/generate_204", headers={"User-Agent": USER_AGENT})
    try:
        with _opener(proxy_url).open(req, timeout=timeout) as resp:
            return resp.status == 204, f"generate_204 returned {resp.status}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


def check_youtube(proxy_url, timeout=10):
    """One player request through the proxy. Returns (status, msg) with status OK, BOT,
    RATE_LIMITED, DEAD or UNKNOWN (unexpected answer - don't trust either way)."""
    body = json.dumps({"videoId": PROBE_VIDEO_ID, "context": {"client": _web_client_context()}}).encode()
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/player?prettyPrint=false", data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with _opener(proxy_url).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        if ex.code == 429:
            return RATE_LIMITED, "HTTP 429 from YouTube"
        return UNKNOWN, f"HTTP {ex.code} from YouTube"
    except Exception as ex:
        return DEAD, f"{type(ex).__name__}: {ex}"

    playability = data.get("playabilityStatus") or {}
    status = playability.get("status", "")
    reason = playability.get("reason", "")
    msg = f"{status}: {reason}".strip(": ")
    if "not a bot" in reason:
        return BOT, msg
    if "try again later" in reason.lower():
        return RATE_LIMITED, msg
    if status in ("OK", "UNPLAYABLE"):
        return OK, msg
    return UNKNOWN, msg


def probe(proxy_url):
    """Full check: alive first (fast fail for dead ports), then the YouTube bot check."""
    alive, msg = check_alive(proxy_url)
    if not alive:
        return DEAD, msg
    return check_youtube(proxy_url)


def probe_many(proxy_urls, max_workers=10):
    """Probe proxies in parallel. Returns {proxy_url: (status, msg)}."""
    proxy_urls = list(proxy_urls)
    if not proxy_urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(proxy_urls))) as pool:
        return dict(zip(proxy_urls, pool.map(probe, proxy_urls)))


def check_alive_many(proxy_urls, max_workers=10):
    """check_alive in parallel. Returns {proxy_url: (alive, msg)}."""
    proxy_urls = list(proxy_urls)
    if not proxy_urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(proxy_urls))) as pool:
        return dict(zip(proxy_urls, pool.map(check_alive, proxy_urls)))


def next_check_after(status, consecutive_failures, now=None):
    """When a proxy should be checked again. Bad proxies back off 15m, 30m, 1h ... up to 6h."""
    now = now or datetime.datetime.now()
    if status in BAD_STATUSES:
        backoff = FIRST_BACKOFF * (2 ** min(max(consecutive_failures - 1, 0), 10))
        return now + min(backoff, MAX_BACKOFF)
    return now + OK_RECHECK
