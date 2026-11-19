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
    editable_llm = pipeline.llm
    if config.editable_text_models:
        # Layout quality depends on the model most: allow a stronger one only for these stages.
        editable_llm = TextLLM(config.gemini_api_key, [], config.vertex_api_key, list(config.editable_text_models))
    reviewer = (TextLLM(config.gemini_api_key, [], config.vertex_api_key, list(config.editable_review_models))
                if config.editable_review_models else None)
    pipeline.editable_service = EditableService(editable_llm, pipeline.images, config.prompts_dir,
                                                presets_dir=config.presets_dir, thinking=config.editable_thinking,
                                                parallel=config.editable_parallel, reviewer=reviewer)
    return pipeline
