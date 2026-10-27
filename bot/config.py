import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    questions_path: Path
    prompts_dir: Path
    presets_dir: Path  # prepared designs the user can pick (bot/presets.py)
    gemini_api_key: str
    gemini_text_models: list[str]
    tavily_api_key: str | None
    search_max_queries: int
    vertex_api_key: str
    vertex_text_models: list[str]
    image_models: list[str]
    files_dir: Path
    libreoffice_path: str | None  # None: Word/PowerPoint files are read as text only


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value


def _find_libreoffice() -> str | None:
    """LibreOffice binary: on PATH (Linux servers) or in the standard macOS app."""
    mac_app = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return shutil.which("soffice") or (str(mac_app) if mac_app.exists() else None)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config() -> Config:
    load_dotenv()
    # Empty GEMINI_TEXT_MODELS = no free models: every text request goes to VERTEX_TEXT_MODELS.
    models = os.getenv("GEMINI_TEXT_MODELS", "gemini-3.5-flash,gemini-flash-latest")
    image_models = os.getenv("IMAGE_MODELS", "gemini-nano-banana-2.1,gemini-3-pro-image")
    return Config(
        bot_token=_required("BOT_TOKEN"),
        db_path=Path(os.getenv("DB_PATH", "data/bot.db")),
        questions_path=Path(os.getenv("QUESTIONS_PATH", "questions.yaml")),
        prompts_dir=Path(os.getenv("PROMPTS_DIR", "prompts")),
        presets_dir=Path(os.getenv("PRESETS_DIR", "presets")),
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_text_models=_split(models),
        tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
        search_max_queries=int(os.getenv("SEARCH_MAX_QUERIES", "6")),
        vertex_api_key=_required("VERTEX_API_KEY"),
        vertex_text_models=_split(os.getenv("VERTEX_TEXT_MODELS", "")),
        image_models=_split(image_models),
        files_dir=Path(os.getenv("FILES_DIR", "data/sessions")),
        libreoffice_path=os.getenv("LIBREOFFICE_PATH") or _find_libreoffice(),
    )
