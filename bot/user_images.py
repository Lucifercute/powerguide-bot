"""The user's own pictures on slides.

The image model can't put a picture on a slide unchanged: it redraws it, and small text,
numbers and faces come out different. So the model leaves a solid area of a marker color
in the picture's proportions, and the picture is pasted into that area here, pixel for pixel.
"""

import io
import json
import logging
import re
from dataclasses import asdict, dataclass

from PIL import Image as PILImage
from PIL import ImageChops, ImageDraw, ImageFilter

log = logging.getLogger(__name__)

MAX_CAPTION_CHARS = 100
# Slides with a picture are assembled at twice the model's resolution, so the picture
# keeps its own sharpness: at 1376×768 a floor plan or a screenshot becomes unreadable.
SCALE = 2
GREEN = (0, 255, 0)
MAGENTA = (255, 0, 255)
_HEX = re.compile(r"#([0-9A-Fa-f]{6})\b")
_TEXT_PRIMARY = re.compile(r"text_primary\W{0,20}#([0-9A-Fa-f]{6})", re.IGNORECASE)
MIN_FILL = 0.9  # share of marker pixels inside the found box: below it the area is not solid
MIN_SIDE = 60  # px: anything smaller is a stray accent, not the area for the picture
MAX_CROP = 0.15  # a picture is trimmed to fill the area only if this share or less is cut off
PAD = 3  # px around the area: its antialiased edge, covered too


@dataclass(frozen=True)
class UserImage:
    path: str
    width: int
    height: int
    caption: str | None = None

    @property
    def ratio(self) -> str:
        """Proportions for the prompts, like "2.33:1"."""
        return f"{self.width / self.height:.2f}:1"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "UserImage":
        return cls(**json.loads(text))


def marker_color(style: str) -> tuple[int, int, int]:
    """Green, unless the style's palette has a green of its own; then magenta."""
    for value in _HEX.findall(style):
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        if g > 150 and r < 140 and b < 140:
            return MAGENTA
    return GREEN


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def find_area(slide: bytes, marker: tuple[int, int, int]) -> tuple[int, int, int, int] | None:
    """Box (x0, y0, x1, y1) of the solid marker-colored area on a slide, or None if there is none."""
    image = PILImage.open(io.BytesIO(slide)).convert("RGB")
    mask = _marker_mask(image, marker)
    width, height = mask.size
    rows = [mask.crop((0, y, width, y + 1)).histogram()[255] for y in range(height)]
    if max(rows) < MIN_SIDE:
        return None
    # Rows and then columns where the marker dominates: stray marker-colored pixels elsewhere don't count.
    ys = [y for y, n in enumerate(rows) if n > 0.5 * max(rows)]
    y0, y1 = ys[0], ys[-1]
    cols = [mask.crop((x, y0, x + 1, y1 + 1)).histogram()[255] for x in range(width)]
    xs = [x for x, n in enumerate(cols) if n > 0.5 * (y1 - y0 + 1)]
    if not xs:
        return None
    x0, x1 = xs[0], xs[-1]
    if x1 - x0 + 1 < MIN_SIDE or y1 - y0 + 1 < MIN_SIDE:
        return None
    filled = mask.crop((x0, y0, x1 + 1, y1 + 1)).histogram()[255]
    if filled / ((x1 - x0 + 1) * (y1 - y0 + 1)) < MIN_FILL:
        return None
    return x0, y0, x1, y1


