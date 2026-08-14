"""
Rendering utilities used by the Claude Code slide builder's MCP tools.
Keeps the heavy imports (PIL, subprocess, matplotlib) out of the hot path
until they're actually needed.
"""
from __future__ import annotations
import base64
import os
import shutil
import subprocess
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Optional


LIBREOFFICE_CANDIDATES = [
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/libreoffice",
    "/usr/bin/soffice",
]


# ── Deck→PDF cache ────────────────────────────────────────────────────────────
# render_slide_png used to convert the ENTIRE deck to PDF via LibreOffice (~seconds
# to 120s) on EVERY call, so a build that previews N slides re-converted the whole
# deck N times. Convert once per deck VERSION (keyed by file mtime+size) and extract
# individual pages from the cached PDF. A rebuild changes mtime/size and transparently
# invalidates the entry — same output, one conversion instead of N.
_pdf_cache: "dict[str, tuple[tuple, str]]" = {}   # pptx_path -> ((mtime_ns, size), pdf_path)
_pdf_cache_lock = threading.Lock()
_PDF_CACHE_MAX = 6


def _deck_version(pptx_path: str) -> tuple:
    st = os.stat(pptx_path)
    return (st.st_mtime_ns, st.st_size)


def libreoffice_convert_pdf(pptx_path: str, candidates, out_dir: str) -> Optional[str]:
    """Convert a deck to PDF with LibreOffice, safe under concurrency and sized to the deck.

    Two fixes over the old inline call: (1) a PRIVATE per-invocation user profile
    (-env:UserInstallation), so up to 12 modules rendering at once don't corrupt each other by
    sharing the default profile (the classic concurrent-headless failure, which fell back to
    unreadable placeholder PNGs); (2) a timeout that scales with the deck (a 60-slide image-dense
    deck legitimately needs more than the old flat 120s, and a too-short timeout burned builder
    turns). Returns the PDF path or None."""
    import uuid as _uuid
    import zipfile as _zf
    try:
        with _zf.ZipFile(pptx_path) as _z:
            n = max(1, len(_z.namelist()))  # cheap proxy for deck size
    except Exception:
        n = 40
    timeout = min(600, max(180, int(n * 5)))
    for lo in candidates:
        profile = f"file://{tempfile.mkdtemp(prefix='lo_profile_')}_{_uuid.uuid4().hex[:8]}"
        try:
            r = subprocess.run(
                [lo, "-env:UserInstallation=" + profile, "--headless",
                 "--convert-to", "pdf", "--outdir", out_dir, pptx_path],
                capture_output=True, timeout=timeout,
            )
            if r.returncode != 0:
                continue
            pdfs = list(Path(out_dir).glob("*.pdf"))
            if pdfs:
                return str(pdfs[0])
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            print(f"[libreoffice] {lo} timed out after {timeout}s on {pptx_path}", flush=True)
            continue
    return None


def _convert_deck_to_pdf(pptx_path: str) -> Optional[str]:
    """Run LibreOffice once to convert the whole deck to a PDF; return its path (or None)."""
    pdf_dir = tempfile.mkdtemp(prefix="syndara_deckpdf_")
    out = libreoffice_convert_pdf(pptx_path, LIBREOFFICE_CANDIDATES, pdf_dir)
    if not out:
        shutil.rmtree(pdf_dir, ignore_errors=True)
    return out


def _get_deck_pdf(pptx_path: str) -> Optional[str]:
    """Get-or-build the deck's PDF, cached by (mtime, size). Thread-safe; the lock also
    coalesces concurrent renders of the same deck into a single conversion."""
    try:
        version = _deck_version(pptx_path)
    except OSError:
        return None
    with _pdf_cache_lock:
        cached = _pdf_cache.get(pptx_path)
        if cached and cached[0] == version and Path(cached[1]).exists():
            return cached[1]
        pdf = _convert_deck_to_pdf(pptx_path)
        if not pdf:
            return None
        if cached:  # drop the stale PDF for this same deck
            shutil.rmtree(Path(cached[1]).parent, ignore_errors=True)
        _pdf_cache[pptx_path] = (version, pdf)
        while len(_pdf_cache) > _PDF_CACHE_MAX:   # bound memory: evict oldest decks
            old_key = next(iter(_pdf_cache))
            _, old_pdf = _pdf_cache.pop(old_key)
            shutil.rmtree(Path(old_pdf).parent, ignore_errors=True)
        return pdf


def invalidate_deck_pdf(pptx_path: Optional[str] = None) -> None:
    """Manually drop cached deck PDF(s). (mtime/size already auto-invalidates on rebuild;
    this is an explicit hook for callers that mutate a deck in place.)"""
    with _pdf_cache_lock:
        keys = [pptx_path] if pptx_path else list(_pdf_cache)
        for k in keys:
            entry = _pdf_cache.pop(k, None)
            if entry:
                shutil.rmtree(Path(entry[1]).parent, ignore_errors=True)


