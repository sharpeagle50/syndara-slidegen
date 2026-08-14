"""Download and validate web images for insertion into PPTX slides."""
from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

from .. import keyring

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 15 * 1024 * 1024
MIN_DIM = 400
WARN_DIM = 600
MAX_DIM = 4000
MAX_CONCURRENT = 5
# Wikimedia's robot policy (w.wiki/4wJS) requires bots to identify themselves with contact
# info (URL or email) in the UA; anonymous product strings get bucketed as non-compliant
# and 429'd almost immediately. The contact address is the public one from our ToS.
USER_AGENT = "SyndaraBot/1.0 (https://syndara.org; ava@syndara.org) httpx"


# ── Per-domain politeness: shared throttle + 429 circuit breaker ─────────────────────────
# Every fetch path (direct plan URLs, page-extract candidates, candidate-page HTML) goes
# through the same per-domain gate. Without it, several planner agents running in parallel
# hammer one host (usually upload.wikimedia.org) from a single egress IP, get rate-limited,
# and then every subsequent candidate burns retries + backoff on a host that has already
# said no — a 429 storm that turns image sourcing into the build's long pole.
_DOMAIN_MIN_INTERVAL = 0.5   # seconds between requests to any one domain, process-wide
_429_TRIP_COUNT = 3          # consecutive 429/503s from a domain before backing off entirely
_429_COOLDOWN_S = 120.0

# A threading.Lock, NOT asyncio.Lock: callers run in many short-lived event loops across
# several planner threads (each find_image tool call is its own asyncio.run), and an
# asyncio.Lock binds to the first loop that touches it, raising "bound to a different event
# loop" everywhere else. This lock only guards synchronous dict math — it is never held
# across an await, so it can't stall an event loop.
_domain_lock = threading.Lock()
_domain_next_slot: dict[str, float] = {}
_domain_429_streak: dict[str, int] = {}
_domain_cooldown_until: dict[str, float] = {}


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _domain_cooling(url: str) -> float:
    """Seconds of 429-cooldown remaining for this URL's domain (0.0 = OK to fetch)."""
    return max(0.0, _domain_cooldown_until.get(_domain_of(url), 0.0) - time.monotonic())


async def _throttle_domain(url: str) -> None:
    dom = _domain_of(url)
    if not dom:
        return
    with _domain_lock:
        # Reserve this domain's next slot while holding the lock, then sleep WITHOUT the
        # lock — otherwise one domain's throttle serializes every other domain's requests.
        fire_at = max(time.monotonic(), _domain_next_slot.get(dom, 0.0) + _DOMAIN_MIN_INTERVAL)
        _domain_next_slot[dom] = fire_at
    wait = fire_at - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)


def _note_rate_limited(url: str) -> None:
    dom = _domain_of(url)
    if not dom:
        return
    with _domain_lock:
        streak = _domain_429_streak.get(dom, 0) + 1
        _domain_429_streak[dom] = streak
        if streak >= _429_TRIP_COUNT:
            _domain_cooldown_until[dom] = time.monotonic() + _429_COOLDOWN_S
    if streak >= _429_TRIP_COUNT:
        log.warning("domain %s tripped the 429 breaker (%d consecutive) — cooling down %.0fs",
                    dom, streak, _429_COOLDOWN_S)


def _note_responded(url: str) -> None:
    """Any non-rate-limit response (even a 404) means the domain is talking to us again."""
    with _domain_lock:
        _domain_429_streak.pop(_domain_of(url), None)


# ── Wikimedia URL normalization ──────────────────────────────────────────────────────────
# upload.wikimedia.org only serves bots the pre-rendered standard thumbnail widths
# (w.wiki/GHai); originals ("thumbnail_unscaled") and odd widths draw a 429 regardless of
# how polite the UA is. Scraped URLs also carry utm_* / cache-buster query params that make
# identical images look distinct to the dedupe. Normalizing fixes both.
# Standard pre-rendered widths we target, floored at 500: a sub-MIN_DIM thumb is useless to
# us, and the full-size original usually exists, so ask for a size that can actually pass
# validation. (If the original is smaller than the requested width Wikimedia errors and the
# candidate loop simply moves on — no worse than today, where the small fetch fails MIN_DIM.)
_WIKIMEDIA_THUMB_BUCKETS = (500, 960)
_WM_THUMB_RE = re.compile(
    r"^(?P<base>/wikipedia/[^/]+/thumb/[^/]/[^/]{2}/[^/]+)/"
    r"(?P<pre>(?:lossy(?:-page\d+)?-|lossless(?:-page\d+)?-|page\d+-)?)(?P<w>\d+)px-(?P<rest>.+)$")
