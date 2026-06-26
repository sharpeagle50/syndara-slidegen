"""
Rendering utilities used by the Claude Code slide builder's MCP tools.
Keeps the heavy imports (PIL, subprocess, matplotlib) out of the hot path
until they're actually needed.
"""
from __future__ import annotations
import base64
import subprocess
import tempfile
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


def render_slide_png(pptx_path: str, slide_index: int, out_dir: Optional[str] = None) -> bytes:
    """Render one slide of a .pptx to PNG bytes. Tries LibreOffice first.

    Raises RuntimeError if rendering fails.
    """
    _auto_dir = None
    if out_dir is None:
        _auto_dir = tempfile.mkdtemp(prefix="syndara_render_")
        out_dir = _auto_dir
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        # Try LibreOffice — produces a PDF or PNGs
        for lo in LIBREOFFICE_CANDIDATES:
            try:
                pdf_out = subprocess.run(
                    [lo, "--headless", "--convert-to", "pdf", "--outdir", str(out_path), pptx_path],
                    capture_output=True, timeout=120,
                )
                if pdf_out.returncode != 0:
                    continue
                pdfs = list(out_path.glob("*.pdf"))
                if not pdfs:
                    continue
                pdf = pdfs[0]
                png_base = str(out_path / "slide")
                # -singlefile writes exactly "{png_base}.png" with no page-number suffix.
                # Without it, pdftoppm zero-pads the page number to the deck's digit width
                # (page 1 of a 26-slide deck -> "slide-01.png"), and a glob like
                # "slide-1*.png" would miss it -> render silently fails for slides 1-9 of
                # any deck with >=10 slides.
                ppm = subprocess.run(
                    ["pdftoppm", "-png", "-r", "120", "-singlefile",
                     "-f", str(slide_index + 1), "-l", str(slide_index + 1),
                     str(pdf), png_base],
                    capture_output=True, timeout=60,
                )
                if ppm.returncode == 0:
                    single = out_path / "slide.png"
                    if single.exists():
                        return single.read_bytes()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        raise RuntimeError(
            "Could not render slide PNG — LibreOffice (soffice) not found or "
            "conversion failed. Slide index may also be out of range."
        )
    finally:
        if _auto_dir:
            import shutil
            shutil.rmtree(_auto_dir, ignore_errors=True)


def render_slide_png_b64(pptx_path: str, slide_index: int) -> str:
    """Same as render_slide_png but returns base64 string for MCP tool results."""
    return base64.standard_b64encode(render_slide_png(pptx_path, slide_index)).decode()


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
