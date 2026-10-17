import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.db import Database
from bot.handlers import router
from bot.llm import ImageLLM, TextLLM
from bot.pipeline import Pipeline
from bot.questions import QuestionBank
from bot.search import WebSearch


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("google_genai.models").setLevel(logging.WARNING)  # "AFC is enabled…" on every call
    config = load_config()

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    search = WebSearch(config.tavily_api_key) if config.tavily_api_key else None
    if search is None:
        logging.warning("TAVILY_API_KEY is not set: step 02 will run without web search")
    if config.libreoffice_path is None:
        logging.warning("LibreOffice not found: Word and PowerPoint files will be read as text only")
    pipeline = Pipeline(
        llm=TextLLM(
            config.gemini_api_key, config.gemini_text_models, config.vertex_api_key, config.vertex_text_models
        ),
        images=ImageLLM(config.vertex_api_key, config.image_models),
        search=search,
        prompts_dir=config.prompts_dir,
        max_queries=config.search_max_queries,
    )
    # Values passed here are injected into handlers by parameter name (db, bank, pipeline, files_dir, soffice).
    dp = Dispatcher(
        db=db,
        bank=QuestionBank(config.questions_path),
        pipeline=pipeline,
        files_dir=config.files_dir,
        soffice=config.libreoffice_path,
    )
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
