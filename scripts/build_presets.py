"""Build presets/<N>/ from a folder of prepared designs.

The source folder holds, for every number N:
    стильN.txt              style text (replaces the result of prompt 05)
    стильN.png | .jpg       background (cropped to 16:9 here if it isn't)
    Ref-preview (N).png     collage of sample slides shown to the user (shrunk to JPEG here)

Usage, from the project root: uv run python scripts/build_presets.py ~/Desktop/Prez-ref
Existing presets/<N>/ folders are replaced; others are left as they are."""
import re
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.presets import PREVIEW_FILE, STYLE_FILE

TARGET = Path("presets")
RATIO = 16 / 9
PREVIEW_MAX_SIDE = 1600  # Telegram shows photos at most ~1280 px wide anyway


def crop_to_ratio(image: Image.Image) -> Image.Image:
    width, height = image.size
    if abs(width / height - RATIO) <= 0.02:
        return image
    if width / height > RATIO:  # too wide: cut the sides
        new_width = round(height * RATIO)
        left = (width - new_width) // 2
        return image.crop((left, 0, left + new_width, height))
    new_height = round(width / RATIO)  # too tall: cut top and bottom
    top = (height - new_height) // 2
    return image.crop((0, top, width, top + new_height))


def build(source: Path) -> None:
    numbers = sorted(int(m.group(1)) for p in source.iterdir() if (m := re.fullmatch(r"стиль(\d+)\.txt", p.name)))
    if not numbers:
        sys.exit(f"No стильN.txt in {source}")
    for n in numbers:
        background = next((p for p in source.glob(f"стиль{n}.*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")), None)
        preview = next((p for p in source.iterdir() if re.fullmatch(rf"Ref-preview \({n}\)\.(png|jpe?g)", p.name, re.I)), None)
        if background is None or preview is None:
            print(f"Стиль {n}: skipped, background or preview is missing")
            continue
        folder = TARGET / str(n)
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)

        shutil.copy(source / f"стиль{n}.txt", folder / STYLE_FILE)
        bg = Image.open(background)
        cropped = crop_to_ratio(bg)
        if background.suffix.lower() == ".png":  # keep the source format: a JPEG would grow as PNG
            cropped.convert("RGB").save(folder / "background.png")
        else:
            cropped.convert("RGB").save(folder / "background.jpg", quality=95)
        image = Image.open(preview).convert("RGB")
        image.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
        image.save(folder / PREVIEW_FILE, quality=85)
        size = (folder / PREVIEW_FILE).stat().st_size // 1024
        print(f"Стиль {n}: background {bg.size} -> {cropped.size}, preview {image.size} {size} KB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(Path(sys.argv[1]).expanduser())
