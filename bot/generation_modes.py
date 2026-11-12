"""A persisted choice between the original generator and editable HTML/PPTX."""
import logging
import re
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bot.generation import run_slides, start_keyboard
from bot.jobs import session_locks
from bot.pipeline import BACKGROUND_ACCEPTED, DESCRIPTIONS
from bot.runtime import _running

log = logging.getLogger(__name__)
router = Router()
MODE = 'generation_mode'
WAIT_MODE = 'generation_mode'
WAIT_FAILED = 'editable_failed'
_choices = session_locks()
CHOICE_TEXT = (
    'Как подготовить презентацию?\n\n'
    '✏️ Редактируемый PPTX — текст и элементы схем можно менять в PowerPoint. '
    'Иллюстрации и фон останутся изображениями. Дополнительно получите HTML.\n\n'
    '▶️ Стандартная генерация — презентация в привычном формате, как раньше.'
)


def choice_keyboard(session_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✏️ Редактируемый PPTX', callback_data=f'genmode:{session_id}:editable')],
        [InlineKeyboardButton(text='▶️ Стандартная генерация', callback_data=f'genmode:{session_id}:standard')],
    ])


def failed_keyboard(session_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Повторить', callback_data='retry_step')],
        [InlineKeyboardButton(text='Перейти к стандартной генерации', callback_data=f'genfallback:{session_id}')],
    ])


@_running.handle_cancellation
async def continue_generation(message, db, pipeline, files_dir, session_id):
    artifacts = await db.get_artifacts(session_id)
    if BACKGROUND_ACCEPTED not in artifacts:
        return
    mode = artifacts.get(MODE)
    if mode is None:
        # Only numbered rendering artifacts indicate an old in-progress job.
        # slide_images_done is set before style selection in EVERY new session.
        old_progress = DESCRIPTIONS in artifacts or any(
            re.fullmatch(r'slide_(?:prompt_|marked_)?[0-9]+', kind) for kind in artifacts
        )
        if old_progress or not getattr(pipeline, 'editable_enabled', False):
            mode = 'standard'
            log.info('Session %d: standard mode, reason=%s', session_id,
                     'existing_render_progress' if old_progress else 'editable_disabled')
            await db.save_artifact(session_id, MODE, mode)
        else:
            await db.set_waiting_for(session_id, WAIT_MODE)
            log.info('Session %d: offering generation mode choice', session_id)
            await message.answer(CHOICE_TEXT, reply_markup=choice_keyboard(session_id))
            return
    await db.set_waiting_for(session_id, None)
    if mode == 'editable':
        await run_editable(message, db, pipeline, files_dir, session_id)
    else:
        await run_slides(message, db, pipeline, files_dir, session_id)


@_running.handle_cancellation
async def run_editable(message, db, pipeline, files_dir, session_id):
    try:
        html, pptx = await pipeline.editable_service.generate(db, session_id, files_dir, message.answer)
        for kind, path, filename in [
            ('pptx', pptx, 'Презентация_редактируемая.pptx'),
            ('html', html, 'Презентация.html'),
        ]:
            if path.stat().st_size > 49_000_000:
                raise ValueError('Файл превышает лимит отправки Telegram')
            delivered = f'editable_sent_{kind}'
            if await db.get_artifact(session_id, delivered) is None:
                await message.answer_document(FSInputFile(path, filename=filename))
                await db.save_artifact(session_id, delivered, str(path))
        await message.answer('Презентация готова! Текст и элементы схем редактируются в PowerPoint.', reply_markup=start_keyboard)
        await db.finish_session(session_id)
    except Exception:
        log.exception('Editable generation failed for session %d', session_id)
        if await db.is_active(session_id):
            await db.set_waiting_for(session_id, WAIT_FAILED)
            await message.answer(
                'Не удалось завершить редактируемую презентацию. Прогресс сохранён. '
                'Можно повторить незавершённый этап или перейти к стандартной генерации.',
                reply_markup=failed_keyboard(session_id),
            )


@router.callback_query(F.data.startswith('genmode:') | F.data.startswith('genfallback:'))
async def on_generation_mode(callback, db, pipeline, files_dir):
    with suppress(TelegramBadRequest):
        await callback.answer()
    parts = callback.data.split(':')
    fallback = parts[0] == 'genfallback'
    try:
        session_id = int(parts[1])
        mode = 'standard' if fallback else parts[2]
    except (ValueError, IndexError):
        return
    if mode not in {'standard', 'editable'}:
        return
    async with _choices[session_id]:
        active = await db.get_active_session(callback.from_user.id)
        waiting = await db.get_waiting_for(session_id) if active == session_id else None
        artifacts = await db.get_artifacts(session_id) if active == session_id else {}
        expected = WAIT_FAILED if fallback else WAIT_MODE
        if active != session_id or waiting != expected or BACKGROUND_ACCEPTED not in artifacts:
            await callback.message.answer('Эта кнопка уже неактуальна.')
            return
        if session_id in _running:
            await callback.message.answer('Работа уже идёт, подождите немного.')
            return
        if not fallback and not getattr(pipeline, 'editable_enabled', False):
            await callback.message.answer('Выбор нового режима сейчас отключён. Отправьте сообщение, чтобы продолжить стандартную генерацию.')
            return
        await db.save_artifact(session_id, MODE, mode, model='user')
        log.info('Session %d: user selected %s mode (fallback=%s)', session_id, mode, fallback)
        await db.set_waiting_for(session_id, None)
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
    await continue_generation(callback.message, db, pipeline, files_dir, session_id)
