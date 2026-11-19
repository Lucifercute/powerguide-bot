import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.bootstrap import create_pipeline
from bot.db import Database
from bot.handlers import router
from bot.presets import PresetStore
from bot.questions import QuestionBank


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("google_genai.models").setLevel(logging.WARNING)  # "AFC is enabled…" on every call
    config = load_config()

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if not config.tavily_api_key:
        logging.warning("TAVILY_API_KEY is not set: step 02 will run without web search")
    if config.libreoffice_path is None:
        logging.warning("LibreOffice not found: Word and PowerPoint files will be read as text only")
    pipeline = create_pipeline(config)
    logging.info('Editable PPTX choice enabled: %s; database: %s',
                 config.editable_pptx_enabled, config.db_path.resolve())
    # Values passed here are injected into handlers by parameter name (db, bank, pipeline, files_dir, soffice, presets).
    dp = Dispatcher(
        db=db,
        bank=QuestionBank(config.questions_path),
        pipeline=pipeline,
        files_dir=config.files_dir,
        soffice=config.libreoffice_path,
        presets=PresetStore(config.presets_dir),
    )
    dp.include_router(router)

    import os
    if os.getenv('PILOT_ENABLED', '0') == '1':
        from bot.web.settings import WebSettings
        from bot.web.chat import PilotMiddleware
        settings = WebSettings.load()
        middleware = PilotMiddleware(settings)
        dp.message.outer_middleware(middleware)
        dp.callback_query.outer_middleware(middleware)
        if settings.miniapp_url:
            from aiogram.types import InlineKeyboardButton, WebAppInfo
            from bot.generation import start_keyboard as result_keyboard
            from bot.handlers import start_keyboard as greeting_keyboard
            for keyboard in (greeting_keyboard, result_keyboard):
                keyboard.inline_keyboard.append([
                    InlineKeyboardButton(text='Открыть приложение', web_app=WebAppInfo(url=settings.miniapp_url))
                ])

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