def place_image(slide: bytes, box: tuple[int, int, int, int], picture: bytes, style: str) -> bytes:
    """Paste the picture into the box, with rounded corners and a thin frame in the style's
    text color. The slide is enlarged SCALE times first. Returns a JPEG: a PNG of this size
    is too big for Telegram photos.

    The model never draws the box in exactly the picture's proportions. A small difference
    (up to MAX_CROP of the picture) is covered by trimming its edges; a big one would cut off
    too much, so the picture goes in whole over a blurred copy of itself."""
    image = PILImage.open(io.BytesIO(slide)).convert("RGB")
    # Under the rounded corners the slide's own background must show, not the marker or a flat fill.
    _rebuild_under(image, box)
    image = image.resize((image.width * SCALE, image.height * SCALE), PILImage.LANCZOS)
    x0, y0 = (box[0] - PAD) * SCALE, (box[1] - PAD) * SCALE
    width, height = (box[2] - box[0] + 2 * PAD) * SCALE, (box[3] - box[1] + 2 * PAD) * SCALE

    photo = PILImage.open(io.BytesIO(picture)).convert("RGB")
    area, own = width / height, photo.width / photo.height
    crop = 1 - min(area / own, own / area)  # share of the picture that filling the box would cut off
    tile = _cover(photo, width, height)
    if crop > MAX_CROP:
        tile = tile.filter(ImageFilter.GaussianBlur(radius=max(width, height) / 25))
        k = min(width / photo.width, height / photo.height)
        whole = photo.resize((max(1, round(photo.width * k)), max(1, round(photo.height * k))), PILImage.LANCZOS)
        tile.paste(whole, ((width - whole.width) // 2, (height - whole.height) // 2))
        fit = f"whole over a blurred copy ({crop:.0%} would be cut)"
    else:
        fit = f"edges trimmed by {crop:.0%}"
    # Logged to see how often the model misses the proportions badly.
    log.info("User picture: box %.2f:1, picture %.2f:1, %s", area, own, fit)

    radius = max(6 * SCALE, round(min(width, height) * 0.04))
    mask = PILImage.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius, fill=255)
    image.paste(tile, (x0, y0), mask)
    ImageDraw.Draw(image).rounded_rectangle(
        (x0, y0, x0 + width - 1, y0 + height - 1), radius, outline=_frame_color(style), width=SCALE
    )
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=93)
    return out.getvalue()


def _cover(photo: PILImage.Image, width: int, height: int) -> PILImage.Image:
    """The picture scaled to fill width×height, its overflow trimmed evenly from both sides."""
    k = max(width / photo.width, height / photo.height)
    scaled = photo.resize(
        (max(width, round(photo.width * k)), max(height, round(photo.height * k))), PILImage.LANCZOS
    )
    left, top = (scaled.width - width) // 2, (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def _rebuild_under(image: PILImage.Image, box: tuple[int, int, int, int]) -> None:
    """Replace the box (and its antialiased edge) with a smooth blend of the slide's pixels
    just outside its four sides (a Coons patch): gradients, photos and plates continue under
    it, so nothing of the box shows past the picture's rounded corners."""
    x0, y0, x1, y1 = box[0] - PAD, box[1] - PAD, box[2] + PAD, box[3] + PAD
    pixels = image.load()

    def at(x: int, y: int) -> tuple[int, int, int]:
        return pixels[min(max(x, 0), image.width - 1), min(max(y, 0), image.height - 1)]

    left = [at(x0 - 1, y) for y in range(y0, y1 + 1)]
    right = [at(x1 + 1, y) for y in range(y0, y1 + 1)]
    top = [at(x, y0 - 1) for x in range(x0, x1 + 1)]
    bottom = [at(x, y1 + 1) for x in range(x0, x1 + 1)]
    tl, tr, bl, br = at(x0 - 1, y0 - 1), at(x1 + 1, y0 - 1), at(x0 - 1, y1 + 1), at(x1 + 1, y1 + 1)
    width, height = x1 - x0 + 1, y1 - y0 + 1
    data = []
    for j in range(height):
        v = (j + 1) / (height + 1)
        for i in range(width):
            u = (i + 1) / (width + 1)
            data.append(
                tuple(
                    max(0, min(255, round(
                        (1 - u) * left[j][c] + u * right[j][c] + (1 - v) * top[i][c] + v * bottom[i][c]
                        - ((1 - u) * (1 - v) * tl[c] + u * (1 - v) * tr[c] + (1 - u) * v * bl[c] + u * v * br[c])
                    )))
                    for c in range(3)
                )
            )
    patch = PILImage.new("RGB", (width, height))
    patch.putdata(data)
    image.paste(patch, (x0, y0))


def _marker_mask(image: PILImage.Image, marker: tuple[int, int, int]) -> PILImage.Image:
    """255 where a pixel is close to the marker color, 0 elsewhere."""
    bands = []
    for band, target in zip(image.split(), marker):
        if target > 127:
            bands.append(band.point(lambda v: 255 if v > 170 else 0))
        else:
            bands.append(band.point(lambda v: 255 if v < 100 else 0))
    return ImageChops.multiply(ImageChops.multiply(bands[0], bands[1]), bands[2])


def _frame_color(style: str) -> str:
    match = _TEXT_PRIMARY.search(style)
    return f"#{match.group(1)}" if match else "#808080"
