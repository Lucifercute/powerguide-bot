from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Question:
    key: str  # unique across the whole survey: "<group>.<question>", e.g. "about.goal"
    number: int  # 1-based position inside its group
    text: str


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    questions: list[Question]


@dataclass(frozen=True)
class Survey:
    groups: list[Group]

    @property
    def questions(self) -> list[Question]:
        """Every question of the survey, in order."""
        return [q for group in self.groups for q in group.questions]


class QuestionBank:
    """The survey is read from YAML on every access, so the file can be edited
    without restarting the bot."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Survey:
        with self.path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        groups = [
            Group(
                key=g["key"],
                title=g["title"],
                questions=[
                    Question(key=f"{g['key']}.{q['key']}", number=i, text=q["text"])
                    for i, q in enumerate(g["questions"], start=1)
                ],
            )
            for g in raw["groups"]
        ]
        return Survey(groups=groups)
