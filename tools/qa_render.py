"""Batch PPTX-to-JPEG renderer for visual QA."""
from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def render_all_slides(pptx_path: str, out_dir: Optional[str] = None, dpi: int = 150) -> list[str]:
    """Render every slide in a PPTX to JPEG files.

    Returns list of JPEG file paths in slide order.
    When out_dir is None a temporary directory is created — the caller is
    responsible for cleaning it up (see cleanup_dir()).
    Raises RuntimeError if rendering fails.
    """
    out_dir = out_dir or tempfile.mkdtemp(prefix="syndara_qa_")
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Step 1: PPTX → PDF via LibreOffice
    from .render_tool import LIBREOFFICE_CANDIDATES
    pdf_path = None
    for lo in LIBREOFFICE_CANDIDATES:
        try:
            result = subprocess.run(
                [lo, "--headless", "--convert-to", "pdf", "--outdir", str(out_path), pptx_path],
                capture_output=True, timeout=120,
            )
            if result.returncode == 0:
                pdfs = list(out_path.glob("*.pdf"))
                if pdfs:
                    pdf_path = pdfs[0]
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if not pdf_path:
        raise RuntimeError("Could not convert PPTX to PDF — LibreOffice not found or failed")

    # Step 2: PDF → JPEGs via pdftoppm
    prefix = str(out_path / "slide")
    try:
        result = subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), str(pdf_path), prefix],
            capture_output=True, timeout=120,
        )
    except FileNotFoundError as e:
        raise RuntimeError("pdftoppm not found — cannot rasterize slides for QA") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("pdftoppm timed out rasterizing slides for QA") from e
    if result.returncode != 0:
        raise RuntimeError(f"pdftoppm failed: {result.stderr.decode()[:500]}")

    # Collect and sort output files
    jpegs = sorted(out_path.glob("slide-*.jpg"))
    if not jpegs:
        raise RuntimeError("pdftoppm produced no output files")

    return [str(j) for j in jpegs]