def render_slide_png(pptx_path: str, slide_index: int, out_dir: Optional[str] = None) -> bytes:
    """Render one slide of a .pptx to PNG bytes via a cached deck→PDF conversion.

    Raises RuntimeError if rendering fails.
    """
    pdf = _get_deck_pdf(pptx_path)
    if not pdf:
        raise RuntimeError(
            "Could not render slide PNG — LibreOffice (soffice) not found or conversion failed."
        )

    _auto_dir = None
    if out_dir is None:
        _auto_dir = tempfile.mkdtemp(prefix="syndara_render_")
        out_dir = _auto_dir
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        png_base = str(out_path / "slide")
        # -singlefile writes exactly "{png_base}.png" with no page-number suffix.
        # Without it, pdftoppm zero-pads the page number to the deck's digit width
        # (page 1 of a 26-slide deck -> "slide-01.png"), and a glob like "slide-1*.png"
        # would miss it -> render silently fails for slides 1-9 of any deck with >=10 slides.
        ppm = subprocess.run(
            ["pdftoppm", "-png", "-r", "120", "-singlefile",
             "-f", str(slide_index + 1), "-l", str(slide_index + 1),
             pdf, png_base],
            capture_output=True, timeout=60,
        )
        single = out_path / "slide.png"
        if ppm.returncode == 0 and single.exists():
            return single.read_bytes()
        raise RuntimeError(
            f"Could not render slide {slide_index + 1} (pdftoppm exit {ppm.returncode}); "
            "slide index may be out of range."
        )
    finally:
        if _auto_dir:
            shutil.rmtree(_auto_dir, ignore_errors=True)


_MAX_TOOL_PNG_BYTES = 600_000   # keep base64 tool results far below the agent SDK's message buffer


def render_slide_png_b64(pptx_path: str, slide_index: int) -> str:
    """Same as render_slide_png but returns base64 string for MCP tool results.

    Downscales a dense slide's PNG until it fits _MAX_TOOL_PNG_BYTES: one oversized preview,
    base64'd inside a single JSON message, crashed an entire module build against the SDK's
    buffer limit (job 203 m5). The builder only needs a legible preview, not print resolution,
    and smaller previews also cost fewer vision tokens."""
    raw = render_slide_png(pptx_path, slide_index)
    if len(raw) > _MAX_TOOL_PNG_BYTES:
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            while len(raw) > _MAX_TOOL_PNG_BYTES and min(img.size) > 480:
                img = img.resize((int(img.width * 0.75), int(img.height * 0.75)), resample)
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                raw = buf.getvalue()
        except Exception:
            pass   # worst case ship the original; the raised SDK buffer still absorbs it
    return base64.standard_b64encode(raw).decode()


def render_matplotlib_chart(code: str, out_path: str) -> dict:
    """Execute matplotlib code that saves a figure to out_path.
    Returns {success, error?}."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    out_path = str(Path(out_path).resolve())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    namespace = {
        "plt": plt, "np": np, "output_path": out_path,
        "__builtins__": __builtins__,
    }
    # Auto-save if the code didn't
    if "savefig" not in code:
        code += "\nplt.savefig(output_path, dpi=150, bbox_inches='tight')"
    if "plt.close" not in code:
        code += "\nplt.close('all')"
    try:
        exec(compile(code, "<matplotlib>", "exec"), namespace)
        if Path(out_path).exists():
            return {"success": True, "path": out_path}
        return {"success": False, "error": "Code ran but no file was written."}
    except Exception:
        return {"success": False, "error": traceback.format_exc()[-1500:]}


def render_equation(latex: str, out_path: str, color: str = "#1A1D2E",
                    fontsize: int = 44, serif: bool = False) -> dict:
    """Typeset a math expression to a tightly-cropped TRANSPARENT PNG via matplotlib
    mathtext (no LaTeX install needed — mathtext covers fractions, integrals, sums,
    roots, Greek, sub/superscripts, operators). `serif` picks the STIX (Times-like)
    math fontset so serif-styled decks get serif math; default is the DejaVu sans set,
    which matches sans decks and the existing make_chart output. Returns {success,
    path, width_px, height_px, aspect} so the caller can size the image without
    distortion, or {success: False, error} on a mathtext syntax error."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_path = str(Path(out_path).resolve())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    eq = (latex or "").strip()
    if eq.startswith("$$") and eq.endswith("$$"):
        eq = eq[1:-1]
    if not (eq.startswith("$") and eq.endswith("$")):
        eq = f"${eq}$"
    fig = plt.figure(figsize=(0.1, 0.1))
    try:
        with matplotlib.rc_context({"mathtext.fontset": "stix" if serif else "dejavusans"}):
            fig.text(0, 0, eq, fontsize=fontsize, color=color)
            # bbox_inches="tight" crops to the rendered expression; transparent background so
            # the equation sits on any slide fill. Mathtext syntax errors raise at draw time.
            fig.savefig(out_path, transparent=True, bbox_inches="tight",
                        pad_inches=0.04, dpi=200)
    except Exception:
        return {"success": False, "error": traceback.format_exc()[-1500:]}
    finally:
        plt.close(fig)
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            w, h = im.size
        return {"success": True, "path": out_path, "width_px": w, "height_px": h,
                "aspect": round(w / max(h, 1), 3)}
    except Exception:
        return {"success": True, "path": out_path}


