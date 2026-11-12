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


def build_editable_pptx(plan: str, ratio: str, pictures: dict | None = None, slide_numbers: list[int] | None = None) -> bytes:
    """A separate, simple deck: native text and original pictures, never raster text.

    Keeps the approved wording; complex charts/schemes remain textual descriptions.
    The AI-rendered PDF is still the designed presentation.
    """
    import re
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from bot.plan import plan_slides

    presentation = Presentation()
    width, height = map(int, ratio.split(':'))
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(13.333 * height / width)
    slide_width = presentation.slide_width / 914400
    slide_height = presentation.slide_height / 914400
    margin = min(0.5, slide_height / 12)
    pictures = pictures or {}
    for number, block in plan_slides(plan).items():
        if slide_numbers is not None and number not in slide_numbers:
            continue
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)
        fields = list(re.finditer(r'^[ \t*_]*(ЗАГОЛОВОК|ТИП|СОДЕРЖАНИЕ)[ \t*_]*:[ \t*_]*(.*)$', block, re.M | re.I))
        title = next((m.group(2) for m in fields if m.group(1).upper() == 'ЗАГОЛОВОК'), f'Слайд {number}')
        content_field = next((m for m in fields if m.group(1).upper() == 'СОДЕРЖАНИЕ'), None)
        content = block[content_field.start(2):].strip() if content_field else block
        title_height = min(1.2, slide_height * 0.2)
        title_box = slide.shapes.add_textbox(Inches(margin), Inches(margin), Inches(slide_width-2*margin), Inches(title_height))
        title_box.text_frame.word_wrap = True
        title_box.text_frame.text = title
        for paragraph in title_box.text_frame.paragraphs:
            paragraph.font.size = Pt(26)
            paragraph.font.bold = True
        y = margin + title_height + 0.15
        body_height = max(0.5, slide_height-y-margin)
        picture = pictures.get(number)
        body_width = (slide_width-3*margin)/2 if picture else slide_width-2*margin
        body = slide.shapes.add_textbox(Inches(margin), Inches(y), Inches(body_width), Inches(body_height))
        frame = body.text_frame
        frame.word_wrap = True
        frame.text = content
        # Ask PowerPoint to reduce overflowing text while keeping every word editable.
        from pptx.enum.text import MSO_AUTO_SIZE
        frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        size = min(20, max(10, int(22 - len(content)/250)))
        for paragraph in frame.paragraphs:
            paragraph.font.size = Pt(size)
        if picture:
            available_width = body_width
            available_height = max(0.3, body_height-(0.5 if picture.caption else 0))
            scale = min(available_width/picture.width, available_height/picture.height)
            w, h = picture.width*scale, picture.height*scale
            x = 2*margin+body_width+(available_width-w)/2
            slide.shapes.add_picture(picture.path, Inches(x), Inches(y), width=Inches(w), height=Inches(h))
            if picture.caption:
                caption = slide.shapes.add_textbox(Inches(2*margin+body_width), Inches(y+h), Inches(available_width), Inches(0.5))
                caption.text_frame.text = picture.caption
                for paragraph in caption.text_frame.paragraphs:
                    paragraph.font.size = Pt(12)
    result = io.BytesIO()
    presentation.save(result)
    return result.getvalue()
