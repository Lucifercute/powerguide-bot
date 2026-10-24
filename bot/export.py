import io
from pathlib import Path

import img2pdf
from PIL import Image as PILImage


PAGE_WIDTH_PT = 960  # 13.33 inches, the width of a standard 16:9 slide


def build_pdf(images: list[Path]) -> bytes:
    """One image per page. img2pdf embeds PNG/JPEG without re-compressing, so the PDF keeps
    the slides' original quality. All pages get the same size: slides with the user's picture
    are twice as large in pixels as the others, and would otherwise make bigger pages."""
    with PILImage.open(images[0]) as first:
        height = PAGE_WIDTH_PT * first.height / first.width
    layout = img2pdf.get_layout_fun((img2pdf.in_to_pt(PAGE_WIDTH_PT / 72), img2pdf.in_to_pt(height / 72)))
    return img2pdf.convert([_pdf_ready(path) for path in images], layout_fun=layout)


def _pdf_ready(path: Path) -> bytes:
    data = path.read_bytes()
    with PILImage.open(io.BytesIO(data)) as image:
        if image.mode in ("RGB", "L"):
            return data
        # img2pdf refuses images with transparency; slides don't need it.
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()
