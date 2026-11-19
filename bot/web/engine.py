"""Run durable actions using the bot's existing model pipeline and exporters."""
import json
from pathlib import Path

from bot import presentation
from bot.generation import generate_standard
from bot.pipeline import ASKED_QUESTIONS, PLAN, SURVEY_CONFIRMED, BACKGROUND_ACCEPTED
from bot.web.storage import project, update_project


class PresentationEngine:
    def __init__(self, db, pipeline, bank, files_dir):
        self.db, self.pipeline, self.bank, self.files_dir = db, pipeline, bank, files_dir

    async def run(self, sid, action, progress):
        db, pipeline = self.db, self.pipeline
        p = await project(db, sid)
        if not p or p['status'] != 'in_progress':
            return
        if action == 'analyze':
            await progress('Изучаю описание и исходные материалы…')
            found, model = await presentation.analyze_survey(db, self.bank, pipeline, sid)
            questions = self.bank.load().questions
            for q in questions:
                if q.key in found:
                    await db.save_answer(sid, q.key, q.text, found[q.key])
            missing = [q.key for q in questions if q.key not in found]
            await db.save_artifact(sid, ASKED_QUESTIONS, json.dumps(missing), model)
            # Preserve the existing chat behavior; the Mini App always offers review.
            if p['origin'] == 'chat' and not missing:
                await db.save_artifact(sid, SURVEY_CONFIRMED, '', model='auto')
                return await self.run(sid, 'plan', progress)
            await update_project(db, sid, stage='survey', waiting_for=None)
        elif action == 'plan':
            await presentation.prepare_plan(db, self.bank, pipeline, sid, progress)
            from bot.plan import plan_slides
            slides = plan_slides(await db.get_artifact(sid, PLAN.kind))
            if not slides or len(slides) > 10 or list(slides) != list(range(1, len(slides)+1)):
                await db.delete_artifact(sid, PLAN.kind)
                raise ValueError('План должен содержать от 1 до 10 последовательно пронумерованных слайдов')
            await update_project(db, sid, stage='plan', waiting_for=None)
        elif action in ('background', 'background_change'):
            await progress('Рисую фон для слайдов…')
            await presentation.draw_background(db, pipeline, self.files_dir, sid, change=action == 'background_change')
            await update_project(db, sid, stage='background', waiting_for=None)
        elif action == 'generate':
            if await db.get_artifact(sid, BACKGROUND_ACCEPTED) is None:
                raise ValueError('Фон ещё не принят')
            await update_project(db, sid, stage='generating', waiting_for=None)
            mode = await db.get_artifact(sid, 'generation_mode')
            if mode == 'editable':
                await pipeline.editable_service.generate(db, sid, self.files_dir, progress)
            elif mode == 'standard':
                a = await db.get_artifacts(sid)
                if not all(a.get(k) and Path(a[k]).is_file() for k in ('result_pdf', 'result_pptx')):
                    await generate_standard(db, pipeline, self.files_dir, sid, progress)
            else:
                raise ValueError('Режим генерации не выбран')
            if await db.is_active(sid):
                await update_project(db, sid, stage='complete', waiting_for=None)
                await db.finish_session(sid)
        else:
            raise ValueError('Неизвестное действие')
