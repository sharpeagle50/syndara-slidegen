"""Download and validate web images for insertion into PPTX slides."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 15 * 1024 * 1024
MIN_DIM = 400
WARN_DIM = 600
MAX_DIM = 4000
MAX_CONCURRENT = 5
USER_AGENT = "Syndara/1.0 (educational content platform)"


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

    if img_format in ("WEBP", "GIF"):
        img = img.convert("RGBA")
        img_format = "PNG"

    # Metadata (EXIF/IPTC/XMP) is dropped naturally: the save() calls below never pass
    # exif=/icc_profile=, so PIL writes pixels only. (The old frombytes() round-trip here
    # silently rebuilt palette PNGs with an empty palette, corrupting their colors.)

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
    }


async def fetch_web_image(url: str, out_path: str, timeout: float = 20.0) -> dict:
    import httpx

    # Retry a couple times on 429/503 (e.g. Wikimedia throttling us) with a short backoff
    # that honors Retry-After — these are valid images we simply requested too fast. The
    # Accept header nudges servers that content-negotiate to send the image, not an HTML page.
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/png,image/*,*/*"}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, max_redirects=5, headers=headers,
            ) as client:
                resp = await client.get(url)

            if resp.status_code in (429, 503) and attempt < 2:
                ra = resp.headers.get("retry-after", "")
                try:
                    delay = float(ra)
                except ValueError:
                    delay = 1.5 * (attempt + 1)
                await asyncio.sleep(min(delay, 6.0))
                continue

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


# Image search uses Sonnet 4.6, not Haiku: the web_search_20260209 dynamic-filtering tool
# requires Opus 4.6+ / Sonnet 4.6, and a stronger model picks better image candidates (the
# vision-verify step below rejects bad picks, so better candidates mean fewer rejections).
# Cost is negligible here — search is a per-image fallback and the output is just a URL.
SEARCH_MODEL = "claude-sonnet-4-6"


async def search_and_fetch_image(query: str, out_path: str) -> dict:
    import anthropic

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=SEARCH_MODEL,
            max_tokens=1024,
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
_IMG_SRC_RE = re.compile(r'<img\b[^>]*?\b(?:data-src|src)=["\']([^"\']+)["\']', re.I)
_SRCSET_RE = re.compile(r'\bsrcset=["\']([^"\']+)["\']', re.I)

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
        inner = (parse_qs(sp.query).get("url") or parse_qs(sp.query).get("u") or [""])[0]
        inner = unquote(inner) if inner else ""
        if inner.startswith(("http://", "https://")):
            return inner
    except Exception:
        pass
    return u


def _extract_page_image_urls(html: str, base_url: str, limit: int = 8) -> list[str]:
    """Pull real candidate image URLs from a page's raw HTML (og:image, srcset, <img>).
    Resolves relative URLs; skips data:/svg/obvious icons. Most-representative first."""
    from html import unescape as _unescape
    from urllib.parse import urljoin
    out: list[str] = []
    seen: set = set()

    def add(u: str):
        u = (u or "").strip()
        if not u or u.startswith("data:"):
            return
        # HTML attribute values encode `&` as `&amp;`; decode before use or the query string
        # is malformed (e.g. `?url=...&amp;w=1920` 400s). Then unwrap image-optimizer proxies.
        u = _unwrap_optimizer_url(_unescape(u))
        full = urljoin(base_url, u)
        low = full.lower()
        if not full.startswith(("http://", "https://")):
            return
        if low.endswith(".svg") or "sprite" in low or "/icon" in low or "logo" in low:
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
    for m in _IMG_SRC_RE.findall(html):
        add(m)
    return out[:limit]


async def _fetch_page_html(url: str, timeout: float = 15.0) -> str:
    import httpx
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
    client = anthropic.AsyncAnthropic()
    urls: list[str] = []
    try:
        resp = await client.messages.create(
            model=SEARCH_MODEL, max_tokens=512,
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
            tried += 1
            res = await fetch_web_image(img_url, out_path)
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


VISION_MODEL = "claude-sonnet-4-6"


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
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=VISION_MODEL,
            max_tokens=400,
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
            return {
                "matches": bool(d.get("matches", True)),
                "caption": str(d.get("caption", "")).strip(),
                "reason": str(d.get("reason", "")).strip(),
            }
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

    domain_last_request: dict[str, float] = {}
    domain_lock = asyncio.Lock()

    async def _rate_limit(url: str):
        domain = urlparse(url).netloc
        if not domain:
            return
        import time
        async with domain_lock:
            # Reserve this domain's next slot while holding the lock, then sleep WITHOUT the
            # lock — otherwise a 0.5s throttle on one domain serializes requests to every
            # other domain too, defeating the MAX_CONCURRENT parallelism.
            fire_at = max(time.monotonic(), domain_last_request.get(domain, 0.0) + 0.5)
            domain_last_request[domain] = fire_at
        wait = fire_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

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
                await _rate_limit(image_url)
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
                "caption": caption,
                "failed": False, "error": "",
            }

    tasks = [_download_one(i, entry) for i, entry in enumerate(image_entries)]
    results = await asyncio.gather(*tasks)
    return dict(results)