def render_graphviz_flowchart(dot_source: str, out_path: str) -> dict:
    """Render a Graphviz DOT string to PNG. Returns {success, error?}."""
    try:
        import graphviz
    except ImportError:
        return {"success": False, "error": "graphviz Python package not installed"}
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        g = graphviz.Source(dot_source, format="png")
        # graphviz.Source.render writes to {out_path}.png if out_path has no ext
        # We want exact out_path, so render to temp then move
        with tempfile.TemporaryDirectory() as td:
            rendered = g.render(directory=td, cleanup=True)
            Path(rendered).replace(out_path)
        return {"success": True, "path": str(out_path)}
    except Exception:
        return {"success": False, "error": traceback.format_exc()[-1500:]}


def _svg_to_png(svg_path: str, png_path: str) -> bool:
    """Convert SVG to PNG with transparent background. Returns True on success."""
    try:
        from cairosvg import svg2png
        svg2png(url=svg_path, write_to=png_path, scale=2)
        return True
    except Exception:
        pass
    for cmd in ["rsvg-convert", "resvg"]:
        try:
            args = ([cmd, "-o", png_path, svg_path] if cmd == "rsvg-convert"
                    else [cmd, svg_path, png_path])
            r = subprocess.run(args, capture_output=True, timeout=30)
            if r.returncode == 0 and Path(png_path).exists():
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def render_d2_diagram(d2_source: str, out_path: str, layout: str = "dagre") -> dict:
    """Render a D2 source string to PNG via the `d2` binary.

    Tries SVG → PNG conversion first for transparent backgrounds. Falls
    back to direct PNG rendering (white background) if no SVG converter
    is available.

    layout: 'dagre' (default, grid-like flowcharts), 'elk' (hierarchical),
    or 'tala' (best for architecture — paid layout engine, falls back to
    dagre if unavailable)."""
    out_path = str(Path(out_path).resolve())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".d2", delete=False) as f:
        f.write(d2_source)
        src_path = f.name
    svg_path = out_path.rsplit(".", 1)[0] + ".svg" if "." in out_path else out_path + ".svg"
    try:
        # Try SVG path first for transparent background
        result = subprocess.run(
            ["d2", "--layout", layout, src_path, svg_path],
            capture_output=True, timeout=60, text=True,
        )
        if result.returncode == 0 and Path(svg_path).exists():
            if _svg_to_png(svg_path, out_path):
                Path(svg_path).unlink(missing_ok=True)
                return {"success": True, "path": out_path}
            Path(svg_path).unlink(missing_ok=True)

        # Fallback: render directly to PNG (white background)
        result = subprocess.run(
            ["d2", "--layout", layout, src_path, out_path],
            capture_output=True, timeout=60, text=True,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"d2 exit {result.returncode}: {result.stderr[-1500:]}"}
        if not Path(out_path).exists():
            return {"success": False, "error": "d2 ran but produced no output file"}
        return {"success": True, "path": out_path}
    except FileNotFoundError:
        return {"success": False, "error": "`d2` binary not found on PATH — install github.com/terrastruct/d2"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "d2 render timed out after 60s"}
    except Exception:
        return {"success": False, "error": traceback.format_exc()[-1500:]}
    finally:
        try:
            Path(src_path).unlink()
        except Exception:
            pass
        try:
            Path(svg_path).unlink(missing_ok=True)
        except Exception:
            pass


def render_mermaid_diagram(mermaid_source: str, out_path: str) -> dict:
    """Render a Mermaid source string to PNG via the mmdc CLI."""
    out_path = str(Path(out_path).resolve())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False) as f:
        f.write(mermaid_source)
        src_path = f.name
    try:
        result = subprocess.run(
            ["mmdc", "-i", src_path, "-o", out_path, "-b", "transparent", "-s", "2"],
            capture_output=True, timeout=90, text=True,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"mmdc exit {result.returncode}: {result.stderr[-1500:]}"}
        if not Path(out_path).exists():
            return {"success": False, "error": "mmdc ran but produced no output file"}
        return {"success": True, "path": out_path}
    except FileNotFoundError:
        return {"success": False, "error": "`mmdc` not found on PATH — install @mermaid-js/mermaid-cli"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "mermaid render timed out after 90s"}
    except Exception:
        return {"success": False, "error": traceback.format_exc()[-1500:]}
    finally:
        try:
            Path(src_path).unlink()
        except Exception:
            pass


def image_metadata(path: str) -> dict:
    """Return width/height in pixels for a rendered PNG."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"width_px": im.width, "height_px": im.height,
                    "aspect": round(im.width / max(im.height, 1), 3)}
    except Exception:
        return {"width_px": 0, "height_px": 0, "aspect": 1.0}


def png_to_base64(path: str) -> str:
    return base64.standard_b64encode(Path(path).read_bytes()).decode()