_WM_ORIG_RE = re.compile(
    r"^(?P<proj>/wikipedia/[^/]+)/(?P<d1>[^/])/(?P<d2>[^/]{2})/(?P<fname>[^/]+)$")


def _wm_snap_width(requested: int) -> int:
    return next((b for b in _WIKIMEDIA_THUMB_BUCKETS if b >= requested), 960)


def _normalize_wikimedia_url(u: str) -> str:
    try:
        sp = urlsplit(u)
        if sp.netloc.lower() != "upload.wikimedia.org":
            return u
        path = sp.path
        m = _WM_THUMB_RE.match(path)
        if m:
            w = _wm_snap_width(int(m.group("w")))
            path = f"{m.group('base')}/{m.group('pre')}{w}px-{m.group('rest')}"
        else:
            m = _WM_ORIG_RE.match(path)
            if m:
                fname = m.group("fname")
                thumbname = f"960px-{fname}"
                if fname.lower().endswith(".svg"):
                    thumbname += ".png"
                elif fname.lower().endswith((".tif", ".tiff")):
                    thumbname = f"lossy-page1-{thumbname}.jpg"
                path = (f"{m.group('proj')}/thumb/{m.group('d1')}/{m.group('d2')}/"
                        f"{fname}/{thumbname}")
        # Query strings on upload.wikimedia.org are never load-bearing (utm_*, `_=` busters).
        return urlunsplit((sp.scheme, sp.netloc, path, "", ""))
    except Exception:
        return u


def _empty_result(path: str = "", error: str = "") -> dict:
    return {
        "success": False, "path": path,
        "width_px": 0, "height_px": 0, "aspect": 0.0,
        "format": "", "file_size_bytes": 0, "error": error,
    }


def _process_image(raw_bytes: bytes, out_path: str) -> dict:
    from PIL import Image

    img = Image.open(io.BytesIO(raw_bytes))
    img_format = (img.format or "").upper()

    # Capture rights metadata (CMI) BEFORE the re-save drops it: if the original file names an
    # author or copyright holder, keep the claim alongside the processed file instead of silently
    # destroying it (removing copyright-management information is its own DMCA §1202 issue, apart
    # from any infringement question). Persisted into the fetch result → plan → DB.
    exif_artist = exif_copyright = ""
    try:
        _ex = img.getexif()
        exif_artist = str(_ex.get(315) or "").strip()[:200]       # TIFF/EXIF tag: Artist
        exif_copyright = str(_ex.get(33432) or "").strip()[:200]  # TIFF/EXIF tag: Copyright
    except Exception:
        pass

    if img_format in ("WEBP", "GIF"):
        img = img.convert("RGBA")
        img_format = "PNG"

    # Pixel data only from here: the save() calls below never pass exif=/icc_profile=, so PIL
    # writes pixels only. (The old frombytes() round-trip here silently rebuilt palette PNGs
    # with an empty palette, corrupting their colors.)

    w, h = img.size
    shortest = min(w, h)
    if shortest < MIN_DIM:
        return _empty_result(error=f"Image too small ({w}x{h}), minimum {MIN_DIM}px on shortest side")
    if shortest < WARN_DIM:
        log.warning("Image is low-resolution (%dx%d), proceeding anyway", w, h)

    # Downscale if any dimension exceeds MAX_DIM
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        w, h = int(w * scale), int(h * scale)
        resample = getattr(Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1))
        img = img.resize((w, h), resample)

    out = Path(out_path)
    if img_format == "JPEG":
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=90)
    else:
        img_format = "PNG"
        out = out.with_suffix(".png")
        img.save(out, format="PNG")

    file_size = out.stat().st_size
    return {
        "success": True, "path": str(out),
        "width_px": w, "height_px": h,
        "aspect": round(w / max(h, 1), 3),
        "format": img_format, "file_size_bytes": file_size, "error": "",
        "exif_artist": exif_artist, "exif_copyright": exif_copyright,
    }


