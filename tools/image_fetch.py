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

    # Strip all metadata (EXIF, IPTC, XMP) by rebuilding from raw pixel data
    img = Image.frombytes(img.mode, img.size, img.tobytes())

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

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=5,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            ct = resp.headers.get("content-type", "")
            if "html" in ct.lower():
                return _empty_result(error="Server returned HTML instead of an image (possible hotlink protection)")
            if not ct.startswith("image/"):
                return _empty_result(error=f"Unexpected Content-Type: {ct}")

            cl = resp.headers.get("content-length")
            if cl and int(cl) > MAX_FILE_BYTES:
                return _empty_result(error=f"File too large ({cl} bytes, max {MAX_FILE_BYTES})")
            if len(resp.content) > MAX_FILE_BYTES:
                return _empty_result(error=f"File too large ({len(resp.content)} bytes, max {MAX_FILE_BYTES})")

            return _process_image(resp.content, out_path)

    except Exception as e:
        log.warning("fetch_web_image failed for %s: %s", url, e)
        return _empty_result(error=str(e)[:500])


async def search_and_fetch_image(query: str, out_path: str) -> dict:
    import anthropic

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3, "allowed_callers": ["direct"]}],
            messages=[{
                "role": "user",
                "content": (
                    f"Find a single high-quality image URL for: {query}\n"
                    "Return ONLY the direct image URL (ending in .jpg, .png, .webp, etc). "
                    "No explanation needed."
                ),
            }],
        )
    except Exception as e:
        return _empty_result(error=f"Search API error: {e}")
    try:
        from ..agents.base import report_usage
        report_usage("image_search", "claude-haiku-4-5-20251001", resp.usage)
    except Exception:
        pass

    url = None
    for block in resp.content:
        if getattr(block, "text", None):
            match = re.search(r'https?://[^\s<>"]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s<>"]*)?', block.text, re.I)
            if match:
                url = match.group(0).rstrip(".,;:)]}\"'")
                break

    if not url:
        return _empty_result(error="No image found for query")

    return await fetch_web_image(url, out_path)


async def download_plan_images(image_entries: list[dict], images_dir: str) -> dict:
    Path(images_dir).mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    domain_last_request: dict[str, float] = {}
    domain_lock = asyncio.Lock()

    async def _rate_limit(url: str):
        domain = urlparse(url).netloc
        if not domain:
            return
        async with domain_lock:
            import time
            now = time.monotonic()
            last = domain_last_request.get(domain, 0.0)
            wait = 0.5 - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            domain_last_request[domain] = time.monotonic()

    async def _download_one(idx: int, entry: dict) -> tuple[str, dict]:
        heading = entry.get("slide_heading", f"slide_{idx}")
        ext = ".png"
        filename = f"web_img_{idx + 1:02d}{ext}"
        out_path = str(Path(images_dir) / filename)

        async with sem:
            result = None
            image_url = entry.get("image_url", "")
            if image_url:
                await _rate_limit(image_url)
                result = await fetch_web_image(image_url, out_path)

            if (not result or not result["success"]) and entry.get("search_query"):
                result = await search_and_fetch_image(entry["search_query"], out_path)

            if not result or not result["success"]:
                return heading, {
                    "path": "", "width_px": 0, "height_px": 0, "aspect": 0.0,
                    "attribution": entry.get("attribution", ""),
                    "failed": True,
                    "error": (result or {}).get("error", "No URL or search query provided"),
                }

            return heading, {
                "path": result["path"],
                "width_px": result["width_px"],
                "height_px": result["height_px"],
                "aspect": result["aspect"],
                "attribution": entry.get("attribution", ""),
                "failed": False, "error": "",
            }

    tasks = [_download_one(i, entry) for i, entry in enumerate(image_entries)]
    results = await asyncio.gather(*tasks)
    return dict(results)
