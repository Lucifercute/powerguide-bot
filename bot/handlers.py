import asyncio
import json
import logging
import mimetypes
import re
from contextlib import suppress
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from bot.db import Database
from bot.export import build_pdf
from bot.llm import Image, LLMError
from bot.pipeline import (
    ASKED_QUESTIONS,
    BACKGROUND,
    ATTEMPT_EDIT,
    ATTEMPT_REDRAW,
    BACKGROUND_ACCEPTED,
    BACKGROUND_ALT,
    BACKGROUND_ALT_FIELDS,
    BACKGROUND_ATTEMPT,
    BACKGROUND_CHOICE,
    BACKGROUND_COMMENT,
    BACKGROUND_FIELDS,
    DATA,
    DESCRIPTION,
    DESCRIPTIONS,
    MAX_SLIDES,
    PLAN,
    RATIO_ANSWER_KEY,
    RESEARCH_ANSWER_KEY,
    SKIPPED_ANSWER,
    SOURCES,
    SOURCES_NOTE,
    STEPS,
    STYLE,
    STYLE_PRESET,
    STYLE_REF,
    SURVEY_CONFIRMED,
    TZ,
    BackgroundFields,
    Pipeline,
    parse_aspect_ratio,
    search_declined,
    slide_kind,
    plan_slide_numbers,
    slide_marked_kind,
    slide_prompt_kind,
    split_descriptions,
    text_check_kind,
    user_image_failed_kind,
    user_image_kind,
)
from bot.presets import Preset, PresetStore
from bot.sources import (
    BINARY_TYPES,
    OLD_FORMATS,
    PDF_CONVERTIBLE,
    TEXT_TYPES,
    SourceFile,
    UnreadableFile,
    convert_to_pdf,
    extract_text,
)
from bot.questions import Question, QuestionBank, Survey
from bot.textcheck import TextIssue
from bot.user_images import MAX_CAPTION_CHARS, UserImage, find_area, marker_color, place_image

log = logging.getLogger(__name__)
router = Router()

NEW_PRESENTATION = "new_presentation"
RETRY_STEP = "retry_step"
PLAN_ACCEPT = "plan_accept"
PLAN_EDIT = "plan_edit"
MAX_PLAN_FILE_BYTES = 200_000
WAIT_DESCRIPTION = "description"  # sessions.waiting_for while the bot waits for the free-form description
WAIT_SOURCES = "source_files"  # sessions.waiting_for while the bot collects source files
SOURCES_DONE = "sources_done"  # callback, and the artifact kind that marks the files step as done
MAX_SOURCE_FILES = 10
MAX_SOURCE_FILE_BYTES = 20_000_000  # Telegram doesn't let bots download bigger files
SURVEY_CONFIRM = "survey_confirm"  # also sessions.waiting_for while the summary waits for a button
SURVEY_EDIT = "survey_edit"
SURVEY_SHOW = "survey_show"
SKIP_QUESTION_PREFIX = "skip_question:"  # + question key: «Пропустить / Не знаю» under a question
EDIT_QUESTION_PREFIX = "edit_question:"  # + question key: re-ask this question from the summary
STYLE_REF_SKIP = "style_ref_skip"
STYLE_PRESET_PREFIX = "style_preset:"  # + N: the «Стиль N» button
WAIT_STYLE_REF = "style_ref"  # sessions.waiting_for while the bot waits for a reference image
BACKGROUND_ACCEPT = "background_accept"  # accept the first background
BACKGROUND_EDIT = "background_edit"
# Same value as the old «Перегенерировать» button, so buttons sent before the update still work.
BACKGROUND_REDRAW = "background_regenerate"
BACKGROUND_PICK_PREFIX = "background_pick:"  # + "1" or "2": the background chosen after the change
WAIT_BACKGROUND_COMMENT = "background_comment"  # sessions.waiting_for while the bot waits for the comment
MAX_IMAGE_BYTES = 10_000_000
# Slide requests running at the same time. 1 = one by one: the Vertex trial quota
# answered 429 to 3 parallel requests, and retries only slowed things down.
SLIDE_CONCURRENCY = 1
TEXT_CHECK_CONCURRENCY = 3  # text requests: Vertex has no trouble with a few at once
TELEGRAM_ALBUM_LIMIT = 10
MAX_PRESETS_SHOWN = 10  # one photo message each: more would flood the chat
PDF_FILENAME = "Презентация.pdf"
IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
TELEGRAM_LIMIT = 4096

start_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Новая презентация", callback_data=NEW_PRESENTATION)]]
)
retry_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Повторить", callback_data=RETRY_STEP)]]
)

plan_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Принять", callback_data=PLAN_ACCEPT),
            InlineKeyboardButton(text="Внести правки", callback_data=PLAN_EDIT),
        ]
    ]
)

summary_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Всё верно", callback_data=SURVEY_CONFIRM),
            InlineKeyboardButton(text="Исправить", callback_data=SURVEY_EDIT),
        ]
    ]
)


def skip_keyboard(question: Question) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить / Не знаю", callback_data=f"{SKIP_QUESTION_PREFIX}{question.key}"
                )
            ]
        ]
    )


def edit_question_keyboard(questions: list[Question]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{i}. {q.text}", callback_data=f"{EDIT_QUESTION_PREFIX}{q.key}")]
        for i, q in enumerate(questions, start=1)
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data=SURVEY_SHOW)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


sources_skip_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data=SOURCES_DONE)]]
)
sources_done_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Готово", callback_data=SOURCES_DONE)]]
)

DESCRIPTION_REQUEST = "О чём будет ваша презентация? Опишите в свободной форме."
SOURCES_REQUEST = (
    "Есть ли у вас исходные данные для презентации? Пришлите файлы — PDF, Word, Excel, PowerPoint, "
    f"TXT, CSV или картинки, до {MAX_SOURCE_FILES} файлов, каждый до 20 МБ, — и/или опишите данные текстом. "
    "Когда закончите — нажмите «Готово». Если данных нет — «Пропустить»."
)
LINKS_NOTICE = (
    "Мы пока не поддерживаем поиск по ссылкам: что находится по ссылке, я не увижу. "
    "Если там файл — скачайте его и пришлите мне."
)
# The survey analysis failed: there is no retry button on purpose, the cause is to be found in the logs.
ANALYSIS_FAILED = "Возникли неполадки при анализе ответа. Мы уже разбираемся — попробуйте, пожалуйста, позже."
OUTDATED_SESSION = "Эта презентация начата в старой версии бота. Начните, пожалуйста, новую."
# A link with or without the scheme: «https://…», «www.…», «disk.yandex.ru/…».
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.[^\W\d_]{2,}/", re.IGNORECASE)

# Where the survey of a session stands (see survey_stage).
STAGE_DESCRIPTION = WAIT_DESCRIPTION
STAGE_SOURCES = WAIT_SOURCES
STAGE_ANALYSIS = "analysis"  # files sent, but the analysis is running or has failed
STAGE_QUESTION = "question"  # asking the questions the analysis found no answer to
STAGE_SUMMARY = SURVEY_CONFIRM
STAGE_DONE = "done"  # answers confirmed: the pipeline can run
STAGE_OUTDATED = "outdated"  # started before the survey was reworked: can't be continued

style_ref_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data=STYLE_REF_SKIP)]]
)
background_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data=BACKGROUND_ACCEPT)],
        [
            InlineKeyboardButton(text="Доработать", callback_data=BACKGROUND_EDIT),
            InlineKeyboardButton(text="Нарисовать заново", callback_data=BACKGROUND_REDRAW),
        ],
    ]
)
background_choice_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Вариант 1", callback_data=f"{BACKGROUND_PICK_PREFIX}1"),
            InlineKeyboardButton(text="Вариант 2", callback_data=f"{BACKGROUND_PICK_PREFIX}2"),
        ]
    ]
)
# The second background failed: try again, or keep the first one.
background_retry_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Повторить", callback_data=RETRY_STEP),
            InlineKeyboardButton(text="Принять первый фон", callback_data=BACKGROUND_ACCEPT),
        ]
    ]
)
BACKGROUND_CAPTION = (
    "Фон для слайдов. Если нравится — «Принять». Чтобы поправить детали — «Доработать», "
    "чтобы получить совсем другой вариант — «Нарисовать заново». Изменить фон можно один раз."
)
BACKGROUND_COMMENT_REQUEST = "В свободной форме укажите, что в фоне понравилось и что не понравилось."

