"""One local worker, durable queue, at most three concurrent presentations.

Run with: uv run python -m bot.web.worker
The OS lock prevents two workers from recovering each other's live tasks.
"""
import asyncio
from bot.filelock import lock as file_lock
import logging
from pathlib import Path

from bot.db import Database
from bot.web.engine import PresentationEngine
from bot.web.storage import row, rows, project, enqueue, update_project

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, pipeline, bank, bot=None):
        self.settings, self.pipeline, self.bank, self.bot = settings, pipeline, bank, bot
        self.tasks = {}
        self.task_sessions = {}
        self.stopping = asyncio.Event()

    async def deliver(self, db, sid):
        from aiogram.types import Message, Chat, FSInputFile
        from datetime import datetime, timezone
        from bot.web.storage import result_files
        p = await project(db, sid)
        if not p or not self.bot or await db.get_artifact(sid, 'notification_disabled') is not None:
            return
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=p['user_id'], type='private')).as_(self.bot)
        a = await db.get_artifacts(sid)
        if p['status'] == 'completed':
            for kind, output in (await result_files(db, sid)).items():
                marker = 'delivered_' + kind
                if marker in a:
                    continue
                path = Path(output['path'])
                if path.stat().st_size > 49_000_000:
                    await message.answer('Презентация готова. Файл больше лимита Telegram — скачайте его в приложении.')
                    continue
                await message.answer_document(FSInputFile(path, filename=path.name))
                await db.save_artifact(sid, marker, '1')
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
            buttons = [[InlineKeyboardButton(text='Новая презентация', callback_data='new_presentation')]]
            if self.settings.miniapp_url:
                buttons.append([InlineKeyboardButton(text='Открыть приложение', web_app=WebAppInfo(url=self.settings.miniapp_url))])
            await message.answer('Презентация готова!' + ('\n\n'+a['text_report'] if a.get('text_report') else ''),
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        elif p['origin'] == 'chat':
            from bot import handlers as h
            if p['stage'] == 'survey':
                await h.ask_next_question(message, db, self.bank, sid)
            elif p['stage'] == 'plan':
                await h.show_plan(message, db, sid)
            elif p['stage'] == 'background':
                from bot.pipeline import BACKGROUND, BACKGROUND_ALT, BACKGROUND_ATTEMPT
                from bot.runtime import load_image
                if BACKGROUND_ALT in a:
                    await h.show_background_choice(message, load_image(a[BACKGROUND]), load_image(a[BACKGROUND_ALT]), a[BACKGROUND_ATTEMPT])
                else:
                    await message.answer_photo(FSInputFile(a[BACKGROUND]), caption=h.BACKGROUND_CAPTION, reply_markup=h.background_keyboard)

    async def run_job(self, job):
        db = Database(self.settings.db_path)
        await db.connect(migrate=False)
        sid, jid = job['session_id'], job['id']

        async def progress(text):
            p = await project(db, sid)
            if not p or p['status'] != 'in_progress':
                raise asyncio.CancelledError()
            await db.conn.execute("UPDATE presentation_jobs SET progress=?,updated_at=datetime('now') WHERE id=? AND status='running'", (text, jid))
            await db.commit()

        try:
            if job['action'] == 'deliver':
                await self.deliver(db, sid)
            else:
                await PresentationEngine(db, self.pipeline, self.bank, self.settings.files_dir).run(sid, job['action'], progress)
            async with db.transaction():
                await db.conn.execute("UPDATE presentation_jobs SET status='succeeded',progress='Готово',updated_at=datetime('now') WHERE id=? AND status='running'", (jid,))
                p = await project(db, sid)
                if p and job['action'] != 'deliver' and (p['origin'] == 'chat' or p['status'] == 'completed'):
                    await enqueue(db, sid, 'deliver', self.settings.daily_limit)
        except asyncio.CancelledError:
            await db.conn.execute("UPDATE presentation_jobs SET status='interrupted',progress='Прервано. Нажмите «Продолжить»',updated_at=datetime('now') WHERE id=? AND status='running'", (jid,))
            await db.commit()
            raise
        except Exception:
            log.exception('Presentation job %s failed', jid)
            await db.conn.execute("UPDATE presentation_jobs SET status='failed',error=?,progress='Не удалось завершить шаг',updated_at=datetime('now') WHERE id=? AND status='running'", ('Прогресс сохранён. Можно повторить незавершённый шаг.', jid))
            await db.commit()
            p = await project(db, sid)
            if self.bot and p and p['origin'] == 'chat' and p['status'] == 'in_progress':
                from bot.generation import retry_keyboard
                if job['action'] == 'generate' and await db.get_artifact(sid, 'generation_mode') == 'editable':
                    from bot.generation_modes import failed_keyboard
                    await db.set_waiting_for(sid, 'editable_failed')
                    retry_keyboard = failed_keyboard(sid)
                try:
                    await self.bot.send_message(p['user_id'], 'Не удалось завершить шаг. Прогресс сохранён — нажмите «Повторить».', reply_markup=retry_keyboard)
                except Exception:
                    log.warning('Could not notify chat for job %s', jid)
        finally:
            await db.close()

    async def claim(self, db):
        async with db.transaction():
            job = await row(db, "SELECT j.* FROM presentation_jobs j JOIN sessions s ON s.id=j.session_id WHERE j.status='queued' AND (s.status='in_progress' OR (j.action='deliver' AND s.status='completed')) ORDER BY j.id LIMIT 1")
            if job:
                await db.conn.execute("UPDATE presentation_jobs SET status='running',updated_at=datetime('now') WHERE id=? AND status='queued'", (job['id'],))
            return job

    async def run(self):
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.db_path.with_suffix('.worker.lock').open('w') as lock:
            try:
                file_lock(lock, blocking=False)
            except BlockingIOError:
                raise RuntimeError('Обработчик заданий уже запущен для этой базы') from None
            db = Database(self.settings.db_path)
            await db.connect()
            await db.conn.execute("UPDATE presentation_jobs SET status='interrupted',progress='Прервано. Нажмите «Продолжить»' WHERE status='running'")
            await db.commit()
            log.info('Worker started; concurrency=%d', self.settings.concurrency)
            try:
                while not self.stopping.is_set():
                    for jid, task in list(self.tasks.items()):
                        state = await row(db, 'SELECT status FROM presentation_jobs WHERE id=?', (jid,))
                        if not state or state['status'] == 'cancelled':
                            task.cancel()
                        if task.done():
                            await asyncio.gather(task, return_exceptions=True)
                            del self.tasks[jid]
                            self.task_sessions.pop(jid, None)
                    from bot.web.storage import cleanup_deleted
                    await cleanup_deleted(db, self.settings.files_dir, active_sessions=set(self.task_sessions.values()))
                    while len(self.tasks) < self.settings.concurrency:
                        job = await self.claim(db)
                        if not job:
                            break
                        self.tasks[job['id']] = asyncio.create_task(self.run_job(job))
                        self.task_sessions[job['id']] = job['session_id']
                    try:
                        await asyncio.wait_for(self.stopping.wait(), .5)
                    except TimeoutError:
                        pass
            finally:
                for task in self.tasks.values():
                    task.cancel()
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
                await db.close()


async def main():
    from bot.config import load_config
    from bot.bootstrap import create_pipeline
    from bot.questions import QuestionBank
    from bot.web.settings import WebSettings
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    import signal
    logging.basicConfig(level=logging.INFO)
    config, settings = load_config(), WebSettings.load()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    worker = Worker(settings, create_pipeline(config), QuestionBank(config.questions_path), bot)
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, worker.stopping.set)
    try:
        await worker.run()
    finally:
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
