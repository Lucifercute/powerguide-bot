import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, TypeAdapter, ValidationError

from bot.llm import Image, ImageLLM, LLMError, TextLLM
from bot.questions import Survey
from bot.search import SearchResult, WebSearch
from bot.sources import SourceFile
from bot.textcheck import TextIssue, compare, confirmed, expected_text

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    kind: str  # artifact kind in the DB
    prompt: str  # prompt file name in prompts_dir, without .md
    title: str  # shown to the user
    filename: str  # name of the file sent to the user


TZ = Step("tz", "01_tz", "Техническое задание", "ТЗ_на_презентацию.txt")
DATA = Step("data", "02_data", "Структура данных", "Структура_данных.txt")
PLAN = Step("plan", "03-1_plan", "План презентации", "План_презентации.txt")

# Executed in this order; each step can use the artifacts of the previous ones.
STEPS = [TZ, DATA, PLAN]

# Stage А (survey). Artifact kinds:
DESCRIPTION = "description"  # the user's free-form description of the presentation
SOURCES_NOTE = "sources_note"  # what the user wrote about the source data instead of (or with) files
# JSON list of question keys the analysis found no answer to: the bot asks them one by one.
ASKED_QUESTIONS = "asked_questions"
SURVEY_CONFIRMED = "survey_confirmed"  # marker: answers confirmed (or nothing had to be asked)
SURVEY_ANALYSIS_PROMPT = "00_survey_analysis"
SKIPPED_ANSWER = "Не указано"  # saved when the user presses «Пропустить / Не знаю»

# Web search results for DATA, saved so a retry doesn't spend search credits again.
# Empty string = search done (or unavailable) and found nothing.
SOURCES = "sources"

SEARCH_QUERIES_PROMPT = "02a_search_queries"
# Gemini accepts up to 20 MB inside one request; bigger sets of files go through the File API.
INLINE_FILES_LIMIT = 15_000_000

# Stage Б (design). Artifact kinds:
STYLE_REF = "style_ref"  # path to the user's reference image; "" = skipped
BACKGROUND_FIELDS = "background_fields"  # JSON of BackgroundFields, reused on regeneration
BACKGROUND = "background"  # path to the accepted (or last generated) background image
STYLE = "style"  # result of prompt 05, the style text for slide generation
BACKGROUND_ACCEPTED = "background_accepted"  # marker: the user pressed «Принять» under the background

# Stage В (slides). Artifact kinds:
DESCRIPTIONS = "descriptions"  # result of prompt 06: «Изображение N» blocks


def slide_kind(number: int) -> str:
    """Artifact kind of slide N: path to its image."""
    return f"slide_{number:02d}"


def slide_prompt_kind(number: int) -> str:
    """Artifact kind of the exact prompt slide N was drawn with: kept to trace problems back."""
    return f"slide_prompt_{number:02d}"


def text_check_kind(number: int) -> str:
    """Artifact kind of slide N's text check: JSON list of [expected, found] pairs, [] = no issues."""
    return f"text_check_{number:02d}"


DESCRIPTIONS_PROMPT = "06_descriptions"
TEXT_CHECK_PROMPT = "08_text_check"
# Reading text needs little reasoning, but with thinking fully off the model "corrects"
# typos while reading and misses them (tested on real slides): LOW catches them reliably.
TEXT_CHECK_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
)
SLIDE_PROMPT = "07_slide"
SLIDE_REFERENCE_PROMPT = "07a_slide_reference"
MAX_SLIDES = 10  # every slide is a paid image request
_DESCRIPTION_HEADER = re.compile(r"^[\s#*_«\"]*Изображение\s+(\d+)\b", re.MULTILINE)

BACKGROUND_PROMPT = "04_background"
BACKGROUND_FIELDS_PROMPT = "04a_background_fields"
STYLE_PROMPT = "05_style"

RATIO_ANSWER_KEY = "design.ratio"
# Aspect ratios the image models accept (ImageConfig.aspect_ratio).
SUPPORTED_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
DEFAULT_RATIO = "16:9"
_RATIO = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:[:xх×*/]|на)\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s\])>»\"']+")


