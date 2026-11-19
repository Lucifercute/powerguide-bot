"""Identical model configuration for the chat bot and the presentation worker."""
from bot.editable.service import EditableService
from bot.llm import ImageLLM, TextLLM
from bot.pipeline import Pipeline
from bot.search import WebSearch


def create_pipeline(config):
    pipeline = Pipeline(
        llm=TextLLM(config.gemini_api_key, config.gemini_text_models,
                    config.vertex_api_key, config.vertex_text_models),
        images=ImageLLM(config.vertex_api_key, config.image_models),
        search=WebSearch(config.tavily_api_key) if config.tavily_api_key else None,
        prompts_dir=config.prompts_dir, max_queries=config.search_max_queries,
    )
    pipeline.editable_enabled = config.editable_pptx_enabled
    pipeline.editable_service = EditableService(pipeline.llm, pipeline.images, config.prompts_dir,
                                                presets_dir=config.presets_dir)
    return pipeline
