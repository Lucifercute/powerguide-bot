from dataclasses import dataclass
from pathlib import Path

PREVIEW_FILE = "preview.jpg"  # what the user sees: a collage of sample slides
BACKGROUND_FILES = ("background.png", "background.jpg")  # the slides are drawn on it
STYLE_FILE = "style.md"  # used instead of the result of prompt 05


@dataclass(frozen=True)
class Preset:
    """A prepared design the user can pick instead of sending a reference."""

    number: int
    preview: Path
    background: Path
    style: str


class PresetStore:
    """Prepared designs in <dir>/<number>/ (see scripts/build_presets.py). Read from disk on
    every access, so a set can be added or changed without restarting the bot. A folder
    with a missing file is skipped."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Preset]:
        if not self.path.is_dir():
            return []
        presets = []
        for folder in self.path.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            preview = folder / PREVIEW_FILE
            background = next((folder / name for name in BACKGROUND_FILES if (folder / name).exists()), None)
            style = folder / STYLE_FILE
            if preview.exists() and background is not None and style.exists():
                presets.append(Preset(int(folder.name), preview, background, style.read_text(encoding="utf-8").strip()))
        return sorted(presets, key=lambda p: p.number)

    def get(self, number: int) -> Preset | None:
        return next((p for p in self.load() if p.number == number), None)