async def fetch_web_image(url: str, out_path: str, timeout: float = 20.0, referer: str = "") -> dict:
    import httpx

    # Retry a couple times on 429/503 (e.g. Wikimedia throttling us) with a short backoff
    # that honors Retry-After — these are valid images we simply requested too fast. The
    # Accept header nudges servers that content-negotiate to send the image, not an HTML page.
    base_headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/png,image/*,*/*"}
    headers = dict(base_headers)
    # NOTE: `referer` is accepted for signature compatibility but deliberately NOT used. A 401/403
    # is a site's hotlink protection saying no — retrying with a spoofed Referer to defeat it
    # (removed 2026-08-03) would turn "we fetched a public image" into circumventing the owner's
    # explicit block, which is indefensible if a rights dispute ever reaches a courtroom. We treat
    # 401/403 as a plain failure and let the pipeline pick a different image.
    _ = referer
    url = _normalize_wikimedia_url(url)
    for attempt in range(3):
        cooling = _domain_cooling(url)
        if cooling > 0:
            return _empty_result(
                error=f"{_domain_of(url)} is rate-limiting us (429); cooling down {cooling:.0f}s more")
        await _throttle_domain(url)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, max_redirects=5, headers=headers,
            ) as client:
                resp = await client.get(url)

            if resp.status_code in (429, 503):
                _note_rate_limited(url)
                if attempt < 2:
                    ra = resp.headers.get("retry-after", "")
                    try:
                        delay = float(ra)
                    except ValueError:
                        delay = 1.5 * (attempt + 1)
                    await asyncio.sleep(min(delay, 6.0))
                    continue
            else:
                _note_responded(url)

            resp.raise_for_status()

            ct = resp.headers.get("content-type", "")
            if "html" in ct.lower():
                return _empty_result(error="Server returned HTML instead of an image (possible hotlink protection)")
            if not ct.startswith("image/"):
                return _empty_result(error=f"Unexpected Content-Type: {ct}")
            if len(resp.content) > MAX_FILE_BYTES:
                return _empty_result(error=f"File too large ({len(resp.content)} bytes, max {MAX_FILE_BYTES})")

            return _process_image(resp.content, out_path)

        except Exception as e:
            log.warning("fetch_web_image failed for %s: %s", url, e)
            return _empty_result(error=str(e)[:500])

    return _empty_result(error="rate-limited (429/503) after retries")


# Image search uses Sonnet 5, not Haiku: the web_search_20260209 dynamic-filtering tool
# requires Opus 4.6+ / Sonnet 4.6+, and a stronger model picks better image candidates (the
# vision-verify step below rejects bad picks, so better candidates mean fewer rejections).
# Cost is negligible here — search is a per-image fallback and the output is just a URL.
SEARCH_MODEL = "claude-sonnet-5"


async def search_and_fetch_image(query: str, out_path: str) -> dict:
    import anthropic

    client = keyring.async_anthropic()
    try:
        resp = await client.messages.create(
            model=SEARCH_MODEL,
            # Headroom for Sonnet 5's adaptive thinking (on by default) — thinking
            # tokens share this budget, so a tight cap could truncate before the URL.
            max_tokens=4096,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{
                "role": "user",
                "content": (
                    f"Find a single high-quality image URL for: {query}\n"
                    "Return ONLY the direct image URL — a link that points straight at the "
                    "image file itself, not at a webpage that contains it. Many image hosts "
                    "(e.g. CDNs) serve images at URLs with no file extension; that is fine. "
                    "No explanation needed."
                ),
            }],
        )
    except Exception as e:
        return _empty_result(error=f"Search API error: {e}")
    try:
        from ..agents.base import report_usage
        report_usage("image_search", SEARCH_MODEL, resp.usage)
    except Exception:
        pass

    url = None
    for block in resp.content:
        if getattr(block, "text", None):
            # Accept any URL the model returns, not just ones ending in an image
            # extension — modern CDNs (Unsplash, Wikimedia, S3) serve images at
            # extensionless URLs. fetch_web_image() validates the Content-Type and
            # rejects anything that isn't actually an image.
            match = re.search(r'https?://[^\s<>"\'`\])]+', block.text, re.I)
            if match:
                url = match.group(0).rstrip(".,;:)]}\"'")
                break

    if not url:
        return _empty_result(error="No image found for query")

    return await fetch_web_image(url, out_path)


