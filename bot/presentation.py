"""Presentation operations shared by Telegram and HTTP; no transport objects."""
import json
from pathlib import Path

from bot.pipeline import (
    ASKED_QUESTIONS, DESCRIPTION, SOURCES_NOTE, SOURCES, STEPS, DATA, TZ,
    RESEARCH_ANSWER_KEY, search_declined, BACKGROUND_FIELDS, STYLE_REF, BACKGROUND,
    BACKGROUND_ALT, BACKGROUND_ALT_FIELDS, BACKGROUND_ATTEMPT, BACKGROUND_COMMENT,
    ATTEMPT_EDIT, BackgroundFields,
)
from bot.runtime import load_image, save_image, session_ratio


async def analyze_survey(db, bank, pipeline, sid):
    artifacts = await db.get_artifacts(sid)
    if ASKED_QUESTIONS in artifacts:
        return await db.get_answers(sid), None
    return await pipeline.analyze_survey(bank.load(), artifacts.get(DESCRIPTION, ''),
                                       artifacts.get(SOURCES_NOTE), await db.get_source_files(sid))


async def prepare_plan(db, bank, pipeline, sid, progress):
    answers = await db.get_answers(sid)
    for step in STEPS:
        if await db.get_artifact(sid, step.kind) is not None:
            continue
        await progress(f'Готовлю: {step.title.lower()}…')
        if step is DATA and await db.get_artifact(sid, SOURCES) is None:
            sources = '' if search_declined(answers.get(RESEARCH_ANSWER_KEY)) else await pipeline.find_sources(await db.get_artifact(sid, TZ.kind))
            await db.save_artifact(sid, SOURCES, sources)
        artifacts = await db.get_artifacts(sid)
        result, model = await pipeline.run_step(step, bank.load(), answers, artifacts, await db.get_source_files(sid))
        await db.save_artifact(sid, step.kind, result, model)


async def draw_background(db, pipeline, files_dir, sid, *, change=False):
    a = await db.get_artifacts(sid)
    target = BACKGROUND_ALT if change else BACKGROUND
    if a.get(target) and Path(a[target]).is_file():
        return load_image(a[target]), Path(a[target])
    ratio = await session_ratio(db, sid)
    reference = load_image(a[STYLE_REF]) if a.get(STYLE_REF) else None
    if BACKGROUND_FIELDS in a:
        fields = BackgroundFields.model_validate_json(a[BACKGROUND_FIELDS])
    else:
        fields = await pipeline.background_fields(a[TZ.kind], has_reference=reference is not None)
        await db.save_artifact(sid, BACKGROUND_FIELDS, fields.model_dump_json())
    if change:
        first = load_image(a[BACKGROUND])
        if a[BACKGROUND_ATTEMPT] == ATTEMPT_EDIT:
            image, model = await pipeline.edit_background(first, a[BACKGROUND_COMMENT], ratio)
        else:
            if reference is None:
                if BACKGROUND_ALT_FIELDS in a:
                    fields = BackgroundFields.model_validate_json(a[BACKGROUND_ALT_FIELDS])
                else:
                    fields = await pipeline.background_fields(a[TZ.kind], False, previous=fields)
                    await db.save_artifact(sid, BACKGROUND_ALT_FIELDS, fields.model_dump_json())
            image, model = await pipeline.background(fields, ratio, reference, previous=first)
    else:
        image, model = await pipeline.background(fields, ratio, reference)
    path = save_image(files_dir, sid, 'Фон_2' if change else 'Фон', image)
    await db.save_artifact(sid, target, str(path), model)
    return image, path