def load_prompt(prompts_dir: Path, name: str) -> str:
    """Prompts are read on every call, so they can be edited without restarting the bot."""
    return (prompts_dir / f"{name}.md").read_text(encoding="utf-8").strip()


def format_answers(survey: Survey, answers: dict[str, str]) -> str:
    blocks = []
    for group in survey.groups:
        lines = [group.title]
        for q in group.questions:
            lines.append(f"{q.number}. {q.text}\nОтвет: {answers.get(q.key, '—')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_customer_data(file_names: list[str], note: str | None) -> str:
    """What the customer gave as source data: file names and their own comment."""
    lines = [f"Приложенные файлы: {', '.join(file_names)}." if file_names else "Файлы не приложены."]
    if note:
        lines.append(f"Комментарий заказчика об исходных данных:\n{note}")
    return "\n\n".join(lines)


def format_sources(results: list[SearchResult]) -> str:
    return "\n\n".join(
        f"[{i}] {r.title}\nURL: {r.url}\nФрагмент: {r.content}" for i, r in enumerate(results, start=1)
    )


def _urls(text: str) -> set[str]:
    return {u.rstrip(".,;:") for u in _URL.findall(text)}


def build_prompt(
    step: Step,
    template: str,
    survey: Survey,
    answers: dict[str, str],
    artifacts: dict[str, str],
    files: list[types.Part | str] | None = None,
    file_names: list[str] = (),
) -> types.ContentListUnion:
    """Prompt for the step. For DATA, `files` are the customer's files ready for the model
    (see Pipeline._file_parts); the result is then a list of text and file parts.
    file_names: names of the customer's files, for the ТЗ to say what data there is."""
    note = artifacts.get(SOURCES_NOTE)
    if step is TZ:
        description = artifacts.get(DESCRIPTION)
        about = f"Описание презентации моими словами:\n\n{description}\n\n" if description else ""
        return (
            f"{template}\n\n{about}"
            f"Исходные данные:\n\n{format_customer_data(list(file_names), note)}\n\n"
            f"Мои ответы на вопросы:\n\n{format_answers(survey, answers)}"
        )
    if step is DATA:
        # Without these explicit notes the model invents customer files and URLs.
        sources = artifacts.get(SOURCES)
        if sources:
            internet = (
                "РЕЗУЛЬТАТЫ ПОИСКА В ОТКРЫТЫХ ИСТОЧНИКАХ\n\n"
                "Это все страницы, которые ты открыл. Ссылайся только на них и копируй URL отсюда "
                "символ в символ. Других источников из интернета у тебя нет.\n\n"
                f"{sources}"
            )
        else:
            internet = "ДОСТУП К ИНТЕРНЕТУ\n\nНет. Поиск в открытых источниках недоступен."
        head = f"{template}\n\nТЕХНИЧЕСКОЕ ЗАДАНИЕ НА ПРЕЗЕНТАЦИЮ\n\n{artifacts[TZ.kind]}\n\nИСХОДНЫЕ МАТЕРИАЛЫ ЗАКАЗЧИКА\n\n"
        if note:
            head += f"Комментарий заказчика об исходных данных:\n{note}\n\n"
        if not files:
            return f"{head}Файлы не приложены.\n\n{internet}"
        return [head, *files, f"\n\n{internet}"]
    if step is PLAN:
        return (
            f"{template}\n\n"
            f"ТЕХНИЧЕСКОЕ ЗАДАНИЕ НА ПРЕЗЕНТАЦИЮ\n\n{artifacts[TZ.kind]}\n\n"
            f"СТРУКТУРА ДАННЫХ\n\n{artifacts[DATA.kind]}"
        )
    raise ValueError(f"unknown step: {step.kind}")


def parse_aspect_ratio(answer: str | None) -> str:
    """User's answer like "16х9", "4:3", "1920x1080" → nearest supported ratio; DEFAULT_RATIO if unclear."""
    text = answer or ""
    if "квадрат" in text.lower():
        return "1:1"
    for match in _RATIO.finditer(text):
        width, height = (float(g.replace(",", ".")) for g in match.groups())
        if width > 0 and height > 0:
            value = math.log(width / height)
            return min(SUPPORTED_RATIOS, key=lambda r: abs(math.log(_ratio_value(r)) - value))
    return DEFAULT_RATIO


def _ratio_value(ratio: str) -> float:
    width, height = ratio.split(":")
    return int(width) / int(height)


def canvas_size(ratio: str) -> str:
    """Canvas in pixels for prompt 05: the short side is 1080, like 1920×1080 for 16:9."""
    value = _ratio_value(ratio)
    if value >= 1:
        return f"{round(1080 * value)}×1080"
    return f"1080×{round(1080 / value)}"


class FoundAnswer(BaseModel):
    """An answer the survey analysis found in the user's description or files."""

    key: str
    answer: str


class BackgroundFields(BaseModel):
    """The bracketed fields of prompt 04."""

    topic: str
    palette: str
    style: str
    mood: str


def build_background_prompt(template: str, ratio: str, fields: BackgroundFields) -> str:
    values = {"ratio": ratio, **fields.model_dump()}
    for name, value in values.items():
        template = template.replace(f"{{{name}}}", value.strip())
    return template


def split_descriptions(text: str) -> dict[int, str]:
    """Prompt 06 result → {N: the «Изображение N» block}, in order. Text before the first block
    is dropped; if a number repeats, the first block wins."""
    headers = list(_DESCRIPTION_HEADER.finditer(text))
    blocks: dict[int, str] = {}
    for header, following in zip(headers, headers[1:] + [None]):
        number = int(header.group(1))
        end = following.start() if following else len(text)
        blocks.setdefault(number, text[header.start() : end].strip())
    return blocks


def build_slide_prompt(template: str, number: int, description: str, style: str, ratio: str) -> str:
    values = {"number": str(number), "description": description, "style": style, "ratio": ratio}
    for name, value in values.items():
        template = template.replace(f"{{{name}}}", value.strip())
    return template


def strip_review_request(plan: str) -> str:
    """Prompt 03-1 ends the plan with "Проверьте план и напишите, что поправить…".
    In the bot edits are made by sending the file back, so that line would mislead."""
    lines = plan.rstrip().splitlines()
    if lines and lines[-1].strip().strip('"«»').startswith("Проверьте план"):
        lines.pop()
    return "\n".join(lines).rstrip()


class Pipeline:
    """Model-facing part of the pipeline: builds prompts, calls the LLM and web search.
    Talking to the user stays in handlers."""

    def __init__(
        self, llm: TextLLM, images: ImageLLM, search: WebSearch | None, prompts_dir: Path, max_queries: int
    ):
        self.llm = llm
        self.images = images
        self.search = search
        self.prompts_dir = prompts_dir
        self.max_queries = max_queries

    async def run_step(
        self,
        step: Step,
        survey: Survey,
        answers: dict[str, str],
        artifacts: dict[str, str],
        sources: list[SourceFile] = (),
    ) -> tuple[str, str]:
        """Return (result, model). Raises LLMError if every model failed."""
        files = await self._file_parts(sources) if step is DATA else None
        template = load_prompt(self.prompts_dir, step.prompt)
        prompt = build_prompt(step, template, survey, answers, artifacts, files, [f.name for f in sources])
        result, model = await self.llm.generate(prompt)
        if step is DATA:
            invented = _urls(result) - _urls(artifacts.get(SOURCES) or "")
            if invented:
                log.warning("DATA result cites URLs that were not in search results: %s", sorted(invented))
        if step is PLAN:
            result = strip_review_request(result)
        return result, model

    async def analyze_survey(
        self, survey: Survey, description: str, note: str | None, sources: list[SourceFile]
    ) -> tuple[dict[str, str], str]:
        """Find answers to the survey questions in the user's description, comment and files.
        Returns ({question key: answer}, model) for the answered questions only. Raises LLMError,
        also on a malformed answer: then nobody knows which questions to ask."""
        questions = "\n".join(f"{q.key}: {q.text}" for q in survey.questions)
        head = (
            f"{load_prompt(self.prompts_dir, SURVEY_ANALYSIS_PROMPT)}\n\n"
            f"ВОПРОСЫ (ключ: вопрос)\n\n{questions}\n\n"
            f"ОПИСАНИЕ ПРЕЗЕНТАЦИИ ОТ ЗАКАЗЧИКА\n\n{description}\n\n"
            f"КОММЕНТАРИЙ ЗАКАЗЧИКА ОБ ИСХОДНЫХ ДАННЫХ\n\n{note or 'Нет.'}\n\n"
            "ПРИЛОЖЕННЫЕ ФАЙЛЫ\n\n"
        )
        files = await self._file_parts(sources)
        contents = [head, *files] if files else f"{head}Файлы не приложены."
        text, model = await self.llm.generate(
            contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=list[FoundAnswer]
            ),
        )
        try:
            found = TypeAdapter(list[FoundAnswer]).validate_json(text)
        except ValidationError as e:
            raise LLMError(f"malformed survey analysis: {text[:200]}", quota_exceeded=False) from e
        keys = {q.key for q in survey.questions}
        unknown = [a.key for a in found if a.key not in keys]
        if unknown:
            log.warning("Survey analysis returned unknown question keys: %s", unknown)
        return {a.key: a.answer.strip() for a in found if a.key in keys and a.answer.strip()}, model

    async def _file_parts(self, sources: list[SourceFile]) -> list[types.Part | str]:
        """Customer's files as prompt parts: a caption with the file name (the model cites it),
        then the extracted text or the file itself. Raises LLMError if an upload fails."""
        if not sources:
            return []
        binary = [f for f in sources if f.text is None]
        upload = self.llm.can_upload and sum(f.path.stat().st_size for f in binary) > INLINE_FILES_LIMIT
        parts: list[types.Part | str] = [f"Приложено файлов: {len(sources)}. Ссылайся на них по имени файла."]
        for file in sources:
            if file.text is not None:
                parts.append(f"\n\nФайл «{file.name}» (текст извлечён из файла):\n\n{file.text}")
                continue
            parts.append(f"\n\nФайл «{file.name}»:")
            if upload:
                parts.append(await self.llm.upload(file.path, file.mime_type))
            else:
                parts.append(types.Part.from_bytes(data=file.path.read_bytes(), mime_type=file.mime_type))
        return parts

    async def find_sources(self, tz: str) -> str:
        """Search the web for facts the ТЗ needs. Returns formatted sources, or "" if
        search is not configured, not allowed by the ТЗ, or failed — the step then runs without it."""
        if self.search is None:
            return ""
        try:
            queries = await self._search_queries(tz)
        except (LLMError, ValueError) as e:  # ValueError covers malformed JSON
            log.warning("Could not build search queries: %s", e)
            return ""
        log.info("Search queries: %s", queries)
        if not queries:
            return ""
        return format_sources(await self.search.search(queries))

    async def _search_queries(self, tz: str) -> list[str]:
        template = load_prompt(self.prompts_dir, SEARCH_QUERIES_PROMPT).replace(
            "{max_queries}", str(self.max_queries)
        )
        text, _ = await self.llm.generate(
            f"{template}\n\nТЕХНИЧЕСКОЕ ЗАДАНИЕ\n\n{tz}",
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=list[str]),
        )
        queries = json.loads(text)
        if not isinstance(queries, list):
            raise ValueError(f"expected a JSON list, got: {text[:200]}")
        return [q.strip() for q in queries if isinstance(q, str) and q.strip()][: self.max_queries]

    async def background_fields(self, tz: str, has_reference: bool) -> BackgroundFields:
        """Fill the bracketed fields of prompt 04 from the ТЗ. Raises LLMError."""
        template = load_prompt(self.prompts_dir, BACKGROUND_FIELDS_PROMPT)
        reference = "да" if has_reference else "нет"
        text, _ = await self.llm.generate(
            f"{template}\n\nПОЛЬЗОВАТЕЛЬ ПРИЛОЖИЛ ГРАФИЧЕСКИЙ РЕФЕРЕНС: {reference}\n\n"
            f"ТЕХНИЧЕСКОЕ ЗАДАНИЕ НА ПРЕЗЕНТАЦИЮ\n\n{tz}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=BackgroundFields
            ),
        )
        try:
            return BackgroundFields.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"malformed background fields: {text[:200]}", quota_exceeded=False) from e

    async def background(
        self, fields: BackgroundFields, ratio: str, reference: Image | None
    ) -> tuple[Image, str]:
        """Prompt 04 → background image. Raises LLMError."""
        prompt = build_background_prompt(load_prompt(self.prompts_dir, BACKGROUND_PROMPT), ratio, fields)
        log.info("Background prompt:\n%s", prompt)
        contents: list = [prompt]
        if reference is not None:
            contents.append(types.Part.from_bytes(data=reference.data, mime_type=reference.mime_type))
        return await self.images.generate(contents, aspect_ratio=ratio)

    async def style(self, background: Image, ratio: str) -> tuple[str, str]:
        """Prompt 05 with the background as the reference → style text. Raises LLMError."""
        prompt = load_prompt(self.prompts_dir, STYLE_PROMPT).replace("{canvas}", canvas_size(ratio))
        image = types.Part.from_bytes(data=background.data, mime_type=background.mime_type)
        return await self.llm.generate([image, prompt])

    async def descriptions(self, plan: str) -> tuple[str, str]:
        """Prompt 06 on the plan → «Изображение N» blocks. Raises LLMError, also when the
        answer has no blocks, so a malformed result is not saved and the retry asks again."""
        template = load_prompt(self.prompts_dir, DESCRIPTIONS_PROMPT)
        text, model = await self.llm.generate(f"{template}\n\nПОСЛАЙДОВЫЙ ПЛАН ПРЕЗЕНТАЦИИ\n\n{plan}")
        if not split_descriptions(text):
            raise LLMError(f"no «Изображение N» blocks in descriptions: {text[:200]}", quota_exceeded=False)
        return text, model

    async def check_slide_text(self, slide: Image, description: str) -> tuple[list[TextIssue], str | None]:
        """Prompt 08: read the text on a drawn slide and compare it with the captions its
        description asked for. A slide with differences is read once more, and only the
        differences found both times are kept. Returns (issues, model). Raises LLMError."""
        expected = expected_text(description)
        if not expected:  # nothing to compare with: every word on the slide would look "extra"
            return [], None
        issues, model = await self._read_and_compare(slide, expected)
        if issues:
            again, model = await self._read_and_compare(slide, expected)
            issues = confirmed(issues, again)
        return issues, model

    async def _read_and_compare(self, slide: Image, expected: str) -> tuple[list[TextIssue], str]:
        prompt = load_prompt(self.prompts_dir, TEXT_CHECK_PROMPT)
        image = types.Part.from_bytes(data=slide.data, mime_type=slide.mime_type)
        text, model = await self.llm.generate([image, prompt], config=TEXT_CHECK_CONFIG)
        return compare(expected, text), model

    def slide_prompt(self, number: int, description: str, style: str, ratio: str, with_sample: bool) -> str:
        """Text of prompt 07-1 for one slide. Only this slide's description goes in, so the
        model can't pull text from other slides. with_sample: slide 1 will be attached as a
        sample of the design, and the prompt says how to use it."""
        prompt = build_slide_prompt(load_prompt(self.prompts_dir, SLIDE_PROMPT), number, description, style, ratio)
        if with_sample:
            prompt += "\n\n" + load_prompt(self.prompts_dir, SLIDE_REFERENCE_PROMPT)
        return prompt

    async def slide(
        self, prompt: str, ratio: str, background: Image, first_slide: Image | None
    ) -> tuple[Image, str]:
        """Slide prompt (see slide_prompt) + background (+ slide 1 as a design sample) → image.
        Raises LLMError."""
        contents: list = [prompt, types.Part.from_bytes(data=background.data, mime_type=background.mime_type)]
        if first_slide is not None:
            contents.append(types.Part.from_bytes(data=first_slide.data, mime_type=first_slide.mime_type))
        return await self.images.generate(contents, aspect_ratio=ratio)