# Where the background of a session stands (see background_stage).
BG_OFFERED = "offered"  # the first background waits for «Принять» / «Доработать» / «Нарисовать заново»
BG_COMMENT = "comment"  # «Доработать» pressed, waiting for the comment
BG_PENDING = "pending"  # the second background is being drawn, or drawing it failed
BG_CHOICE = "choice"  # both backgrounds shown, waiting for «Вариант 1» / «Вариант 2»
BG_ACCEPTED = "accepted"
# The user's own pictures for slides, collected after the plan is accepted:
WAIT_SLIDE_IMAGES = "slide_images"  # sessions.waiting_for while the slide buttons are shown
WAIT_SLIDE_PHOTO_PREFIX = "slide_photo:"  # + N: waiting for the photo for slide N
WAIT_CAPTION_PREFIX = "photo_caption:"  # + N: waiting for the caption of slide N's photo
SLIDE_IMAGE_PREFIX = "slide_image:"  # + N: the «Слайд N» button
SLIDE_IMAGES_DONE = "slide_images_done"  # callback, and the artifact kind that marks the step as done
CAPTION_SKIP = "caption_skip"
SLIDE_IMAGES_REQUEST = (
    "План принят. Если хотите вставить свои изображения на слайды, нажмите номер слайда и пришлите фото — "
    "по одному на слайд. Изображение будет вставлено без изменений."
)
caption_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Продолжить без подписи", callback_data=CAPTION_SKIP)]]
)

STYLE_REF_REQUEST = (
    "Теперь дизайн слайдов.\n\n"
    "Если у вас есть картинка, стиль которой нравится (фото, скриншот, слайд из другой презентации), "
    "пришлите её одним изображением — фон сделаю в похожем стиле. Если нет — нажмите «Пропустить»."
)
STYLE_CHOICE_INTRO = "Теперь дизайн слайдов. Ниже — готовые стили: выберите один кнопкой под картинкой."
STYLE_CHOICE_REST = (
    "Не подошло? Пришлите свою картинку, стиль которой нравится (фото, скриншот, слайд из другой "
    "презентации), — фон сделаю в похожем стиле. Если ничего не нужно — нажмите «Пропустить»."
)

# Sessions whose pipeline is running right now: protects from double-clicks and
# from extra messages sent while the model is still working.
_running: set[int] = set()


def markdown_to_telegram(text: str) -> str:
    """Minimal Markdown → Telegram HTML: the model answers in Markdown, Telegram can't render it.

    Converts only what LLMs typically emit (# headings, * / - bullets, **bold**, *italic*);
    everything else is escaped.
    """
    lines = []
    for line in escape(text).splitlines():
        if re.fullmatch(r"\s*[*\-=_]{3,}\s*", line):  # separator line like ********** or ---
            lines.append("———")
            continue
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            line = f"**{heading.group(1).strip('*')}**"
        line = re.sub(r"^(\s*)[*-]\s+", r"\1• ", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", line)
        lines.append(line)
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so each chunk fits into one Telegram message.

    Tags produced by markdown_to_telegram never span lines, so chunks stay valid HTML.
    """
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single giant line: hard cut
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


async def survey_stage(db: Database, survey: Survey, session_id: int) -> str:
    """Where the session's survey stands, derived from the DB, so it survives a bot restart."""
    waiting_for = await db.get_waiting_for(session_id)
    if waiting_for in (STAGE_DESCRIPTION, STAGE_SOURCES, STAGE_SUMMARY):
        return waiting_for
    artifacts = await db.get_artifacts(session_id)
    if ASKED_QUESTIONS in artifacts:
        if await current_question(db, survey, session_id) is not None:
            return STAGE_QUESTION
        return STAGE_DONE if SURVEY_CONFIRMED in artifacts else STAGE_SUMMARY
    if SOURCES_DONE in artifacts:
        return STAGE_ANALYSIS
    if TZ.kind in artifacts:  # an older session past its survey
        return STAGE_DONE
    return STAGE_OUTDATED


def asked_questions(survey: Survey, artifacts: dict[str, str]) -> list[Question]:
    """Questions the analysis found no answer to, in survey order. Questions removed from
    questions.yaml since then are dropped."""
    keys = set(json.loads(artifacts.get(ASKED_QUESTIONS) or "[]"))
    return [q for q in survey.questions if q.key in keys]


def next_question(questions: list[Question], answers: dict[str, str]) -> tuple[int, Question] | None:
    """(1-based number, question) of the first unanswered question; None if all are answered."""
    for number, q in enumerate(questions, start=1):
        if q.key not in answers:
            return number, q
    return None


async def current_question(db: Database, survey: Survey, session_id: int) -> tuple[int, Question] | None:
    """The question the bot is waiting an answer to, or None if it isn't asking one now."""
    if await db.get_waiting_for(session_id) is not None:
        return None
    artifacts = await db.get_artifacts(session_id)
    if ASKED_QUESTIONS not in artifacts:
        return None
    return next_question(asked_questions(survey, artifacts), await db.get_answers(session_id))


async def ask_description(message: Message, db: Database, session_id: int) -> None:
    await db.set_waiting_for(session_id, WAIT_DESCRIPTION)
    await message.answer(DESCRIPTION_REQUEST)


async def ask_next_question(message: Message, db: Database, bank: QuestionBank, session_id: int) -> None:
    """Ask the next unanswered question with a counter, or show the summary when all are answered."""
    survey = bank.load()
    questions = asked_questions(survey, await db.get_artifacts(session_id))
    current = next_question(questions, await db.get_answers(session_id))
    if current is None:
        await show_summary(message, db, bank, session_id)
        return
    number, q = current
    await message.answer(f"<b>Вопрос {number}/{len(questions)}</b>\n{escape(q.text)}", reply_markup=skip_keyboard(q))


async def show_summary(message: Message, db: Database, bank: QuestionBank, session_id: int) -> None:
    """The asked questions with the user's answers, for a final check before the pipeline runs.
    Answers the analysis found are not shown: the user didn't type them."""
    await db.set_waiting_for(session_id, SURVEY_CONFIRM)
    questions = asked_questions(bank.load(), await db.get_artifacts(session_id))
    answers = await db.get_answers(session_id)
    blocks = ["<b>Проверьте ответы</b>"]
    for number, q in enumerate(questions, start=1):
        answer = answers.get(q.key, "—")
        shown = "Пропущено" if answer == SKIPPED_ANSWER else answer
        blocks.append(f"{number}. {escape(q.text)}\n<i>{escape(shown)}</i>")
    blocks.append("Если всё верно — нажмите «Всё верно». Чтобы изменить ответ — «Исправить».")
    chunks = split_message("\n\n".join(blocks))
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=summary_keyboard)


async def resume_survey(
    message: Message, db: Database, bank: QuestionBank, session_id: int, stage: str
) -> None:
    """Repeat what the bot is waiting for at this survey stage (any stage but STAGE_DONE)."""
    if stage == STAGE_DESCRIPTION:
        await message.answer(DESCRIPTION_REQUEST)
    elif stage == STAGE_SOURCES:
        await ask_source_files(message, db, session_id)
    elif stage == STAGE_QUESTION:
        await ask_next_question(message, db, bank, session_id)
    elif stage == STAGE_SUMMARY:
        await show_summary(message, db, bank, session_id)
    elif stage == STAGE_ANALYSIS and session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
    elif stage == STAGE_ANALYSIS:
        await message.answer(ANALYSIS_FAILED, reply_markup=start_keyboard)
    else:
        await message.answer(OUTDATED_SESSION, reply_markup=start_keyboard)


async def ask_source_files(message: Message, db: Database, session_id: int) -> None:
    await db.set_waiting_for(session_id, WAIT_SOURCES)
    await message.answer(SOURCES_REQUEST, reply_markup=sources_skip_keyboard)


