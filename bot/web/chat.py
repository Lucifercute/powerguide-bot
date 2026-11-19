"""Pilot-only chat boundary; keeps the original dialogue and queues paid work."""
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from bot.db import Database
from bot.jobs import session_locks
from bot.web.storage import project, latest_job, enqueue, Conflict, QuotaExceeded


class PilotMiddleware(BaseMiddleware):
    def __init__(self, settings):
        self.settings = settings
        self.locks = session_locks()

    async def __call__(self, handler, event, data):
        user = event.from_user
        if not user:
            return
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if callback:
            await event.answer()
        if user.id not in self.settings.allowed_ids:
            await message.answer('Доступ открыт только участникам пилота.')
            return
        if message.chat.type != 'private':
            await message.answer('Создание презентаций доступно в личном чате с ботом.')
            return
        async with self.locks[user.id]:
            db = Database(self.settings.db_path)
            await db.connect(migrate=False)
            db.queue_settings = self.settings
            data['db'] = db
            try:
                active = await db.get_active_session(user.id)
                p = await project(db, active) if active else None
                code = event.data if callback else ''
                if code and code.startswith('replace_chat:'):
                    try:
                        expected = int(code.split(':')[1])
                    except ValueError:
                        return
                    if not p or p['id'] != expected:
                        await message.answer('Активная презентация изменилась. Нажмите «Новая презентация» ещё раз.')
                        return
                    db.confirm_replace_sid = expected
                    event = event.model_copy(update={'data': 'new_presentation'})
                    code = 'new_presentation'
                elif code == 'new_presentation' and p:
                    await message.answer('Отменить текущую презентацию и начать новую в чате?', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text='Отменить и создать новую', callback_data=f'replace_chat:{active}')
                    ]]))
                    return
                if p and p['origin'] == 'miniapp' and code != 'new_presentation':
                    markup = None
                    if self.settings.miniapp_url:
                        markup = InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text='Открыть презентацию', web_app=WebAppInfo(url=self.settings.miniapp_url))
                        ]])
                    await message.answer('Активная презентация создана в приложении. Продолжите её там или нажмите «Новая презентация», чтобы заменить её.', reply_markup=markup)
                    return
                job = await latest_job(db, active) if active else None
                if p and job and code == 'retry_step' and job['status'] in ('failed','interrupted'):
                    try:
                        async with db.transaction():
                            await enqueue(db, active, job['action'], self.settings.daily_limit)
                        await message.answer('Продолжаю с сохранённого этапа…')
                    except (Conflict, QuotaExceeded) as error:
                        await message.answer(str(error))
                    return
                if job and job['status'] in ('queued','running') and code != 'new_presentation' and getattr(message,'text',None) != '/cancel':
                    await message.answer(job['progress'] or 'Работа уже идёт. Можно подождать или отправить /cancel.')
                    return
                try:
                    return await handler(event, data)
                except (Conflict, QuotaExceeded) as error:
                    await message.answer(str(error))
            finally:
                await db.close()
