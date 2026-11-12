"""Shared session resources; no dependency on Telegram handlers."""
import mimetypes
from pathlib import Path
from bot.db import Database
from bot.llm import Image
from bot.pipeline import (MAX_SLIDES, RATIO_ANSWER_KEY, BACKGROUND, BACKGROUND_ALT,
                          BACKGROUND_CHOICE, parse_aspect_ratio, user_image_kind)
from bot.user_images import UserImage
from bot.jobs import SessionJobs

_running = SessionJobs()
IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

async def load_user_images(db: Database, session_id: int) -> dict[int, UserImage]:
    """The user's pictures by slide number."""
    artifacts = await db.get_artifacts(session_id)
    images = {}
    for number in range(1, MAX_SLIDES + 1):
        if user_image_kind(number) in artifacts:
            images[number] = UserImage.from_json(artifacts[user_image_kind(number)])
    return images


def save_image(files_dir: Path, session_id: int, name: str, image: Image) -> Path:
    folder = files_dir / str(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}{IMAGE_EXTENSIONS.get(image.mime_type, '.png')}"
    path.write_bytes(image.data)
    return path


def load_image(path: str) -> Image:
    file = Path(path)
    return Image(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "image/png")


async def session_ratio(db: Database, session_id: int) -> str:
    return parse_aspect_ratio((await db.get_answers(session_id)).get(RATIO_ANSWER_KEY))


def chosen_background(artifacts: dict[str, str]) -> str:
    """Path to the background the slides are drawn on."""
    return artifacts[BACKGROUND_ALT] if artifacts.get(BACKGROUND_CHOICE) == "2" else artifacts[BACKGROUND]