async def run_analysis(
    message: Message, db: Database, bank: QuestionBank, pipeline: Pipeline, session_id: int
) -> None:
    """Find answers to the survey in the description and source data, then ask the rest one by one.
    If everything is answered, the pipeline starts right away."""
    if session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
        return
    _running.add(session_id)
    try:
        await message.answer("Изучаю ваше описание и исходные данные…")
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        survey = bank.load()
        artifacts = await db.get_artifacts(session_id)
        sources = await db.get_source_files(session_id)
        try:
            found, model = await pipeline.analyze_survey(
                survey, artifacts.get(DESCRIPTION, ""), artifacts.get(SOURCES_NOTE), sources
            )
        except LLMError as e:
            log.error("Survey analysis failed for session %d: %s", session_id, e)
            await message.answer(ANALYSIS_FAILED, reply_markup=start_keyboard)
            return
        log.info("Survey analysis for session %d found answers to: %s", session_id, sorted(found))
        for q in survey.questions:
            if q.key in found:
                await db.save_answer(session_id, q.key, q.text, found[q.key])
        missing = [q.key for q in survey.questions if q.key not in found]
        await db.save_artifact(session_id, ASKED_QUESTIONS, json.dumps(missing, ensure_ascii=False), model)
        if missing:
            await ask_next_question(message, db, bank, session_id)
            return
        await db.save_artifact(session_id, SURVEY_CONFIRMED, "", model="auto")
    finally:
        _running.discard(session_id)
    await run_pipeline(message, db, bank, pipeline, session_id)


async def notice_links(message: Message) -> None:
    if _LINK.search(message.text or ""):
        await message.answer(LINKS_NOTICE)


# Telegram delivers files sent together as separate updates handled concurrently;
# the lock keeps the 10-file limit and the numbering exact.
_source_files_lock = asyncio.Lock()


async def receive_source_file(
    message: Message,
    db: Database,
    files_dir: Path,
    session_id: int,
    file,
    name: str | None,
    mime_type: str | None,
    soffice: str | None = None,
) -> None:
    """Validate, download and save one source file; it is converted right away (Word and
    PowerPoint to PDF, other text formats to text), so a broken file is reported now, not
    when the pipeline runs. name=None: a Telegram photo, which has no file name; it is
    called «Фото_N.jpg». soffice: LibreOffice binary, None if it is not installed."""
    suffix = Path(name).suffix.lower() if name else ".jpg"
    convertible = soffice is not None and suffix in PDF_CONVERTIBLE
    if suffix in OLD_FORMATS and not convertible:
        await message.answer(_old_format_text(name))
        return
    if suffix not in BINARY_TYPES and suffix not in TEXT_TYPES and not convertible:
        await message.answer(
            f"Файл «{escape(name)}» не подходит. Принимаю PDF, DOCX, XLSX, PPTX, TXT, CSV, JPG, PNG и WebP."
        )
        return
    if (file.file_size or 0) > MAX_SOURCE_FILE_BYTES:
        await message.answer(f"Файл «{escape(name or 'фото')}» больше 20 МБ — такие Telegram не даёт боту скачать.")
        return
    async with _source_files_lock:
        count = len(await db.get_source_files(session_id))
        if count >= MAX_SOURCE_FILES:
            await message.answer(
                f"Уже получено {MAX_SOURCE_FILES} файлов — больше принять не могу. Нажмите «Готово».",
                reply_markup=sources_done_keyboard,
            )
            return
        name = name or f"Фото_{count + 1}.jpg"
        folder = files_dir / str(session_id) / "sources"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{count + 1:02d}{suffix}"  # original names may clash or be unsafe on disk
        path.write_bytes((await message.bot.download(file)).read())
        mime_type = mime_type or BINARY_TYPES.get(suffix, "")
        text = None
        if convertible:
            pdf = await _to_pdf(soffice, path, name)
            if pdf is not None:
                path.unlink()
                path, mime_type = pdf, BINARY_TYPES[".pdf"]
            elif suffix in OLD_FORMATS:  # no text reader for .doc/.ppt: nothing to fall back to
                path.unlink()
                await message.answer(_old_format_text(name))
                return
        if path.suffix in TEXT_TYPES:
            try:
                text = await asyncio.to_thread(extract_text, path)
            except UnreadableFile as e:
                log.warning("Unreadable source file %s: %s", name, e)
                text = ""
            if not text:
                path.unlink()
                await message.answer(f"Не удалось прочитать текст из файла «{escape(name)}». Проверьте файл.")
                return
        await db.add_source_file(session_id, SourceFile(name, path, mime_type, text))
    await message.answer(
        f"Получен файл «{escape(name)}» ({count + 1} из {MAX_SOURCE_FILES}). "
        "Пришлите ещё или нажмите «Готово».",
        reply_markup=sources_done_keyboard,
    )


