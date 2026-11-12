"""Presentation generation and delivery, independent of dialogue routing."""
import asyncio
import json
import logging
from contextlib import suppress
from html import escape
from pathlib import Path
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, BufferedInputFile, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from bot.db import Database
from bot.export import build_pdf, build_editable_pptx
from bot.llm import Image, LLMError
from bot.pipeline import (Pipeline, STYLE, DESCRIPTIONS, PLAN, MAX_SLIDES,
                         slide_kind, user_image_failed_kind, slide_prompt_kind, slide_marked_kind,
                         text_check_kind, validate_descriptions)
from bot.runtime import _running, load_user_images, save_image, load_image, session_ratio, chosen_background
from bot.textcheck import TextIssue
from bot.user_images import UserImage, find_area, marker_color, place_image

log = logging.getLogger(__name__)
SLIDE_CONCURRENCY = 1
TEXT_CHECK_CONCURRENCY = 3
TELEGRAM_ALBUM_LIMIT = 10
PDF_FILENAME = "Презентация.pdf"
start_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Новая презентация", callback_data="new_presentation")]])
retry_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Повторить", callback_data="retry_step")]])

@_running.handle_cancellation
async def run_slides(
    message: Message, db: Database, pipeline: Pipeline, files_dir: Path, session_id: int
) -> None:
    """Everything after the background is accepted, without stops: style (05), slide
    descriptions (06), slide images (07), then the album and the PDF.

    Each result is saved as soon as it's ready, so a retry continues where it stopped:
    finished slides are not drawn again.
    """
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

    try:
        blocks = validate_descriptions(artifacts[PLAN.kind], artifacts[DESCRIPTIONS])
    except LLMError:
        await db.delete_artifact(session_id, DESCRIPTIONS)
        await message.answer("Описание слайдов неполное. Нажмите «Повторить», чтобы подготовить его заново.", reply_markup=retry_keyboard)
        return
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
    editable = await asyncio.to_thread(
        build_editable_pptx, artifacts[PLAN.kind], ratio,
        await load_user_images(db, session_id), list(blocks),
    )
    await message.answer_document(BufferedInputFile(editable, filename="Презентация_редактируемая.pptx"))
    no_picture = [str(n) for n in blocks if user_image_failed_kind(n) in artifacts]
    if no_picture:
        report += (
            "\n\nНе удалось вставить ваши изображения на слайды: " + ", ".join(no_picture) + ". "
            "Эти слайды нарисованы без них."
        )
    await message.answer(report)
    await message.answer("Презентация готова!", reply_markup=start_keyboard)
    await db.finish_session(session_id)


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

    async with asyncio.TaskGroup() as group:
        for number in missing:
            group.create_task(draw_limited(number))
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

    async with asyncio.TaskGroup() as group:
        for number in unchecked:
            group.create_task(check(number))
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
