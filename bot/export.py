import io
from pathlib import Path

import img2pdf
from PIL import Image as PILImage


def build_pdf(images: list[Path]) -> bytes:
    """One image per page, page size = image size. img2pdf embeds PNG/JPEG without
    re-compressing, so the PDF keeps the slides' original quality."""
    return img2pdf.convert([_pdf_ready(path) for path in images])


def _pdf_ready(path: Path) -> bytes:
    data = path.read_bytes()
    with PILImage.open(io.BytesIO(data)) as image:
        if image.mode in ("RGB", "L"):
            return data
        # img2pdf refuses images with transparency; slides don't need it.
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()