# ── Agentic image acquisition: page-extract, not model-guess ─────────────────────────────
# Asking a model to recall an image URL makes it hallucinate plausible-but-dead URLs (the
# fake ctfassets asset IDs we saw). Instead: search for candidate PAGES, fetch their real
# HTML, and extract the actual <img>/og:image URLs from the markup — then download +
# vision-verify, trying the next candidate on any failure. This mirrors how an agent finds
# an image and structurally eliminates hallucinated-URL 404s.

_OG_IMAGE_RES = [
    re.compile(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
]
# Lazy-loading sites stash a placeholder in src= and the REAL image in a data-* attribute.
# Pull the lazy attrs separately and add them BEFORE plain src= so the real image is tried
# first. (<noscript><img> fallbacks need no special case — we scan raw HTML, so the plain
# _IMG_SRC_RE already matches an <img> inside <noscript>.)
_LAZY_SRC_RE = re.compile(r'<img\b[^>]*?\b(?:data-src|data-original|data-lazy-src|data-lazy)=["\']([^"\']+)["\']', re.I)
_IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.I)
_SRCSET_RE = re.compile(r'\b(?:data-srcset|srcset)=["\']([^"\']+)["\']', re.I)

# Image optimizer/proxy paths (Next.js, Cloudflare, imgproxy, Cloudinary fetch, etc.). These
# wrap the real CDN original in a `url`/`u` query param and frequently 400 on a direct GET.
_OPTIMIZER_PATH_RE = re.compile(r'(?i)(/_next/image|/cdn-cgi/image|/imgproxy|/image/fetch|/_vercel/image)')


def _unwrap_optimizer_url(u: str) -> str:
    """If `u` is an image-optimizer/proxy URL that wraps a real absolute image URL in its
    `url`/`u` param (e.g. `/_next/image?url=https%3A%2F%2Fcdn...png&w=1920&q=75`), return the
    unwrapped original — the wrapper itself usually 400s on a direct fetch. Otherwise return
    `u` unchanged. Run AFTER html-unescaping so the query string parses correctly."""
    try:
        from urllib.parse import urlsplit, parse_qs, unquote
        sp = urlsplit(u)
        if not _OPTIMIZER_PATH_RE.search(sp.path):
            return u
        # (a) query-param form: ?url=<encoded absolute URL> (Next.js, Vercel, weserv).
        inner = (parse_qs(sp.query).get("url") or parse_qs(sp.query).get("u") or [""])[0]
        inner = unquote(inner) if inner else ""
        if inner.startswith(("http://", "https://")):
            return inner
        # (b) path-embedded form: the original absolute URL sits in the path after the transform
        # segment (Cloudflare /cdn-cgi/image/<opts>/https://..., Cloudinary /image/fetch/<opts>/
        # https%3A//..., Thumbor). Only unwrap when a literal http(s):// is present after decoding,
        # so we never guess at a scheme-less original.
        dec = unquote(sp.path)
        m = re.search(r'https?://\S+', dec)
        if m:
            return m.group(0)
    except Exception:
        pass
    return u


