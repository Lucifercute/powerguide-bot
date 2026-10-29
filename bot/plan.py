"""The slide plan (prompt 03-1) split into parts for showing it slide by slide and editing one slide.

The plan stays one text in the DB (prompt 06 gets it whole): slides «СЛАЙД N» first, then the
sections «НЕ ХВАТАЕТ ДАННЫХ» and «ОПОРА НА СТРУКТУРУ». Only the slides and the missing-data
notes are shown to the user; the other section is kept for the model.
"""

import re
from dataclasses import dataclass

SLIDE_TYPES = ("титульный", "текст", "таблица", "диаграмма", "схема", "текст + иллюстрация")
FIELDS = ("ЗАГОЛОВОК", "ТИП", "СОДЕРЖАНИЕ")

_SLIDE_HEADER = re.compile(r"^[ \t#*_]*СЛАЙД[ \t]+(\d+)[^\n]*$", re.MULTILINE | re.IGNORECASE)
_SECTION_HEADER = re.compile(
    r"^[ \t#*_]*(НЕ ХВАТАЕТ ДАННЫХ|ОПОРА НА СТРУКТУРУ)[ \t#*_:]*(.*)$", re.MULTILINE | re.IGNORECASE
)
_MISSING_SECTION = "НЕ ХВАТАЕТ ДАННЫХ"
_MISSING_SLIDE = re.compile(r"^Слайд\s+(\d+)\s*[,:.—–-]?\s*(.*)$", re.IGNORECASE)
_SEPARATOR = re.compile(r"^[ \t]*[-—–_*=]{3,}[ \t]*$", re.MULTILINE)  # «---» the model may put between slides
_FIELD = re.compile(r"^[ \t*_]*(ЗАГОЛОВОК|ТИП|СОДЕРЖАНИЕ)[ \t*_]*:[ \t*_]*(.*)$", re.MULTILINE | re.IGNORECASE)


class SlideFormatError(ValueError):
    """The edited slide lost its structure; the message says what to fix."""


@dataclass
class _Block:
    start: int
    end: int
    text: str  # without the header line


def _slide_blocks(plan: str) -> dict[int, _Block]:
    """Slides by number. Only lines before the first section count: «ОПОРА НА СТРУКТУРУ» has
    lines like "Слайд 3: 1.1", which are not slides."""
    sections = list(_SECTION_HEADER.finditer(plan))
    slides_end = sections[0].start() if sections else len(plan)
    headers = list(_SLIDE_HEADER.finditer(plan, 0, slides_end))
    blocks: dict[int, _Block] = {}
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else slides_end
        text = _SEPARATOR.sub("", plan[header.end() : end]).strip()
        blocks.setdefault(int(header.group(1)), _Block(header.start(), end, text))
    return blocks


def _missing_block(plan: str) -> _Block | None:
    """The «НЕ ХВАТАЕТ ДАННЫХ» section; its text includes what the model put on the header line."""
    sections = list(_SECTION_HEADER.finditer(plan))
    for i, header in enumerate(sections):
        if header.group(1).upper() == _MISSING_SECTION:
            end = sections[i + 1].start() if i + 1 < len(sections) else len(plan)
            return _Block(header.start(), end, f"{header.group(2)}\n{plan[header.end() : end]}".strip())
    return None


def _missing_lines(text: str) -> list[str]:
    """Notes of the section without brackets and bullets; «нет» means no notes."""
    lines = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•*").strip().strip("[]").strip()
        if line and line.strip('."«»').lower() != "нет":
            lines.append(line)
    return lines


def plan_slides(plan: str) -> dict[int, str]:
    """Slide texts (ЗАГОЛОВОК / ТИП / СОДЕРЖАНИЕ, without «СЛАЙД N») by number, in plan order."""
    return {number: block.text for number, block in _slide_blocks(plan).items()}


def missing_data(plan: str) -> tuple[dict[int, list[str]], list[str]]:
    """Missing-data notes: those starting with «Слайд N» by slide (without that prefix),
    and the rest — notes about the whole plan."""
    block = _missing_block(plan)
    by_slide: dict[int, list[str]] = {}
    general = []
    for line in _missing_lines(block.text) if block else []:
        match = _MISSING_SLIDE.match(line)
        if match:
            by_slide.setdefault(int(match.group(1)), []).append(match.group(2) or line)
        else:
            general.append(line)
    return by_slide, general


def replace_slide(plan: str, number: int, text: str) -> str:
    """Plan with slide N replaced by the user's text; slide N's missing-data notes are dropped:
    the user has seen them and edited the slide."""
    missing = _missing_block(plan)
    if missing:
        kept = [
            line
            for line in _missing_lines(missing.text)
            if not ((m := _MISSING_SLIDE.match(line)) and int(m.group(1)) == number)
        ]
        section = f"{_MISSING_SECTION}\n" + ("\n".join(f"[{line}]" for line in kept) or "нет") + "\n\n"
        plan = plan[: missing.start] + section + plan[missing.end :]
    slide = _slide_blocks(plan)[number]
    return plan[: slide.start] + f"СЛАЙД {number}\n{text}\n\n" + plan[slide.end :]


def _slide_type(raw: str) -> str:
    """«Текст+иллюстрация.» → «текст + иллюстрация»."""
    return " ".join(raw.lower().replace("+", " + ").split()).strip(".")


def parse_slide_edit(text: str, number: int) -> str:
    """The user's edited slide N, checked: ЗАГОЛОВОК, ТИП and СОДЕРЖАНИЕ in this order, a known
    type, a scheme with blocks and links. The «СЛАЙД N» line may be kept or removed.
    Returns the text without that line; raises SlideFormatError."""
    text = text.strip()
    header = _SLIDE_HEADER.match(text)
    if header:
        if int(header.group(1)) != number:
            raise SlideFormatError(
                f"в тексте указан слайд {header.group(1)}, а исправляется слайд {number}. "
                f"Напишите «СЛАЙД {number}» или уберите эту строку."
            )
        text = text[header.end() :].strip()
    fields: dict[str, re.Match] = {}
    for match in _FIELD.finditer(text):
        fields.setdefault(match.group(1).upper(), match)  # «Тип: …» inside the content is not the field
    absent = [name for name in FIELDS if name not in fields]
    if absent:
        raise SlideFormatError("не найдены строки " + ", ".join(f"«{name}:»" for name in absent) + ".")
    if not fields["ЗАГОЛОВОК"].start() < fields["ТИП"].start() < fields["СОДЕРЖАНИЕ"].start():
        raise SlideFormatError("сохраните порядок строк: ЗАГОЛОВОК, ТИП, СОДЕРЖАНИЕ.")
    if not fields["ЗАГОЛОВОК"].group(2).strip():
        raise SlideFormatError("заголовок пустой.")
    kind = _slide_type(fields["ТИП"].group(2))
    if kind not in SLIDE_TYPES:
        raise SlideFormatError(
            f"тип «{fields['ТИП'].group(2).strip()}» не подходит. Возможные типы: " + ", ".join(SLIDE_TYPES) + "."
        )
    content = text[fields["СОДЕРЖАНИЕ"].start(2) :].strip()
    if not content:
        raise SlideFormatError("содержание пустое.")
    if kind == "схема" and not ("блоки:" in content.lower() and "связи:" in content.lower()):
        raise SlideFormatError("у схемы в содержании должны быть «Блоки:» и «Связи:».")
    return text
