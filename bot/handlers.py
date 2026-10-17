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
    BACKGROUND_ACCEPTED,
    BACKGROUND_FIELDS,
    DATA,
    DESCRIPTION,
    DESCRIPTIONS,
    MAX_SLIDES,
    PLAN,
    RATIO_ANSWER_KEY,
    SKIPPED_ANSWER,
    SOURCES,
    SOURCES_NOTE,
    STEPS,
    STYLE,
    STYLE_REF,
    SURVEY_CONFIRMED,
    TZ,
    BackgroundFields,
    Pipeline,
    parse_aspect_ratio,
    slide_kind,
    slide_prompt_kind,
    split_descriptions,
    text_check_kind,
)
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
WAIT_STYLE_REF = "style_ref"  # sessions.waiting_for while the bot waits for a reference image
BACKGROUND_ACCEPT = "background_accept"
BACKGROUND_REGENERATE = "background_regenerate"
MAX_IMAGE_BYTES = 10_000_000
# Slide requests running at the same time. 1 = one by one: the Vertex trial quota
# answered 429 to 3 parallel requests, and retries only slowed things down.
SLIDE_CONCURRENCY = 1
TEXT_CHECK_CONCURRENCY = 3  # text requests: Vertex has no trouble with a few at once
TELEGRAM_ALBUM_LIMIT = 10
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
        [
            InlineKeyboardButton(text="Принять", callback_data=BACKGROUND_ACCEPT),
            InlineKeyboardButton(text="Перегенерировать", callback_data=BACKGROUND_REGENERATE),
        ]
    ]
)
STYLE_REF_REQUEST = (
    "План принят. Теперь дизайн слайдов.\n\n"
    "Если у вас есть картинка, стиль которой нравится (фото, скриншот, слайд из другой презентации), "
    "пришлите её одним изображением — фон сделаю в похожем стиле. Если нет — нажмите «Пропустить»."
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
        or await db.get_waiting_for(session_id) == WAIT_STYLE_REF
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
    """Generate a background (prompt 04) and show it with «Принять» / «Перегенерировать».

    Always draws a new image, so it serves both the first attempt and regeneration.
    The prompt fields are filled from the ТЗ once and reused, so a regenerated
    background is a variation of the same idea, not a different design.
    """
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
            caption="Фон для слайдов. Если нравится — «Принять», если нет — «Перегенерировать».",
            reply_markup=background_keyboard,
        )
    finally:
        _running.discard(session_id)


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
        background = load_image(artifacts[BACKGROUND])
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
                descriptions, model = await pipeline.descriptions(artifacts[PLAN.kind])
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
        await message.answer(report)
        await message.answer("Презентация готова!", reply_markup=start_keyboard)
    finally:
        _running.discard(session_id)


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

    The first slide is drawn alone and then attached to the others as a design sample;
    the others follow, SLIDE_CONCURRENCY at a time.
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

    async def draw(number: int, sample: Image | None) -> Image | None:
        nonlocal done
        prompt = pipeline.slide_prompt(number, blocks[number], style, ratio, with_sample=sample is not None)
        # Saved before the request: if the slide comes out wrong, the prompt shows where the problem came from.
        await db.save_artifact(session_id, slide_prompt_kind(number), prompt)
        try:
            image, model = await pipeline.slide(prompt, ratio, background, sample)
        except LLMError as e:
            log.error("Slide %d failed for session %d: %s", number, session_id, e)
            failed.append(number)
            return None
        path = save_image(files_dir, session_id, f"Слайд_{number:02d}", image)
        await db.save_artifact(session_id, slide_kind(number), str(path), model)
        done += 1
        with suppress(TelegramBadRequest):  # e.g. "message is not modified"
            await status.edit_text(f"Рисую слайды. Готово: {done} из {len(blocks)}…")
        return image

    first = next(iter(blocks))
    if first in missing:
        sample = await draw(first, None)
        if sample is None:
            return failed  # without the sample the others would look different: stop here
        missing.remove(first)
    else:
        sample = load_image(artifacts[slide_kind(first)])

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
                slide = load_image(artifacts[slide_kind(number)])
                issues, model = await pipeline.check_slide_text(slide, blocks[number])
            except LLMError as e:
                log.warning("Text check of slide %d failed for session %d: %s", number, session_id, e)
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