def _extract_page_image_urls(html: str, base_url: str, limit: int = 8) -> list[str]:
    """Pull real candidate image URLs from a page's raw HTML (og:image, srcset, <img>).
    Resolves relative URLs; skips data:/svg/obvious icons. Most-representative first."""
    from html import unescape as _unescape
    from urllib.parse import urljoin, urlsplit
    out: list[str] = []
    seen: set = set()

    def add(u: str):
        u = (u or "").strip()
        if not u or u.startswith("data:"):
            return
        # HTML attribute values encode `&` as `&amp;`; decode before use or the query string
        # is malformed (e.g. `?url=...&amp;w=1920` 400s). Then unwrap image-optimizer proxies.
        u = _unwrap_optimizer_url(_unescape(u))
        full = _normalize_wikimedia_url(urljoin(base_url, u))
        low = full.lower()
        if not full.startswith(("http://", "https://")):
            return
        # Skip vector chrome and non-image media (video/audio): a srcset/og:image can point at a
        # .mp4 etc., which only wastes a candidate slot (the Content-Type check would reject it
        # anyway). Test the extension on the PATH so a `?v=2` query string doesn't defeat it.
        path_low = urlsplit(full).path.lower()
        if (path_low.endswith((".svg", ".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".ogv", ".mp3", ".pdf"))
                or "sprite" in low or "/icon" in low or "logo" in low):
            return
        # Page chrome that survives the checks above: favicons, CC license badges, and other
        # tiny insignia (all over Wikimedia/Commons pages). Each one is a doomed candidate —
        # it would fail the MIN_DIM check after download — so drop it before spending a fetch.
        if re.search(r"(?i)(favicon|wordmark|badge|cc[-_](?:by|sa|nc|nd|zero|some)|creative_commons"
                     r"|copyright_icon|_icon\.)", path_low):
            return
        if full in seen:
            return
        seen.add(full)
        out.append(full)

    for rx in _OG_IMAGE_RES:
        for m in rx.findall(html):
            add(m)
    for ss in _SRCSET_RE.findall(html):
        cands = [p.strip().split(" ")[0] for p in ss.split(",") if p.strip()]
        if cands:
            add(cands[-1])   # largest entry in the srcset
    for m in _LAZY_SRC_RE.findall(html):   # real lazy-loaded urls before any src= placeholder
        add(m)
    for m in _IMG_SRC_RE.findall(html):
        add(m)
    return out[:limit]


async def _fetch_page_html(url: str, timeout: float = 15.0) -> str:
    import httpx
    if _domain_cooling(url) > 0:
        log.info("skipping page %s: domain cooling down after repeated 429s", url)
        return ""
    await _throttle_domain(url)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=5,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        ) as client:
            r = await client.get(url)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "").lower():
            return r.text
    except Exception as e:
        log.info("page fetch failed for %s: %s", url, e)
    return ""


async def _search_candidate_pages(query: str, max_pages: int = 5) -> list[str]:
    """Real candidate PAGE urls from the web_search index (not model-emitted image URLs)."""
    import anthropic
    client = keyring.async_anthropic()
    urls: list[str] = []
    try:
        resp = await client.messages.create(
            # max_tokens has headroom for Sonnet 5 adaptive thinking (shares the budget).
            model=SEARCH_MODEL, max_tokens=4096,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": (
                f"Search the web for pages that contain a real, embeddable image (screenshot or "
                f"photo) of: {query}. Prefer official docs, product pages, reputable "
                f"blogs/tutorials, or Wikimedia. You don't need to write an answer."
            )}],
        )
        try:
            from ..agents.base import report_usage
            report_usage("image_page_search", SEARCH_MODEL, resp.usage)
        except Exception:
            pass
        for block in resp.content:
            if getattr(block, "type", "") == "web_search_tool_result":
                for r in (getattr(block, "content", None) or []):
                    u = getattr(r, "url", None) or (r.get("url") if isinstance(r, dict) else None)
                    if u:
                        urls.append(u)
            elif getattr(block, "text", None):
                urls += re.findall(r'https?://[^\s<>"\'`\])]+', block.text)
    except Exception as e:
        log.warning("candidate-page search failed for %r: %s", query, e)
    finally:
        try:
            await client.close()
        except Exception:
            pass
    seen: set = set()
    deduped: list[str] = []
    for u in urls:
        u = u.rstrip('.,;:)]}"\'')
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped[:max_pages]


