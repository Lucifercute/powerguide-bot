"""Ownership and cancellation of in-process generation jobs."""
import asyncio
import inspect
import logging
from functools import wraps
from weakref import WeakValueDictionary

log = logging.getLogger(__name__)


def session_locks():
    # Strong references are held by running handlers, including waiters.
    return _SessionLocks()


class _SessionLocks:
    def __init__(self):
        self._locks = WeakValueDictionary()

    def __getitem__(self, session_id):
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock


class SessionJobs:
    def __init__(self):
        self._tasks = {}

    def __contains__(self, session_id):
        return session_id in self._tasks

    def add(self, session_id):
        self._tasks[session_id] = asyncio.current_task()

    def discard(self, session_id):
        if self._tasks.get(session_id) is asyncio.current_task():
            self._tasks.pop(session_id, None)

    async def cancel(self, session_id):
        task = self._tasks.get(session_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def handle_cancellation(self, function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            db, session_id = arguments['db'], arguments['session_id']
            settings = getattr(db, 'queue_settings', None)
            queued_actions = {'run_analysis': 'analyze', 'run_pipeline': 'plan',
                              'run_design': 'background', 'run_background_change': 'background_change',
                              'run_slides': 'generate', 'run_editable': 'generate'}
            if settings and function.__name__ in queued_actions:
                from bot.web.storage import enqueue, Conflict, QuotaExceeded
                try:
                    async with db.transaction():
                        await enqueue(db, session_id, queued_actions[function.__name__], settings.daily_limit)
                    await arguments['message'].answer('Задание принято. Можно закрыть Telegram — результат придёт в этот чат.')
                except (Conflict, QuotaExceeded) as error:
                    from bot.generation import retry_keyboard
                    await arguments['message'].answer(str(error), reply_markup=retry_keyboard)
                return
            task = asyncio.current_task()
            existing = self._tasks.get(session_id)
            if existing is not None and existing is not task:
                await arguments['message'].answer('Работа уже идёт, подождите немного.')
                return
            owner = existing is None
            if owner:
                self.add(session_id)
            try:
                if not await db.is_active(session_id):
                    return
                return await function(*args, **kwargs)
            except asyncio.CancelledError:
                if await db.is_active(session_id):
                    raise  # preserve process shutdown cancellation
                log.info('Cancelled generation for session %d', session_id)
            except Exception:
                log.exception('Generation failed for session %d', session_id)
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                await arguments['message'].answer(
                    'Не удалось завершить шаг. Прогресс сохранён — попробуйте ещё раз.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text='Повторить', callback_data='retry_step')
                    ]]),
                )
            finally:
                if owner:
                    self.discard(session_id)
        return wrapped
