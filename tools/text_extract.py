"""Extract plain text from common document formats, for grounding deck content.

Used by the CLI's ``--context-file`` / ``--context-dir`` flags and shared with
Syndara's hosted upload handler so both sides have one implementation.

The heavy parsers (PDF / Word / Excel) are **optional dependencies** — install
them with ``pip install 'syndara-slidegen[context]'``. When one is missing, the
matching file type raises a clear error (or is skipped, for directory walks), so
the core package stays dependency-light. Deliberately framework-agnostic (no web
framework imports) so it can be reused anywhere.
"""
from __future__ import annotations

import io
from pathlib import Path

# Plain-text extensions read directly; binary formats need an optional parser.
TEXT_EXTS = ("txt", "md", "csv", "tsv")
SUPPORTED_EXTS = set(TEXT_EXTS) | {"pdf", "docx", "xlsx"}

_SUPPORTED_LABEL = "PDF, DOCX, TXT, MD, CSV, Excel"


class UnsupportedFileType(ValueError):
    """Raised when a file's extension isn't a supported text source."""


def extract_text(data: bytes, filename: str) -> str:
    """Return the text content of a document from its raw bytes + filename.

    Raises ``UnsupportedFileType`` for unknown extensions, and ``RuntimeError``
    with an install hint if the optional parser for a supported type is missing.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in TEXT_EXTS:
        return data.decode("utf-8", errors="replace")
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("PDF support needs pypdf — install with: pip install 'syndara-slidegen[context]'")
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == "docx":
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("DOCX support needs python-docx — install with: pip install 'syndara-slidegen[context]'")
        doc = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext == "xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise RuntimeError("XLSX support needs openpyxl — install with: pip install 'syndara-slidegen[context]'")
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                sheets.append(f"[Sheet: {name}]\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(sheets)
    raise UnsupportedFileType(f"Unsupported file type: .{ext}. Supported: {_SUPPORTED_LABEL}.")


def extract_text_from_path(path: str | Path) -> str:
    """Read a single file from disk and extract its text."""
    p = Path(path)
    return extract_text(p.read_bytes(), p.name)


def extract_text_from_dir(
    directory: str | Path,
    *,
    max_chars: int = 500_000,
    recursive: bool = False,
    log=lambda _m: None,
) -> str:
    """Concatenate extracted text from every supported file in a directory.

    Unsupported files (and supported files whose optional parser isn't
    installed) are skipped with a logged note rather than failing the whole run.
    Adds files in sorted order until ``max_chars`` is reached, so a huge folder
    can't blow up the context window.
    """
    d = Path(directory)
    if not d.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")
    paths = sorted(d.rglob("*") if recursive else d.glob("*"))
    parts: list[str] = []
    total = 0
    for p in paths:
        if not p.is_file() or p.suffix.lstrip(".").lower() not in SUPPORTED_EXTS:
            continue
        try:
            text = extract_text_from_path(p)
        except Exception as e:  # missing parser, corrupt file, etc. — skip, don't abort
            log(f"  skip {p.name}: {e}")
            continue
        if not text.strip():
            continue
        chunk = f"\n\n----- {p.name} -----\n\n{text}"
        if total + len(chunk) > max_chars:
            log(f"  context size cap ({max_chars} chars) reached — skipping remaining files")
            break
        parts.append(chunk)
        total += len(chunk)
        log(f"  + {p.name} ({len(text)} chars)")
    return "".join(parts).strip()