async def find_images_for_target(query: str, intent: str, out_path: str, *,
                                 max_pages: int = 5, max_candidates: int = 12) -> dict:
    """Agentic page-extract image finder: search pages -> fetch real HTML -> extract real
    <img> URLs -> download + vision-verify -> first match wins (retry next candidate on any
    failure). Returns a fetch_web_image result dict (with caption/source on success) or an
    _empty_result. No model ever emits an image URL, so hallucinated-URL 404s can't happen."""
    pages = await _search_candidate_pages(query, max_pages=max_pages)
    if not pages:
        return _empty_result(error="no candidate pages found")
    last_error = "no usable image on candidate pages"
    tried = 0
    for page in pages:
        html = await _fetch_page_html(page)
        if not html:
            continue
        for img_url in _extract_page_image_urls(html, page):
            if tried >= max_candidates:
                return _empty_result(error=f"exhausted {max_candidates} candidates; last: {last_error}")
            # A cooling domain is a known dead end — skip WITHOUT burning a candidate slot,
            # so one rate-limited host doesn't starve candidates hosted elsewhere.
            if _domain_cooling(img_url) > 0:
                last_error = f"{_domain_of(img_url)} cooling down after repeated 429s"
                continue
            tried += 1
            res = await fetch_web_image(img_url, out_path, referer=page)
            if not (res and res.get("success")):
                last_error = (res or {}).get("error", last_error)
                continue
            v = await verify_image(intent or query, res["path"])
            if v.get("matches"):
                res["caption"] = v.get("caption", "")
                res["source_page"] = page
                res["src_url"] = img_url
                return res
            last_error = f"vision rejected: {v.get('reason', '')}"
    return _empty_result(error=last_error)


VISION_MODEL = "claude-sonnet-5"


async def verify_image(intent: str, image_path: str) -> dict:
    """Vision check: does the downloaded image genuinely depict `intent` (and not a
    logo, wordmark, generic branding, or an unrelated stock photo)? Returns
    {matches, caption, reason}, where `caption` describes what the image ACTUALLY
    shows. Fail-OPEN on technical error (keep the image) — only the model's own
    judgment rejects an image, never an API hiccup."""
    import base64
    import json as _json

    if not intent or not image_path:
        return {"matches": True, "caption": "", "reason": "no-intent"}
    client = None
    try:
        import anthropic
        import io as _io
        from PIL import Image

        # Downscale a copy just for verification — a small thumbnail is plenty to judge
        # the subject (and "logo vs. real screenshot"), and it keeps the request tiny and
        # fast regardless of the source image's size.
        with Image.open(image_path) as _im:
            _im = _im.convert("RGB")
            _im.thumbnail((768, 768))
            _buf = _io.BytesIO()
            _im.save(_buf, format="JPEG", quality=85)
        b64 = base64.standard_b64encode(_buf.getvalue()).decode()
        client = keyring.async_anthropic()
        resp = await client.messages.create(
            model=VISION_MODEL,
            # Headroom for Sonnet 5 adaptive thinking (shares the budget) before the JSON verdict.
            max_tokens=3000,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": (
                    "You are verifying an image chosen to illustrate a slide.\n"
                    f'The slide needs an image of: "{intent}".\n'
                    "Look at the ACTUAL image. Does it genuinely depict that subject? "
                    "Answer NO if it is a logo, wordmark, generic branding, an unrelated "
                    "stock photo, or otherwise not actually showing the subject. But answer "
                    "YES if it genuinely depicts the subject, even if it is not a perfect, "
                    "official, or high-resolution example.\n"
                    'Respond with ONLY JSON: {"matches": true|false, '
                    '"caption": "one factual sentence describing what the image actually shows", '
                    '"reason": "brief"}'
                )},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": b64,
                }},
            ]}],
        )
        try:
            from ..agents.base import report_usage
            report_usage("image_verify", VISION_MODEL, resp.usage)
        except Exception:
            pass
        text = "".join(getattr(b, "text", "") or "" for b in resp.content)
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            d = _json.loads(m.group(0))
            _matches = bool(d.get("matches", True))
            _caption = str(d.get("caption", "")).strip()
            _reason = str(d.get("reason", "")).strip()
            # Record the verify decision so a downstream "inaccurate_visual" QA flag can be
            # correlated: did this image already pass the fetch-time relevance check?
            try:
                from ..agents.base import report_gen_event
                report_gen_event("image", "verify " + ("kept" if _matches else "REJECTED"),
                                 {"intent": (intent or "")[:200], "reason": _reason[:300],
                                  "caption": _caption[:300]})
            except Exception:
                pass
            return {"matches": _matches, "caption": _caption, "reason": _reason}
    except Exception as e:
        log.warning("verify_image failed for %s: %s", image_path, e)
    finally:
        # Close the async client before the per-call event loop tears down, else its httpx
        # cleanup fires on a closed loop ("RuntimeError: Event loop is closed").
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return {"matches": True, "caption": "", "reason": "verify-error"}