def _old_format_text(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return (
        f"Формат {escape(suffix)} не поддерживается. Пересохраните файл «{escape(name)}» "
        f"в {OLD_FORMATS[suffix]} и пришлите снова."
    )


async def _to_pdf(soffice: str, path: Path, name: str) -> Path | None:
    """PDF made from a Word/PowerPoint file, so the model sees its pictures and charts;
    None if conversion failed — the caller then falls back to the file's text."""
    try:
        pdf = await convert_to_pdf(soffice, path)
    except UnreadableFile as e:
        log.warning("Could not convert %s to PDF: %s", name, e)
        return None
    if pdf.stat().st_size > MAX_SOURCE_FILE_BYTES:
        log.warning("PDF made from %s is too big (%d bytes): using its text", name, pdf.stat().st_size)
        pdf.unlink()
        return None
    return pdf


async def run_pipeline(
    message: Message, db: Database, bank: QuestionBank, pipeline: Pipeline, session_id: int
) -> None:
    """Run every pipeline step that has no saved result yet, in order, then show the plan.

    Intermediate results are not shown to the user. They are saved after each step,
    so after a failure the retry continues from the failed step instead of starting over.
    """
    if session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
        return
    _running.add(session_id)
    try:
        survey = bank.load()
        answers = await db.get_answers(session_id)
        for number, step in enumerate(STEPS, start=1):
            if await db.get_artifact(session_id, step.kind) is not None:
                continue
            await message.answer(f"Шаг {number} из {len(STEPS)}: {step.title.lower()}. Это может занять минуту…")
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

            if step is DATA and await db.get_artifact(session_id, SOURCES) is None:
                if search_declined(answers.get(RESEARCH_ANSWER_KEY)):
                    # Empty sources: step 02 is told there is no internet access.
                    log.info("Web search skipped for session %d: the user said it is not needed", session_id)
                    await db.save_artifact(session_id, SOURCES, "")
                else:
                    await message.answer("Ищу информацию в открытых источниках…")
                    tz = await db.get_artifact(session_id, TZ.kind)
                    await db.save_artifact(session_id, SOURCES, await pipeline.find_sources(tz))

            artifacts = await db.get_artifacts(session_id)
            try:
                sources = await db.get_source_files(session_id)
                result, model = await pipeline.run_step(step, survey, answers, artifacts, sources)
            except LLMError as e:
                log.error("Step %s failed for session %d: %s", step.kind, session_id, e)
                if e.quota_exceeded:
                    text = "Лимит бесплатных запросов к нейросети исчерпан. Прогресс сохранён — попробуйте позже."
                else:
                    text = "Нейросеть сейчас не отвечает. Прогресс сохранён — попробуйте ещё раз."
                await message.answer(text, reply_markup=retry_keyboard)
                return

            await db.save_artifact(session_id, step.kind, result, model)

        await show_plan(message, await db.get_artifact(session_id, PLAN.kind))
    finally:
        _running.discard(session_id)


def slide_images_keyboard(numbers: list[int], with_image: set[int]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"✓ Слайд {n}" if n in with_image else f"Слайд {n}", callback_data=f"{SLIDE_IMAGE_PREFIX}{n}"
        )
        for n in numbers
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    done = "Готово" if with_image else "Продолжить без фото"
    rows.append([InlineKeyboardButton(text=done, callback_data=SLIDE_IMAGES_DONE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def load_user_images(db: Database, session_id: int) -> dict[int, UserImage]:
    """The user's pictures by slide number."""
    artifacts = await db.get_artifacts(session_id)
    images = {}
    for number in range(1, MAX_SLIDES + 1):
        if user_image_kind(number) in artifacts:
            images[number] = UserImage.from_json(artifacts[user_image_kind(number)])
    return images


async def show_slide_images(
    message: Message, db: Database, presets: PresetStore, session_id: int, text: str
) -> None:
    """The «Слайд N» buttons; a plan without «СЛАЙД N» lines goes straight to the style reference."""
    numbers = plan_slide_numbers(await db.get_artifact(session_id, PLAN.kind) or "")[:MAX_SLIDES]
    if not numbers:
        await finish_slide_images(message, db, presets, session_id)
        return
    await db.set_waiting_for(session_id, WAIT_SLIDE_IMAGES)
    with_image = set(await load_user_images(db, session_id))
    await message.answer(text, reply_markup=slide_images_keyboard(numbers, with_image))


async def finish_slide_images(message: Message, db: Database, presets: PresetStore, session_id: int) -> None:
    await db.save_artifact(session_id, SLIDE_IMAGES_DONE, "", model="user")
    await db.set_waiting_for(session_id, WAIT_STYLE_REF)
    await show_style_choice(message, presets.load())


def style_preset_keyboard(preset: Preset) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text=f"Стиль {preset.number}", callback_data=f"{STYLE_PRESET_PREFIX}{preset.number}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def show_style_choice(message: Message, presets: list[Preset]) -> None:
    """Each prepared design as its own preview with the «Стиль N» button under it, then the
    request for a reference with «Пропустить». Without prepared designs: only that request."""
    if not presets:
        await message.answer(STYLE_REF_REQUEST, reply_markup=style_ref_keyboard)
        return
    await message.answer(STYLE_CHOICE_INTRO)
    for preset in presets[:MAX_PRESETS_SHOWN]:
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
        await message.answer_photo(FSInputFile(preset.preview), reply_markup=style_preset_keyboard(preset))
    await message.answer(STYLE_CHOICE_REST, reply_markup=style_ref_keyboard)


def waiting_number(waiting_for: str | None, prefix: str) -> int | None:
    """N from a waiting_for value like "slide_photo:N", or None if it has another prefix."""
    if waiting_for and waiting_for.startswith(prefix):
        return int(waiting_for.removeprefix(prefix))
    return None


def in_slide_images_step(waiting_for: str | None) -> bool:
    return waiting_for == WAIT_SLIDE_IMAGES or (
        waiting_for is not None and waiting_for.startswith((WAIT_SLIDE_PHOTO_PREFIX, WAIT_CAPTION_PREFIX))
    )


async def receive_slide_photo(message: Message, db: Database, files_dir: Path, session_id: int, number: int) -> None:
    """Save the photo for slide N (its Telegram caption is ignored) and ask for a caption."""
    photo = message.photo[-1]  # Telegram sends several sizes; the last one is the largest
    if (photo.file_size or 0) > MAX_IMAGE_BYTES:
        await message.answer("Фото слишком большое (больше 10 МБ). Пришлите поменьше.")
        return
    data = (await message.bot.download(photo)).read()
    path = save_image(files_dir, session_id, f"Фото_слайд_{number:02d}", Image(data, "image/jpeg"))
    image = UserImage(str(path), photo.width, photo.height)
    await db.save_artifact(session_id, user_image_kind(number), image.to_json(), model="user")
    await db.set_waiting_for(session_id, f"{WAIT_CAPTION_PREFIX}{number}")
    await message.answer(
        f"Фото для слайда {number} получено. Если хотите добавить подпись, пришлите её следующим сообщением "
        f"(до {MAX_CAPTION_CHARS} символов).",
        reply_markup=caption_keyboard,
    )


async def show_plan(message: Message, plan: str) -> None:
    """Plan as text to read and as a file to edit; the buttons go on the file message."""
    await message.answer(f"<b>{escape(PLAN.title)}</b>")
    for chunk in split_message(markdown_to_telegram(plan)):
        await message.answer(chunk)
    await message.answer_document(
        BufferedInputFile(plan.encode("utf-8"), filename=PLAN.filename),
        caption="Если план устраивает — «Принять». Если нет — «Внести правки».",
        reply_markup=plan_keyboard,
    )


async def session_with_plan(callback: CallbackQuery, db: Database) -> int | None:
    """Active session whose plan is waiting for «Принять» / «Внести правки»,
    or None (stale button) after telling the user."""
    session_id = await db.get_active_session(callback.from_user.id)
    if (
        session_id is None
        or await db.get_artifact(session_id, PLAN.kind) is None
        or await db.get_artifact(session_id, STYLE_REF) is not None  # plan already accepted
        or await db.get_artifact(session_id, SLIDE_IMAGES_DONE) is not None  # plan already accepted
        or await db.get_waiting_for(session_id) == WAIT_STYLE_REF
        or in_slide_images_step(await db.get_waiting_for(session_id))
    ):
        await callback.message.answer("Этот план уже неактуален.", reply_markup=start_keyboard)
        return None
    return session_id


async def read_plan_file(message: Message) -> str | None:
    """Text of an uploaded .txt plan, or None after telling the user what's wrong."""
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await message.answer("Пришлите план файлом .txt.")
        return None
    if (doc.file_size or 0) > MAX_PLAN_FILE_BYTES:
        await message.answer("Файл слишком большой для плана.")
        return None
    raw = (await message.bot.download(doc)).read()
    for encoding in ("utf-8-sig", "cp1251"):  # Windows Notepad may still save in cp1251
        try:
            text = raw.decode(encoding).strip()
            break
        except UnicodeDecodeError:
            continue
    else:
        await message.answer("Не удалось прочитать файл. Сохраните его в кодировке UTF-8.")
        return None
    if not text:
        await message.answer("Файл пустой. Пришлите исправленный план.")
        return None
    return text


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


async def receive_style_ref(
    message: Message, db: Database, pipeline: Pipeline, files_dir: Path, session_id: int, file, mime_type: str
) -> None:
    """Save the uploaded reference image (a photo or an image document) and start the background."""
    if (file.file_size or 0) > MAX_IMAGE_BYTES:
        await message.answer("Картинка слишком большая (больше 10 МБ). Пришлите поменьше.")
        return
    data = (await message.bot.download(file)).read()
    path = save_image(files_dir, session_id, "Референс", Image(data, mime_type))
    await db.save_artifact(session_id, STYLE_REF, str(path), model="user")
    await db.set_waiting_for(session_id, None)
    await message.answer("Референс получен.")
    await run_design(message, db, pipeline, files_dir, session_id)


async def run_design(message: Message, db: Database, pipeline: Pipeline, files_dir: Path, session_id: int) -> None:
    """Generate the first background (prompt 04) and show it with «Принять» / «Доработать» /
    «Нарисовать заново». The prompt fields are filled from the ТЗ once and reused on retry."""
    if session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
        return
    _running.add(session_id)
    try:
        artifacts = await db.get_artifacts(session_id)
        reference = load_image(artifacts[STYLE_REF]) if artifacts.get(STYLE_REF) else None
        await message.answer("Рисую фон для слайдов. Это может занять до минуты…")
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
        try:
            if BACKGROUND_FIELDS in artifacts:
                fields = BackgroundFields.model_validate_json(artifacts[BACKGROUND_FIELDS])
            else:
                fields = await pipeline.background_fields(artifacts[TZ.kind], has_reference=reference is not None)
                await db.save_artifact(session_id, BACKGROUND_FIELDS, fields.model_dump_json())
            image, model = await pipeline.background(fields, await session_ratio(db, session_id), reference)
        except LLMError as e:
            log.error("Background failed for session %d: %s", session_id, e)
            await message.answer(
                "Не получилось нарисовать фон: нейросеть не ответила. Попробуйте ещё раз.",
                reply_markup=retry_keyboard,
            )
            return
        path = save_image(files_dir, session_id, "Фон", image)
        await db.save_artifact(session_id, BACKGROUND, str(path), model)
        await message.answer_photo(
            BufferedInputFile(image.data, filename=path.name),
            caption=BACKGROUND_CAPTION,
            reply_markup=background_keyboard,
        )
    finally:
        _running.discard(session_id)


def background_stage(artifacts: dict[str, str], waiting_for: str | None) -> str | None:
    """Where the session's background stands; None if there is no background yet."""
    if BACKGROUND not in artifacts:
        return None
    if BACKGROUND_ACCEPTED in artifacts:
        return BG_ACCEPTED
    if BACKGROUND_ALT in artifacts:
        return BG_CHOICE
    if waiting_for == WAIT_BACKGROUND_COMMENT:
        return BG_COMMENT
    if BACKGROUND_ATTEMPT in artifacts:
        return BG_PENDING
    return BG_OFFERED


def chosen_background(artifacts: dict[str, str]) -> str:
    """Path to the background the slides are drawn on."""
    return artifacts[BACKGROUND_ALT] if artifacts.get(BACKGROUND_CHOICE) == "2" else artifacts[BACKGROUND]


async def run_background_change(
    message: Message, db: Database, pipeline: Pipeline, files_dir: Path, session_id: int
) -> None:
    """The one change of the background: fix the first one by the user's comment («Доработать»)
    or draw a different one («Нарисовать заново»); then show both to choose from.

    «Нарисовать заново» without a reference picks new colors and style too; with a reference
    the style stays the reference's, and only the composition and elements change.
    """
    if session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
        return
    _running.add(session_id)
    try:
        artifacts = await db.get_artifacts(session_id)
        attempt = artifacts[BACKGROUND_ATTEMPT]
        ratio = await session_ratio(db, session_id)
        first = load_image(artifacts[BACKGROUND])
        if attempt == ATTEMPT_EDIT:
            await message.answer("Дорабатываю фон. Это может занять до минуты…")
        else:
            await message.answer("Рисую новый фон. Это может занять до минуты…")
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
        try:
            if attempt == ATTEMPT_EDIT:
                image, model = await pipeline.edit_background(first, artifacts[BACKGROUND_COMMENT], ratio)
            else:
                reference = load_image(artifacts[STYLE_REF]) if artifacts.get(STYLE_REF) else None
                fields = BackgroundFields.model_validate_json(artifacts[BACKGROUND_FIELDS])
                if reference is None:
                    if BACKGROUND_ALT_FIELDS in artifacts:
                        fields = BackgroundFields.model_validate_json(artifacts[BACKGROUND_ALT_FIELDS])
                    else:
                        fields = await pipeline.background_fields(artifacts[TZ.kind], False, previous=fields)
                        await db.save_artifact(session_id, BACKGROUND_ALT_FIELDS, fields.model_dump_json())
                image, model = await pipeline.background(fields, ratio, reference, previous=first)
        except LLMError as e:
            log.error("Background change (%s) failed for session %d: %s", attempt, session_id, e)
            await message.answer(
                "Не получилось нарисовать фон: нейросеть не ответила. Попробуйте ещё раз или примите первый фон.",
                reply_markup=background_retry_keyboard,
            )
            return
        path = save_image(files_dir, session_id, "Фон_2", image)
        await db.save_artifact(session_id, BACKGROUND_ALT, str(path), model)
        await show_background_choice(message, first, image, attempt)
    finally:
        _running.discard(session_id)


async def show_background_choice(message: Message, first: Image, second: Image, attempt: str) -> None:
    second_title = "доработанный" if attempt == ATTEMPT_EDIT else "нарисованный заново"
    await message.answer_media_group(
        [
            InputMediaPhoto(media=BufferedInputFile(first.data, "Фон_1.png"), caption="Вариант 1 — исходный"),
            InputMediaPhoto(media=BufferedInputFile(second.data, "Фон_2.png"), caption=f"Вариант 2 — {second_title}"),
        ]
    )
    await message.answer("Какой фон берём для слайдов?", reply_markup=background_choice_keyboard)


async def run_slides(
    message: Message, db: Database, pipeline: Pipeline, files_dir: Path, session_id: int
) -> None:
    """Everything after the background is accepted, without stops: style (05), slide
    descriptions (06), slide images (07), then the album and the PDF.

    Each result is saved as soon as it's ready, so a retry continues where it stopped:
    finished slides are not drawn again.
    """
    if session_id in _running:
        await message.answer("Работа уже идёт, подождите немного.")
        return
    _running.add(session_id)
    try:
        artifacts = await db.get_artifacts(session_id)
        ratio = await session_ratio(db, session_id)
        background = load_image(chosen_background(artifacts))
        try:
            if STYLE not in artifacts:
                await message.answer("Готовлю стиль оформления слайдов…")
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                style, model = await pipeline.style(background, ratio)
                await db.save_artifact(session_id, STYLE, style, model)
                artifacts[STYLE] = style
            if DESCRIPTIONS not in artifacts:
                await message.answer("Продумываю, как показать содержимое каждого слайда…")
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
                images = await load_user_images(db, session_id)
                descriptions, model = await pipeline.descriptions(artifacts[PLAN.kind], images)
                await db.save_artifact(session_id, DESCRIPTIONS, descriptions, model)
                artifacts[DESCRIPTIONS] = descriptions
        except LLMError as e:
            log.error("Slides preparation failed for session %d: %s", session_id, e)
            await message.answer(
                "Нейросеть сейчас не отвечает. Прогресс сохранён — попробуйте ещё раз.",
                reply_markup=retry_keyboard,
            )
            return

        blocks = split_descriptions(artifacts[DESCRIPTIONS])
        if len(blocks) > MAX_SLIDES:
            await message.answer(
                f"В плане {len(blocks)} слайдов, а в демо-версии можно не больше {MAX_SLIDES}. "
                f"Рисую первые {MAX_SLIDES}."
            )
            blocks = dict(list(blocks.items())[:MAX_SLIDES])

        failed = await draw_slides(message, db, pipeline, files_dir, session_id, blocks, ratio, background)
        if failed:
            await message.answer(
                "Не получилось нарисовать слайды: " + ", ".join(map(str, failed)) + ". "
                "Готовые слайды сохранены — «Повторить» дорисует только недостающие.",
                reply_markup=retry_keyboard,
            )
            return

        report = await check_slides_text(message, db, pipeline, session_id, blocks)
        artifacts = await db.get_artifacts(session_id)
        paths = [Path(artifacts[slide_kind(n)]) for n in blocks]
        await send_slides(message, paths)
        await db.finish_session(session_id)
        no_picture = [str(n) for n in blocks if user_image_failed_kind(n) in artifacts]
        if no_picture:
            report += (
                "\n\nНе удалось вставить ваши изображения на слайды: " + ", ".join(no_picture) + ". "
                "Эти слайды нарисованы без них."
            )
        await message.answer(report)
        await message.answer("Презентация готова!", reply_markup=start_keyboard)
    finally:
        _running.discard(session_id)


def sample_slide(blocks: dict[int, str], with_picture: set[int] = frozenset()) -> int | None:
    """Slide whose design the others copy: the second one, as slide 1 is the title slide
    (prompt 03-1) and its large centered heading doesn't fit content slides. A slide with the
    user's picture is skipped, or the model would draw similar pictures on the others;
    None if every slide has one."""
    numbers = list(blocks)
    candidates = [n for n in numbers[1:] + numbers[:1] if n not in with_picture]
    return candidates[0] if candidates else None


async def draw_slides(
    message: Message,
    db: Database,
    pipeline: Pipeline,
    files_dir: Path,
    session_id: int,
    blocks: dict[int, str],
    ratio: str,
    background: Image,
) -> list[int]:
    """Draw the slides that have no saved image yet. Returns the numbers that failed.

    The sample slide (see sample_slide) is drawn alone and then attached to the others as
    a design sample; the others follow, SLIDE_CONCURRENCY at a time.
    """
    artifacts = await db.get_artifacts(session_id)
    style = artifacts[STYLE]
    missing = [n for n in blocks if slide_kind(n) not in artifacts]
    if not missing:
        return []
    done = len(blocks) - len(missing)
    status = await message.answer(f"Рисую слайды. Готово: {done} из {len(blocks)}…")
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    failed: list[int] = []

    images = await load_user_images(db, session_id)
    marker = marker_color(style)

    async def render(number: int, sample: Image | None, picture: UserImage | None, area: bool) -> tuple[Image, str]:
        prompt = pipeline.slide_prompt(
            number,
            blocks[number],
            style,
            ratio,
            with_sample=sample is not None,
            image=picture,
            marker=marker if area else None,
        )
        # Saved before the request: if the slide comes out wrong, the prompt shows where the problem came from.
        await db.save_artifact(session_id, slide_prompt_kind(number), prompt)
        return await pipeline.slide(prompt, ratio, background, sample)

    async def draw_with_picture(number: int, sample: Image | None, picture: UserImage) -> tuple[Path, Image, str]:
        """The model leaves a marker-colored area, the picture is pasted into it. Two tries;
        if the area doesn't come out, the slide is drawn without it."""
        for attempt in (1, 2):
            image, model = await render(number, sample, picture, area=True)
            box = await asyncio.to_thread(find_area, image.data, marker)
            if box is not None:
                marked = save_image(files_dir, session_id, f"Слайд_{number:02d}_разметка", image)
                await db.save_artifact(session_id, slide_marked_kind(number), str(marked), model)
                picture_data = Path(picture.path).read_bytes()
                data = await asyncio.to_thread(place_image, image.data, box, picture_data, style)
                final = Image(data, "image/jpeg")
                return save_image(files_dir, session_id, f"Слайд_{number:02d}", final), final, model
            log.warning("No area for the user's picture: session %d, slide %d, try %d", session_id, number, attempt)
        image, model = await render(number, sample, picture, area=False)
        await db.save_artifact(session_id, user_image_failed_kind(number), "")
        return save_image(files_dir, session_id, f"Слайд_{number:02d}", image), image, model

    async def draw(number: int, sample: Image | None) -> Image | None:
        nonlocal done
        try:
            if number in images:
                path, image, model = await draw_with_picture(number, sample, images[number])
            else:
                image, model = await render(number, sample, None, area=False)
                path = save_image(files_dir, session_id, f"Слайд_{number:02d}", image)
        except LLMError as e:
            log.error("Slide %d failed for session %d: %s", number, session_id, e)
            failed.append(number)
            return None
        await db.save_artifact(session_id, slide_kind(number), str(path), model)
        done += 1
        with suppress(TelegramBadRequest):  # e.g. "message is not modified"
            await status.edit_text(f"Рисую слайды. Готово: {done} из {len(blocks)}…")
        return image

    sample_number = sample_slide(blocks, set(images))
    if sample_number is None:
        sample = None
    elif sample_number in missing:
        sample = await draw(sample_number, None)
        if sample is None:
            return failed  # without the sample the others would look different: stop here
        missing.remove(sample_number)
    else:
        sample = load_image(artifacts[slide_kind(sample_number)])

    semaphore = asyncio.Semaphore(SLIDE_CONCURRENCY)

    async def draw_limited(number: int) -> None:
        async with semaphore:
            await draw(number, sample)

    await asyncio.gather(*(draw_limited(n) for n in missing))
    return sorted(failed)


async def check_slides_text(
    message: Message, db: Database, pipeline: Pipeline, session_id: int, blocks: dict[int, str]
) -> str:
    """Compare the text drawn on every slide with its description (prompt 08) and return
    the report for the user. Results are saved, so a retry doesn't check finished slides
    again; a slide whose check failed is listed as unchecked, it doesn't stop the presentation."""
    artifacts = await db.get_artifacts(session_id)
    unchecked = [n for n in blocks if text_check_kind(n) not in artifacts]
    if unchecked:
        await message.answer("Проверяю текст на слайдах…")
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    semaphore = asyncio.Semaphore(TEXT_CHECK_CONCURRENCY)

    async def check(number: int) -> None:
        async with semaphore:
            try:
                # A slide with the user's picture is checked as drawn, before the picture went in:
                # text on the picture is not the slide's text.
                slide = load_image(artifacts.get(slide_marked_kind(number)) or artifacts[slide_kind(number)])
                issues, model = await pipeline.check_slide_text(slide, blocks[number])
            except LLMError as e:
                log.warning("Text check of slide %d failed for session %d: %s", number, session_id, e)
                return
            except Exception:  # the check is optional: whatever breaks, the slides are still sent
                log.exception("Text check of slide %d crashed for session %d", number, session_id)
                return
        for issue in issues:
            log.info("Text check: session %d, slide %d: %s", session_id, number, issue.describe())
        pairs = [[issue.expected, issue.found] for issue in issues]
        await db.save_artifact(session_id, text_check_kind(number), json.dumps(pairs, ensure_ascii=False), model)

    await asyncio.gather(*(check(n) for n in unchecked))
    return text_check_report(list(blocks), await db.get_artifacts(session_id))


def text_check_report(numbers: list[int], artifacts: dict[str, str]) -> str:
    lines, unchecked = [], []
    for number in numbers:
        saved = artifacts.get(text_check_kind(number))
        if saved is None:
            unchecked.append(number)
            continue
        for expected, found in json.loads(saved):
            lines.append(f"• Слайд {number}: {escape(TextIssue(expected, found).describe())}")
    if lines:
        text = "Проверка текста на слайдах нашла возможные ошибки:\n" + "\n".join(lines)
    else:
        text = "Проверка текста на слайдах: ошибок не найдено."
    if unchecked:
        text += "\n\nНе удалось проверить слайды: " + ", ".join(map(str, unchecked)) + "."
    return text


async def send_slides(message: Message, paths: list[Path]) -> None:
    """Slides as photo albums to look through, then the PDF in full quality."""
    for i in range(0, len(paths), TELEGRAM_ALBUM_LIMIT):
        chunk = paths[i : i + TELEGRAM_ALBUM_LIMIT]
        if len(chunk) == 1:  # an album needs at least 2 items
            await message.answer_photo(BufferedInputFile(chunk[0].read_bytes(), filename=chunk[0].name))
            continue
        await message.answer_media_group(
            [InputMediaPhoto(media=BufferedInputFile(p.read_bytes(), filename=p.name)) for p in chunk]
        )
    pdf = await asyncio.to_thread(build_pdf, paths)
    await message.answer_document(BufferedInputFile(pdf, filename=PDF_FILENAME))


async def session_at_background(callback: CallbackQuery, db: Database, stages: set[str]) -> int | None:
    """Active session whose background is at one of `stages`, or None (stale button) after telling the user."""
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is not None:
        stage = background_stage(await db.get_artifacts(session_id), await db.get_waiting_for(session_id))
        if stage in stages:
            return session_id
    await callback.message.answer("Эта кнопка уже неактуальна.")
    return None


async def ack(callback: CallbackQuery) -> None:
    """Confirm a button press (stops the spinner on the button). Telegram accepts it only
    within a short time; a press delivered late (the bot was asleep, offline or restarting)
    must still be handled, so a refused confirmation is ignored."""
    with suppress(TelegramBadRequest):
        await callback.answer()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я помогу собрать растровую презентацию по методологии PowerGuide.\n"
        "Сначала расскажите о презентации, а я уточню то, чего не хватает.",
        reply_markup=start_keyboard,
    )


@router.callback_query(F.data == NEW_PRESENTATION)
async def on_new_presentation(callback: CallbackQuery, db: Database, bank: QuestionBank) -> None:
    session_id = await db.start_session(callback.from_user.id)
    await ack(callback)
    await ask_description(callback.message, db, session_id)


@router.callback_query(F.data == RETRY_STEP)
async def on_retry_step(
    callback: CallbackQuery, db: Database, bank: QuestionBank, pipeline: Pipeline, files_dir: Path
) -> None:
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None:
        await callback.message.answer("Активной презентации нет.", reply_markup=start_keyboard)
        return
    stage = await survey_stage(db, bank.load(), session_id)
    if stage != STAGE_DONE:
        await resume_survey(callback.message, db, bank, session_id, stage)
        return
    artifacts = await db.get_artifacts(session_id)
    if BACKGROUND_ACCEPTED in artifacts:
        await run_slides(callback.message, db, pipeline, files_dir, session_id)
    elif STYLE_REF in artifacts and BACKGROUND not in artifacts:
        await run_design(callback.message, db, pipeline, files_dir, session_id)
    elif background_stage(artifacts, await db.get_waiting_for(session_id)) == BG_PENDING:
        await run_background_change(callback.message, db, pipeline, files_dir, session_id)
    elif STYLE_REF not in artifacts and await db.get_waiting_for(session_id) is None:
        await run_pipeline(callback.message, db, bank, pipeline, session_id)
    else:
        await callback.message.answer("Повторять нечего — продолжайте с последнего сообщения бота.")


async def session_with_summary(callback: CallbackQuery, db: Database) -> int | None:
    """Active session whose answer summary waits for a button, or None (stale button)."""
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None or await db.get_waiting_for(session_id) != SURVEY_CONFIRM:
        await callback.message.answer("Это сообщение уже неактуально.", reply_markup=start_keyboard)
        return None
    return session_id


@router.callback_query(F.data == SURVEY_CONFIRM)
async def on_survey_confirm(
    callback: CallbackQuery, db: Database, bank: QuestionBank, pipeline: Pipeline
) -> None:
    await ack(callback)
    session_id = await session_with_summary(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, SURVEY_CONFIRMED, "", model="user")
    await db.set_waiting_for(session_id, None)
    await run_pipeline(callback.message, db, bank, pipeline, session_id)


@router.callback_query(F.data == SURVEY_EDIT)
async def on_survey_edit(callback: CallbackQuery, db: Database, bank: QuestionBank) -> None:
    await ack(callback)
    session_id = await session_with_summary(callback, db)
    if session_id is None:
        return
    questions = asked_questions(bank.load(), await db.get_artifacts(session_id))
    await callback.message.edit_reply_markup(reply_markup=edit_question_keyboard(questions))


@router.callback_query(F.data == SURVEY_SHOW)
async def on_survey_show(callback: CallbackQuery, db: Database) -> None:
    await ack(callback)
    if await session_with_summary(callback, db) is None:
        return
    await callback.message.edit_reply_markup(reply_markup=summary_keyboard)


@router.callback_query(F.data == SOURCES_DONE)
async def on_sources_done(callback: CallbackQuery, db: Database, bank: QuestionBank, pipeline: Pipeline) -> None:
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None or await db.get_waiting_for(session_id) != WAIT_SOURCES:
        await callback.message.answer("Это сообщение уже неактуально.", reply_markup=start_keyboard)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, SOURCES_DONE, "", model="user")
    await db.set_waiting_for(session_id, None)
    await run_analysis(callback.message, db, bank, pipeline, session_id)


@router.callback_query(F.data.startswith(SKIP_QUESTION_PREFIX))
async def on_skip_question(callback: CallbackQuery, db: Database, bank: QuestionBank) -> None:
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    key = callback.data.removeprefix(SKIP_QUESTION_PREFIX)
    current = await current_question(db, bank.load(), session_id) if session_id is not None else None
    if current is None or current[1].key != key:  # an old question's button
        await callback.message.answer("Этот вопрос уже неактуален.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_answer(session_id, key, current[1].text, SKIPPED_ANSWER)
    await ask_next_question(callback.message, db, bank, session_id)


@router.callback_query(F.data.startswith(EDIT_QUESTION_PREFIX))
async def on_edit_question(callback: CallbackQuery, db: Database, bank: QuestionBank) -> None:
    """Forget the answer and ask the question again; the summary comes back after it."""
    await ack(callback)
    session_id = await session_with_summary(callback, db)
    if session_id is None:
        return
    key = callback.data.removeprefix(EDIT_QUESTION_PREFIX)
    questions = asked_questions(bank.load(), await db.get_artifacts(session_id))
    if key not in {q.key for q in questions}:  # questions.yaml was edited and the question is gone
        await callback.message.answer("Такого вопроса больше нет.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.delete_answers(session_id, [key])
    await db.set_waiting_for(session_id, None)
    await ask_next_question(callback.message, db, bank, session_id)


@router.callback_query(F.data == PLAN_ACCEPT)
async def on_plan_accept(callback: CallbackQuery, db: Database, presets: PresetStore) -> None:
    await ack(callback)
    session_id = await session_with_plan(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_slide_images(callback.message, db, presets, session_id, SLIDE_IMAGES_REQUEST)


@router.callback_query(F.data.startswith(SLIDE_IMAGE_PREFIX))
async def on_slide_image(callback: CallbackQuery, db: Database) -> None:
    """«Слайд N»: wait for the photo for that slide."""
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    if not in_slide_images_step(waiting_for):  # while a caption is awaited, another slide means "no caption"
        await callback.message.answer("Эта кнопка уже неактуальна.")
        return
    number = int(callback.data.removeprefix(SLIDE_IMAGE_PREFIX))
    if await db.get_artifact(session_id, user_image_kind(number)) is not None:
        await callback.message.answer("Фото для этого слайда уже добавлено.")
        return
    await db.set_waiting_for(session_id, f"{WAIT_SLIDE_PHOTO_PREFIX}{number}")
    await callback.message.answer(f"Пришлите фото для слайда {number}.")


@router.callback_query(F.data == CAPTION_SKIP)
async def on_caption_skip(callback: CallbackQuery, db: Database, presets: PresetStore) -> None:
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    number = waiting_number(waiting_for, WAIT_CAPTION_PREFIX)
    if number is None:
        await callback.message.answer("Эта кнопка уже неактуальна.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    text = f"Фото для слайда {number} добавлено без подписи. Можно выбрать следующий слайд."
    await show_slide_images(callback.message, db, presets, session_id, text)


@router.callback_query(F.data == SLIDE_IMAGES_DONE)
async def on_slide_images_done(callback: CallbackQuery, db: Database, presets: PresetStore) -> None:
    """«Готово» / «Продолжить без фото»: go on to the style reference."""
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    if not in_slide_images_step(waiting_for):
        await callback.message.answer("Эта кнопка уже неактуальна.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await finish_slide_images(callback.message, db, presets, session_id)


@router.callback_query(F.data == PLAN_EDIT)
async def on_plan_edit(callback: CallbackQuery, db: Database) -> None:
    await ack(callback)
    session_id = await session_with_plan(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.set_waiting_for(session_id, PLAN_EDIT)
    await callback.message.answer(
        "Скачайте файл с планом, исправьте в нём то, что нужно, и пришлите файл обратно (.txt)."
    )


@router.callback_query(F.data == STYLE_REF_SKIP)
async def on_style_ref_skip(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None or await db.get_waiting_for(session_id) != WAIT_STYLE_REF:
        await callback.message.answer("Это сообщение уже неактуально.", reply_markup=start_keyboard)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, STYLE_REF, "", model="user")
    await db.set_waiting_for(session_id, None)
    await run_design(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data.startswith(STYLE_PRESET_PREFIX))
async def on_style_preset(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path, presets: PresetStore
) -> None:
    """«Стиль N»: the prepared design replaces the reference, the background (04) and the style (05);
    the background counts as accepted, so the slides start at once."""
    await ack(callback)
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None or await db.get_waiting_for(session_id) != WAIT_STYLE_REF:
        await callback.message.answer("Это сообщение уже неактуально.", reply_markup=start_keyboard)
        return
    preset = presets.get(int(callback.data.removeprefix(STYLE_PRESET_PREFIX)))
    if preset is None:
        await callback.message.answer("Этого стиля больше нет. Выберите другой или пришлите свою картинку.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    path = save_image(files_dir, session_id, "Фон", load_image(str(preset.background)))
    # The waiting state is cleared last: if saving stops halfway, the button still works.
    await db.save_artifact(session_id, STYLE, preset.style, model=f"preset:{preset.number}")
    await db.save_artifact(session_id, BACKGROUND, str(path), model=f"preset:{preset.number}")
    await db.save_artifact(session_id, STYLE_REF, "", model="user")
    await db.save_artifact(session_id, STYLE_PRESET, str(preset.number), model="user")
    await db.save_artifact(session_id, BACKGROUND_ACCEPTED, "", model="user")
    await db.set_waiting_for(session_id, None)
    await callback.message.answer(f"Выбран стиль {preset.number}.")
    await run_slides(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data == BACKGROUND_ACCEPT)
async def on_background_accept(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    """Accept the first background: under it, while the comment is awaited, or after the change failed."""
    await ack(callback)
    session_id = await session_at_background(callback, db, {BG_OFFERED, BG_COMMENT, BG_PENDING})
    if session_id is None:
        return
    if session_id in _running:  # the second background is being drawn right now
        await callback.message.answer("Работа уже идёт, подождите немного.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.set_waiting_for(session_id, None)
    await db.save_artifact(session_id, BACKGROUND_ACCEPTED, "", model="user")
    await run_slides(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data == BACKGROUND_EDIT)
async def on_background_edit(callback: CallbackQuery, db: Database) -> None:
    await ack(callback)
    session_id = await session_at_background(callback, db, {BG_OFFERED})
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, BACKGROUND_ATTEMPT, ATTEMPT_EDIT, model="user")
    await db.set_waiting_for(session_id, WAIT_BACKGROUND_COMMENT)
    await callback.message.answer(BACKGROUND_COMMENT_REQUEST)


@router.callback_query(F.data == BACKGROUND_REDRAW)
async def on_background_redraw(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    await ack(callback)
    session_id = await session_at_background(callback, db, {BG_OFFERED})
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, BACKGROUND_ATTEMPT, ATTEMPT_REDRAW, model="user")
    await run_background_change(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data.startswith(BACKGROUND_PICK_PREFIX))
async def on_background_pick(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    await ack(callback)
    session_id = await session_at_background(callback, db, {BG_CHOICE})
    if session_id is None:
        return
    choice = callback.data.removeprefix(BACKGROUND_PICK_PREFIX)
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, BACKGROUND_CHOICE, choice, model="user")
    await db.save_artifact(session_id, BACKGROUND_ACCEPTED, "", model="user")
    await callback.message.answer(f"Беру вариант {choice}.")
    await run_slides(callback.message, db, pipeline, files_dir, session_id)


def unexpected_file_text(waiting_for: str | None) -> str:
    if waiting_for == WAIT_DESCRIPTION:
        return "Сначала опишите презентацию текстом — файлы можно будет прислать на следующем шаге."
    if waiting_for == WAIT_BACKGROUND_COMMENT:
        return "Напишите, что понравилось и что нет, текстом."
    return "Сейчас я не жду файлов."


@router.message(F.photo)
async def on_photo(message: Message, db: Database, pipeline: Pipeline, files_dir: Path) -> None:
    session_id = await db.get_active_session(message.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    if waiting_for == WAIT_SOURCES:
        await receive_source_file(message, db, files_dir, session_id, message.photo[-1], None, "image/jpeg")
        return
    number = waiting_number(waiting_for, WAIT_SLIDE_PHOTO_PREFIX)
    if number is not None:
        await receive_slide_photo(message, db, files_dir, session_id, number)
        return
    if waiting_for == WAIT_SLIDE_IMAGES:
        await message.answer("Сначала нажмите номер слайда, для которого это фото.")
        return
    if waiting_number(waiting_for, WAIT_CAPTION_PREFIX) is not None:
        await message.answer(
            "Пришлите подпись текстом или нажмите «Продолжить без подписи».", reply_markup=caption_keyboard
        )
        return
    if waiting_for != WAIT_STYLE_REF:
        await message.answer(unexpected_file_text(waiting_for))
        return
    # Telegram sends a photo in several sizes; the last one is the largest. Photos are always JPEG.
    await receive_style_ref(message, db, pipeline, files_dir, session_id, message.photo[-1], "image/jpeg")


@router.message(F.document)
async def on_document(
    message: Message, db: Database, pipeline: Pipeline, files_dir: Path, soffice: str | None = None
) -> None:
    session_id = await db.get_active_session(message.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    if in_slide_images_step(waiting_for):
        await message.answer("Пришлите изображение как фото, а не файлом.")
        return
    if waiting_for == WAIT_STYLE_REF:
        # An image sent "as a file" keeps full quality; accept it like a photo.
        mime_type = message.document.mime_type or ""
        if mime_type not in IMAGE_EXTENSIONS:
            await message.answer("Пришлите картинку в формате JPG, PNG или WebP.")
            return
        await receive_style_ref(message, db, pipeline, files_dir, session_id, message.document, mime_type)
        return
    if waiting_for == WAIT_SOURCES:
        document = message.document
        name = document.file_name or f"Файл_{document.file_unique_id}"
        await receive_source_file(message, db, files_dir, session_id, document, name, document.mime_type, soffice)
        return
    if waiting_for != PLAN_EDIT:
        await message.answer(unexpected_file_text(waiting_for))
        return
    plan = await read_plan_file(message)
    if plan is None:
        return
    await db.save_artifact(session_id, PLAN.kind, plan, model="user")
    await db.set_waiting_for(session_id, None)
    await message.answer("Исправленный план сохранён.")
    await show_plan(message, plan)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database) -> None:
    session_id = await db.get_active_session(message.from_user.id)
    if session_id is None:
        await message.answer("Активной презентации нет.", reply_markup=start_keyboard)
        return
    await db.finish_session(session_id, status="cancelled")
    await message.answer("Презентация отменена.", reply_markup=start_keyboard)


@router.message(F.text)
async def on_answer(
    message: Message, db: Database, bank: QuestionBank, pipeline: Pipeline, files_dir: Path, presets: PresetStore
) -> None:
    session_id = await db.get_active_session(message.from_user.id)
    if session_id is None:
        await message.answer("Нажми «Новая презентация», чтобы начать.", reply_markup=start_keyboard)
        return
    waiting_for = await db.get_waiting_for(session_id)
    if waiting_for == PLAN_EDIT:
        await message.answer("Пришлите исправленный план файлом .txt.")
        return
    if waiting_for == WAIT_STYLE_REF:
        if presets.load():
            text = "Выберите стиль кнопкой под картинкой выше, пришлите картинку-референс или нажмите «Пропустить»."
        else:
            text = "Пришлите картинку-референс или нажмите «Пропустить»."
        await message.answer(text, reply_markup=style_ref_keyboard)
        return
    caption_for = waiting_number(waiting_for, WAIT_CAPTION_PREFIX)
    if caption_for is not None:
        caption = message.text.strip()
        if len(caption) > MAX_CAPTION_CHARS:
            await message.answer(
                f"Подпись слишком длинная: {len(caption)} символов, можно до {MAX_CAPTION_CHARS}. "
                "Сократите, пожалуйста.",
                reply_markup=caption_keyboard,
            )
            return
        image = UserImage.from_json(await db.get_artifact(session_id, user_image_kind(caption_for)))
        image = UserImage(image.path, image.width, image.height, caption)
        await db.save_artifact(session_id, user_image_kind(caption_for), image.to_json(), model="user")
        text = "Подпись сохранена. Можно выбрать следующий слайд."
        await show_slide_images(message, db, presets, session_id, text)
        return
    photo_for = waiting_number(waiting_for, WAIT_SLIDE_PHOTO_PREFIX)
    if photo_for is not None:
        await message.answer(f"Пришлите фото для слайда {photo_for}.")
        return
    if waiting_for == WAIT_SLIDE_IMAGES:
        text = "Нажмите номер слайда, чтобы добавить фото, или кнопку внизу, чтобы продолжить."
        await show_slide_images(message, db, presets, session_id, text)
        return
    if waiting_for == WAIT_BACKGROUND_COMMENT:
        await db.save_artifact(session_id, BACKGROUND_COMMENT, message.text.strip(), model="user")
        await db.set_waiting_for(session_id, None)
        await run_background_change(message, db, pipeline, files_dir, session_id)
        return
    if waiting_for == WAIT_DESCRIPTION:
        await notice_links(message)
        await db.save_artifact(session_id, DESCRIPTION, message.text.strip(), model="user")
        await ask_source_files(message, db, session_id)
        return
    if waiting_for == WAIT_SOURCES:
        await notice_links(message)
        note = await db.get_artifact(session_id, SOURCES_NOTE)
        text = message.text.strip() if not note else f"{note}\n\n{message.text.strip()}"
        await db.save_artifact(session_id, SOURCES_NOTE, text, model="user")
        await message.answer(
            "Записал. Пришлите ещё файлы или текст — или нажмите «Готово».", reply_markup=sources_done_keyboard
        )
        return
    if waiting_for == SURVEY_CONFIRM:
        await message.answer("Проверьте ответы выше и нажмите кнопку:", reply_markup=summary_keyboard)
        return
    artifacts = await db.get_artifacts(session_id)
    if BACKGROUND_ACCEPTED in artifacts:
        # Slides are being drawn, or the previous attempt failed: continue.
        await run_slides(message, db, pipeline, files_dir, session_id)
        return
    stage = background_stage(artifacts, waiting_for)
    if stage == BG_CHOICE:
        await message.answer("Выберите фон выше:", reply_markup=background_choice_keyboard)
        return
    if stage == BG_PENDING:
        # The second background is being drawn, or the previous attempt failed: continue.
        await run_background_change(message, db, pipeline, files_dir, session_id)
        return
    if stage == BG_OFFERED:
        await message.answer("Посмотрите фон выше и нажмите кнопку:", reply_markup=background_keyboard)
        return
    if STYLE_REF in artifacts:
        # Plan accepted but there is no background yet: the previous attempt failed.
        await run_design(message, db, pipeline, files_dir, session_id)
        return
    if PLAN.kind in artifacts:
        await message.answer("Посмотрите план выше и нажмите кнопку:", reply_markup=plan_keyboard)
        return
    survey = bank.load()
    stage = await survey_stage(db, survey, session_id)
    if stage == STAGE_DONE:
        # Answers confirmed but the pipeline hasn't finished (e.g. the model failed earlier).
        await run_pipeline(message, db, bank, pipeline, session_id)
        return
    current = await current_question(db, survey, session_id) if stage == STAGE_QUESTION else None
    if current is None:
        await resume_survey(message, db, bank, session_id, stage)
        return
    await notice_links(message)
    q = current[1]
    await db.save_answer(session_id, q.key, q.text, message.text.strip())
    await ask_next_question(message, db, bank, session_id)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока я понимаю только текстовые ответы.")