async def session_with_background(callback: CallbackQuery, db: Database) -> int | None:
    """Active session whose background waits for «Принять» / «Перегенерировать», or None (stale button)."""
    session_id = await db.get_active_session(callback.from_user.id)
    if (
        session_id is None
        or await db.get_artifact(session_id, BACKGROUND) is None
        or await db.get_artifact(session_id, BACKGROUND_ACCEPTED) is not None
    ):
        await callback.message.answer("Этот фон уже неактуален.", reply_markup=start_keyboard)
        return None
    return session_id


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
    await callback.answer()
    await ask_description(callback.message, db, session_id)


@router.callback_query(F.data == RETRY_STEP)
async def on_retry_step(
    callback: CallbackQuery, db: Database, bank: QuestionBank, pipeline: Pipeline, files_dir: Path
) -> None:
    await callback.answer()
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
    await callback.answer()
    session_id = await session_with_summary(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, SURVEY_CONFIRMED, "", model="user")
    await db.set_waiting_for(session_id, None)
    await run_pipeline(callback.message, db, bank, pipeline, session_id)


@router.callback_query(F.data == SURVEY_EDIT)
async def on_survey_edit(callback: CallbackQuery, db: Database, bank: QuestionBank) -> None:
    await callback.answer()
    session_id = await session_with_summary(callback, db)
    if session_id is None:
        return
    questions = asked_questions(bank.load(), await db.get_artifacts(session_id))
    await callback.message.edit_reply_markup(reply_markup=edit_question_keyboard(questions))


@router.callback_query(F.data == SURVEY_SHOW)
async def on_survey_show(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    if await session_with_summary(callback, db) is None:
        return
    await callback.message.edit_reply_markup(reply_markup=summary_keyboard)


@router.callback_query(F.data == SOURCES_DONE)
async def on_sources_done(callback: CallbackQuery, db: Database, bank: QuestionBank, pipeline: Pipeline) -> None:
    await callback.answer()
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
    await callback.answer()
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
    await callback.answer()
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
async def on_plan_accept(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    session_id = await session_with_plan(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.set_waiting_for(session_id, WAIT_STYLE_REF)
    await callback.message.answer(STYLE_REF_REQUEST, reply_markup=style_ref_keyboard)


@router.callback_query(F.data == PLAN_EDIT)
async def on_plan_edit(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
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
    await callback.answer()
    session_id = await db.get_active_session(callback.from_user.id)
    if session_id is None or await db.get_waiting_for(session_id) != WAIT_STYLE_REF:
        await callback.message.answer("Это сообщение уже неактуально.", reply_markup=start_keyboard)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, STYLE_REF, "", model="user")
    await db.set_waiting_for(session_id, None)
    await run_design(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data == BACKGROUND_REGENERATE)
async def on_background_regenerate(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    await callback.answer()
    session_id = await session_with_background(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await run_design(callback.message, db, pipeline, files_dir, session_id)


@router.callback_query(F.data == BACKGROUND_ACCEPT)
async def on_background_accept(
    callback: CallbackQuery, db: Database, pipeline: Pipeline, files_dir: Path
) -> None:
    await callback.answer()
    session_id = await session_with_background(callback, db)
    if session_id is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await db.save_artifact(session_id, BACKGROUND_ACCEPTED, "", model="user")
    await run_slides(callback.message, db, pipeline, files_dir, session_id)


def unexpected_file_text(waiting_for: str | None) -> str:
    if waiting_for == WAIT_DESCRIPTION:
        return "Сначала опишите презентацию текстом — файлы можно будет прислать на следующем шаге."
    return "Сейчас я не жду файлов."


@router.message(F.photo)
async def on_photo(message: Message, db: Database, pipeline: Pipeline, files_dir: Path) -> None:
    session_id = await db.get_active_session(message.from_user.id)
    waiting_for = await db.get_waiting_for(session_id) if session_id is not None else None
    if waiting_for == WAIT_SOURCES:
        await receive_source_file(message, db, files_dir, session_id, message.photo[-1], None, "image/jpeg")
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
    message: Message, db: Database, bank: QuestionBank, pipeline: Pipeline, files_dir: Path
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
        await message.answer("Пришлите картинку-референс или нажмите «Пропустить».", reply_markup=style_ref_keyboard)
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
    if BACKGROUND in artifacts:
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