async def download_plan_images(image_entries: list[dict], images_dir: str) -> dict:
    Path(images_dir).mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _download_one(idx: int, entry: dict) -> tuple[str, dict]:
        heading = entry.get("slide_heading", f"slide_{idx}")
        ext = ".png"
        filename = f"web_img_{idx + 1:02d}{ext}"
        out_path = str(Path(images_dir) / filename)

        # What this image is supposed to depict, used to vision-verify the fetch. Combine
        # the DESCRIPTIVE signals (search query + slide heading) only — never the generic
        # "slide_N" placeholder (meaningless) or the attribution (a source, not a subject).
        # If there's no real intent, verification is skipped rather than judged against a
        # junk string (which would spuriously reject good images).
        _intent_parts = []
        for _p in (entry.get("search_query", ""), entry.get("slide_heading", "")):
            _p = (_p or "").strip()
            if _p and not re.match(r"(?i)^slide[ _-]?\d+$", _p):
                _intent_parts.append(_p)
        intent = " — ".join(dict.fromkeys(_intent_parts))  # dedupe, keep order

        async with sem:
            caption = ""
            last_error = "No URL or search query provided"
            result = None

            # Attempt 0: a curated, owner-vetted image (our own reliable hosting), injected by
            # the caller for recurring subjects (e.g. Claude UI). Pre-vetted, so trust it
            # without a vision check.
            curated_url = entry.get("curated_url", "")
            if curated_url:
                result = await fetch_web_image(curated_url, out_path)
                if result and result.get("success"):
                    caption = entry.get("curated_caption", "") or caption
                else:
                    last_error = (result or {}).get("error", last_error)
                    result = None

            # Attempt 1: a direct URL from the plan, then vision-verify it.
            image_url = entry.get("image_url", "")
            if (not result or not result.get("success")) and image_url:
                result = await fetch_web_image(image_url, out_path)
                if result and result.get("success"):
                    v = await verify_image(intent, result["path"])
                    if v["matches"]:
                        caption = v["caption"]
                    else:
                        last_error = f"direct image rejected by vision check: {v.get('reason', '')}"
                        result = None  # fall through to search
                elif result:
                    last_error = result.get("error", last_error)

            # Attempt 2: agentic page-extract finder — search candidate pages, pull real
            # <img> URLs from their HTML, download + vision-verify, trying the next candidate
            # on failure. (Verification happens inside, so we don't re-verify here.)
            if (not result or not result.get("success")) and entry.get("search_query"):
                result = await find_images_for_target(entry["search_query"], intent, out_path)
                if result and result.get("success"):
                    caption = result.get("caption", caption)
                elif result:
                    last_error = result.get("error", last_error)

            if not result or not result.get("success"):
                return heading, {
                    "path": "", "width_px": 0, "height_px": 0, "aspect": 0.0,
                    "attribution": entry.get("attribution", ""),
                    "caption": "",
                    "failed": True,
                    "error": last_error,
                }

            return heading, {
                "path": result["path"],
                "width_px": result["width_px"],
                "height_px": result["height_px"],
                "aspect": result["aspect"],
                "attribution": entry.get("attribution", ""),
                "source": entry.get("source", "") or result.get("source_page", ""),
                "caption": caption,
                "failed": False, "error": "",
            }

    tasks = [_download_one(i, entry) for i, entry in enumerate(image_entries)]
    # return_exceptions: an image is a best-effort enhancement, never a reason to fail the module.
    # Every real failure mode already returns {"failed": True}; this only catches an UNEXPECTED
    # exception (socket reset, Anthropic error in verify) so one bad image can't abort the build.
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict = {}
    for entry, r in zip(image_entries, raw):
        if isinstance(r, Exception):
            heading = entry.get("slide_heading", "")
            if heading:
                out[heading] = {"path": "", "failed": True, "error": str(r)[:200]}
        else:
            out[r[0]] = r[1]
    return out
