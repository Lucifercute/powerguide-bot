"""Load private model instructions; prompt text is not distributed in Git."""
from dataclasses import dataclass
from pathlib import Path

FILES = {
    "PLAN": "editable_visual_plan.md",
    "HTML": "editable_html.md",
    "IMAGE": "editable_image.md",
}
# Optional: without it the rendered slides are not reviewed by a model.
OPTIONAL = {"REVIEW": "editable_review.md"}


@dataclass(frozen=True)
class PromptSet:
    PLAN: str
    HTML: str
    IMAGE: str
    REVIEW: str = ""


def load(directory: Path) -> PromptSet:
    missing = [name for name in FILES.values() if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(
            "Не найдены закрытые промпты редактируемой презентации: "
            + ", ".join(missing)
            + ". Получите комплект у владельца проекта и поместите в PROMPTS_DIR."
        )
    values = {key: (directory / name).read_text(encoding="utf-8") for key, name in FILES.items()}
    if any(not value.strip() for value in values.values()):
        raise RuntimeError("Файлы промптов редактируемой презентации не должны быть пустыми")
    values.update({key: (directory / name).read_text(encoding="utf-8")
                   for key, name in OPTIONAL.items() if (directory / name).is_file()})
    return PromptSet(**values)