"""Customer's source files for step 02: which formats are accepted and how they reach the model.

PDF and images go to Gemini as they are (it reads text, tables and charts itself).
Word and PowerPoint files are converted to PDF with LibreOffice when it is installed,
so the model sees their pictures and charts too. DOCX, XLSX, PPTX, TXT and CSV are
not supported by Gemini directly, so otherwise their text is extracted here once,
when the file is received, and sent as plain text.
"""

import asyncio
import datetime
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import openpyxl
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pptx import Presentation

# Extension → MIME type of files the model reads itself.
BINARY_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
TEXT_TYPES = {".docx", ".xlsx", ".pptx", ".txt", ".csv"}
OLD_FORMATS = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}
# Converted to PDF when LibreOffice is available. Not Excel: wide sheets get cut into
# unreadable pages, and the model understands cell values better as text.
PDF_CONVERTIBLE = {".docx", ".pptx", ".doc", ".ppt"}
CONVERT_TIMEOUT_SECONDS = 90
# First bytes of old binary Office files (.doc, .ppt): the OLE2 container.
_OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")

MAX_TEXT_CHARS = 150_000  # per file, roughly 40k tokens
MAX_SHEET_ROWS = 2_000


@dataclass(frozen=True)
class SourceFile:
    name: str  # original file name: shown to the user, and the model cites it
    path: Path
    mime_type: str
    text: str | None  # extracted text; None = the model reads the file itself


class UnreadableFile(Exception):
    pass


def extract_text(path: Path) -> str:
    """Text of a DOCX/XLSX/PPTX/TXT/CSV file, cut to MAX_TEXT_CHARS. Raises UnreadableFile."""
    readers = {".docx": _docx, ".xlsx": _xlsx, ".pptx": _pptx, ".txt": _plain, ".csv": _plain}
    try:
        text = readers[path.suffix.lower()](path).strip()
    except UnreadableFile:
        raise
    except Exception as e:  # corrupted or mislabeled files fail in many library-specific ways
        raise UnreadableFile(str(e)) from e
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n\n[Файл обрезан: показаны первые {MAX_TEXT_CHARS} символов.]"
    return text


async def convert_to_pdf(soffice: str, path: Path) -> Path:
    """Convert a Word/PowerPoint file to PDF next to it with LibreOffice; return the PDF
    path. Raises UnreadableFile."""
    # LibreOffice opens anything it can't parse as plain text, so a broken or mislabeled
    # file would become a PDF of garbage: check that the file is what its name says.
    if not _looks_like_office_file(path):
        raise UnreadableFile("not a Word/PowerPoint file")
    pdf = path.with_suffix(".pdf")
    pdf.unlink(missing_ok=True)
    # A separate profile per call: conversions don't clash with each other or with
    # LibreOffice opened by hand on the same machine.
    with tempfile.TemporaryDirectory(prefix="soffice-") as profile:
        process = await asyncio.create_subprocess_exec(
            soffice,
            "--headless",
            "--norestore",
            f"-env:UserInstallation={Path(profile).as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(path.parent),
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), CONVERT_TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            pdf.unlink(missing_ok=True)
            raise UnreadableFile(f"conversion took longer than {CONVERT_TIMEOUT_SECONDS}s") from None
    # LibreOffice may exit with 0 without writing anything, so the file itself is checked.
    if process.returncode != 0 or not pdf.exists() or pdf.stat().st_size == 0:
        pdf.unlink(missing_ok=True)
        details = stderr.decode(errors="replace").strip()[:300]
        raise UnreadableFile(f"conversion failed (exit code {process.returncode}): {details}")
    return pdf


def _looks_like_office_file(path: Path) -> bool:
    if path.suffix.lower() in (".doc", ".ppt"):
        with path.open("rb") as f:
            return f.read(len(_OLE_SIGNATURE)) == _OLE_SIGNATURE
    try:
        with zipfile.ZipFile(path) as archive:
            return "[Content_Types].xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _plain(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnreadableFile("unknown text encoding")


def _docx(path: Path) -> str:
    """Paragraphs and tables in document order; headings marked with #."""
    document = Document(str(path))
    parts = []
    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = paragraph.style.name if paragraph.style is not None else ""
            parts.append(f"# {text}" if style.startswith(("Heading", "Title", "Заголовок")) else text)
        elif tag == "tbl":
            rows = _table_rows([[cell.text for cell in row.cells] for row in Table(element, document).rows])
            if rows:
                parts.append("\n".join(rows))
    return "\n\n".join(parts)


def _xlsx(path: Path) -> str:
    """Every sheet as a table of cell values (results of formulas, not the formulas)."""
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for sheet in workbook.worksheets:
            rows = []
            for values in sheet.iter_rows(values_only=True):
                cells = [_cell(v) for v in values]
                if not any(cells):
                    continue
                if len(rows) == MAX_SHEET_ROWS:
                    rows.append(f"[Лист обрезан: показаны первые {MAX_SHEET_ROWS} строк.]")
                    break
                rows.append(" | ".join(cells))
            if rows:
                sheets.append(f"Лист «{sheet.title}»:\n" + "\n".join(rows))
        return "\n\n".join(sheets)
    finally:
        workbook.close()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(round(value, 10))
    if isinstance(value, datetime.datetime) and value.time() == datetime.time():
        return value.date().isoformat()
    return str(value).strip()


def _pptx(path: Path) -> str:
    """Text of every slide (text boxes, tables, groups) plus speaker notes."""
    presentation = Presentation(str(path))
    slides = []
    for number, slide in enumerate(presentation.slides, start=1):
        lines = [line for shape in slide.shapes for line in _shape_lines(shape)]
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Заметки: {notes}")
        if lines:
            slides.append(f"Слайд {number}:\n" + "\n".join(lines))
    return "\n\n".join(slides)


def _shape_lines(shape) -> list[str]:
    if shape.shape_type is not None and hasattr(shape, "shapes"):  # group shape
        return [line for child in shape.shapes for line in _shape_lines(child)]
    if getattr(shape, "has_table", False) and shape.has_table:
        return _table_rows([[cell.text for cell in row.cells] for row in shape.table.rows])
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        text = shape.text_frame.text.strip()
        return [text] if text else []
    return []


def _table_rows(rows: list[list[str]]) -> list[str]:
    """Table rows as "a | b | c" lines, without fully empty rows."""
    cleaned = [[cell.strip() for cell in row] for row in rows]
    return [" | ".join(row) for row in cleaned if any(row)]
