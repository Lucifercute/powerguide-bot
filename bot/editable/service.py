"""Resumable visual plan -> per-slide HTML -> illustrations -> editable PPTX.

Every model answer is saved next to its request, so an interrupted or failed
generation resumes without repeating finished stages. Slides are laid out
independently and in parallel; shared design tokens and the deck frame come
from the code, and only slides that fail the rendered checks are redone.
"""
import asyncio
import hashlib
import io
import json
import mimetypes
from pathlib import Path

from google.genai import types
from PIL import Image as PILImage

from bot.pipeline import PLAN, TZ, DATA, STYLE, STYLE_PRESET, MAX_SLIDES
from bot.plan import plan_slides
from bot.presets import PresetStore
from bot.runtime import chosen_background, load_user_images, session_ratio
from . import prompts
from .browser import chrome_path, render
from .html import assemble, normalize_markup, package, slide_contract, validate_markup, validate_plan
from .models import SlideMarkup, SlideReview, VisualPlan

EXPORT_VERSION = 11
THINKING = {'low': types.ThinkingLevel.LOW, 'medium': types.ThinkingLevel.MEDIUM, 'high': types.ThinkingLevel.HIGH}
# Pro with deep reasoning spends a large part of the budget on thoughts.
MAX_OUTPUT_TOKENS = 65536
# Facts are checked when the visual plan is made; slide layout needs only the plan and the style.
LAYOUT_OMITS = ('technical_brief', 'factual_data')


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def write(path, value):
    """Atomic publication: a cancelled stage cannot leave a partial cached result."""
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2),
                    encoding='utf-8')
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def image_data(path):
    p = Path(path)
    data = p.read_bytes()
    with PILImage.open(io.BytesIO(data)) as im:
        im.verify()
    mime = mimetypes.guess_type(p.name)[0]
    if mime not in {'image/png', 'image/jpeg'}:
        with PILImage.open(io.BytesIO(data)) as im:
            out = io.BytesIO()
            im.save(out, format='PNG')
            return out.getvalue(), 'image/png'
    return data, mime


class EditableService:
    def __init__(self, llm, images, prompts_dir=Path('prompts'), *, prompt_set=None, presets_dir=Path('presets'),
                 thinking='low', parallel=4, reviewer=None):
        self.llm, self.images = llm, images
        # Optional cheap vision model that looks at each rendered slide for visible defects.
        self.reviewer = reviewer
        self.prompts_dir = Path(prompts_dir)
        self.prompt_set = prompt_set
        self.presets = PresetStore(Path(presets_dir))
        self.thinking = thinking
        self.parallel = parallel

    def _config(self, schema):
        return types.GenerateContentConfig(
            response_mime_type='application/json', response_schema=schema, max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING[self.thinking]))

    async def _json_stage(self, folder, name, prompt, schema, validate, background, *, normalize=None, extra=()):
        """Request, validate and cache one structured answer; up to two corrective retries.
        extra: (caption, bytes, mime) images sent after the background."""
        final = folder / f'{name}.json'
        if final.exists():
            value = schema.model_validate_json(final.read_text(encoding='utf-8'))
            if normalize:
                value = normalize(value)
            validate(value)
            return value
        feedback = ''
        # Revalidate saved responses first; each explicit retry may make at most
        # two additional requests if earlier responses still violate the contract.
        existing = len(list(folder.glob(f'{name}-attempt-*.response.json')))
        for attempt in range(existing + 2):
            prefix = folder / f'{name}-attempt-{attempt + 1}'
            actual = prompt + feedback
            raw = prefix.with_suffix('.response.json')
            if not raw.exists():
                write(prefix.with_suffix('.request.json'), {
                    'prompt': actual, 'background_sha256': sha(background[0]), 'schema': schema.model_json_schema(),
                    'images': [{'caption': c, 'sha256': sha(d)} for c, d, _ in extra],
                    'config': {'thinking_level': self.thinking, 'max_output_tokens': MAX_OUTPUT_TOKENS}})
                usage = {}
                parts = [actual, 'Image 1: exact background.', types.Part.from_bytes(data=background[0], mime_type=background[1])]
                for caption, data, mime in extra:
                    parts += [caption, types.Part.from_bytes(data=data, mime_type=mime)]
                text, model = await self.llm.generate(parts, self._config(schema), usage=usage)
                write(raw, {'model': model, 'text': text, 'usage': usage})
            saved = read_json(raw)
            try:
                value = schema.model_validate_json(saved['text'])
                if normalize:
                    value = normalize(value)
                validate(value)
            except ValueError as error:
                feedback = ('\nИсправь предыдущий ответ. Ошибка проверки: ' + str(error)[:3000]
                            + '\nПРЕДЫДУЩИЙ ОТВЕТ:\n' + saved['text'])
                write(prefix.with_suffix('.validation.txt'), str(error))
                continue
            write(final, value.model_dump(mode='json'))
            return value
        raise ValueError(f'Не удалось проверить этап {name}; сохранены запросы и ответы')

    async def _slides(self, folder, plan, numbers, prompt_for, pictures, background, suffix='', extra_for=None):
        """Lay out the given slides in parallel; returns {number: SlideMarkup}."""
        gate = asyncio.Semaphore(self.parallel)
        specs = {s.number: s for s in plan.slides}

        async def one(n):
            spec = specs[n]
            async with gate:
                return n, await self._json_stage(
                    folder, f'slide-{n}{suffix}', prompt_for(spec), SlideMarkup,
                    lambda m: validate_markup(m, spec, n in pictures), background,
                    normalize=lambda m: normalize_markup(m, spec), extra=extra_for(n) if extra_for else ())
        return dict(await asyncio.gather(*(one(n) for n in numbers)))

    async def _review(self, folder, plan, numbers, instructions, round_):
        """A vision model looks at each rendered slide; returns {number: [problems]}. Cached per render."""
        if not self.reviewer or not instructions.REVIEW.strip():
            return {}
        specs = {s.number: s for s in plan.slides}
        gate = asyncio.Semaphore(4)
        config = types.GenerateContentConfig(
            response_mime_type='application/json', response_schema=SlideReview, max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW))

        async def one(n):
            path = folder / f'review-{n}-r{round_ + 1}.json'
            picture = (folder / f'preview-slide-{n}.png').read_bytes()
            if path.exists() and read_json(path).get('render_sha256') == sha(picture):
                return n, read_json(path)['problems']
            spec = specs[n]
            slide = {'title': spec.title, 'visual_type': spec.visual_type, 'form': spec.form,
                     'nodes': [node.label for node in spec.nodes],
                     'links': [f'{e.source} → {e.target}' for e in spec.edges]}
            async with gate:
                usage = {}
                try:
                    text, model = await self.reviewer.generate(
                        [instructions.REVIEW + '\nSLIDE\n' + json.dumps(slide, ensure_ascii=False),
                         'Image 1: the rendered slide.', types.Part.from_bytes(data=picture, mime_type='image/png')],
                        config, usage=usage)
                    problems = SlideReview.model_validate_json(text).problems
                except Exception as error:  # the review only adds hints; a failure must not stop the export
                    write(path.with_suffix('.error.txt'), f'{type(error).__name__}: {error}')
                    return n, []
            write(path, {'render_sha256': sha(picture), 'model': model, 'usage': usage, 'problems': problems})
            return n, problems
        return {n: p for n, p in await asyncio.gather(*(one(n) for n in numbers)) if p}

    async def _illustrations(self, folder, deck, instructions, context, background, status):
        assets = {}
        for i, spec in enumerate(deck.images, 1):
            image_prompt = (instructions.IMAGE + '\nSTYLE_PASSPORT\n' + context.get('style_description', '')
                            + '\nILLUSTRATION\n' + spec.prompt)
            image_hash = sha(image_prompt + spec.aspect_ratio + sha(background[0]))
            meta_path = folder / f'image-{spec.id}.json'
            meta = read_json(meta_path) if meta_path.exists() else {}
            path = folder / meta.get('file', '__missing__')
            if path.is_file() and meta.get('fingerprint') == image_hash:
                assets[spec.id] = image_data(path)
                continue
            await status(f'Создаю иллюстрации: {i} из {len(deck.images)}…')
            write(folder / f'image-{spec.id}.request.json', {'prompt': image_prompt,
                  'aspect_ratio': spec.aspect_ratio, 'background_sha256': sha(background[0])})
            usage = {}
            image, model = await self.images.generate(
                [image_prompt, types.Part.from_bytes(data=background[0], mime_type=background[1])],
                spec.aspect_ratio, usage=usage)
            path = folder / f'image-{spec.id}{mimetypes.guess_extension(image.mime_type) or ".png"}'
            temp = path.with_suffix(path.suffix + '.tmp')
            temp.write_bytes(image.data)
            temp.replace(path)
            assets[spec.id] = image_data(path)
            write(meta_path, {'file': path.name, 'fingerprint': image_hash, 'model': model,
                              'prompt': image_prompt, 'usage': usage})
        return assets

    async def _context(self, db, session_id, folder, numbers, background):
        artifacts = await db.get_artifacts(session_id)
        ratio = await session_ratio(db, session_id)
        a, b = map(float, ratio.split(':'))
        width = 1600 if a >= b else 900
        height = round(width * b / a)
        pictures = {n: v for n, v in (await load_user_images(db, session_id)).items() if n in numbers}
        user_assets = {n: image_data(v.path) for n, v in pictures.items()}
        context = {'slide_count': len(numbers), 'canvas': {'width': width, 'height': height},
                   'APPROVED_PLAN': artifacts[PLAN.kind],
                   'technical_brief': artifacts.get(TZ.kind, ''), 'factual_data': artifacts.get(DATA.kind, ''),
                   'style_description': artifacts.get(STYLE, ''), 'style_preset': artifacts.get(STYLE_PRESET),
                   'user_images': {str(n): {'caption': v.caption, 'width': v.width, 'height': v.height,
                                            'sha256': sha(user_assets[n][0])} for n, v in pictures.items()}}
        fingerprint = sha(json.dumps(context, ensure_ascii=False) + sha(background[0]))
        request = folder / 'input.json'
        if request.exists():
            saved = read_json(request)
            if saved['source_fingerprint'] != fingerprint:
                raise ValueError('Исходные материалы изменились после начала генерации')
            # Resume with the passport/font policy of the initial request,
            # even if the preset files have since been updated.
            return saved['context'], pictures, user_assets
        preset = context.get('style_preset')
        if preset and str(preset).isdigit():
            context.update(self.presets.html_options(int(preset)))
        write(request, {'source_fingerprint': fingerprint, 'context': context, 'background_sha256': sha(background[0])})
        return context, pictures, user_assets

    async def generate(self, db, session_id, files_dir, status):
        chrome_path()  # check the local dependency before spending API quota
        folder = files_dir / str(session_id) / 'editable'
        folder.mkdir(parents=True, exist_ok=True)
        saved_prompts = folder / 'prompt-set.json'
        if saved_prompts.exists():
            instructions = prompts.PromptSet(**read_json(saved_prompts))
        else:
            instructions = self.prompt_set or prompts.load(self.prompts_dir)
            write(saved_prompts, vars(instructions))
        artifacts = await db.get_artifacts(session_id)
        numbers = list(plan_slides(artifacts[PLAN.kind]))
        if not 1 <= len(numbers) <= MAX_SLIDES or numbers != list(range(1, len(numbers) + 1)):
            raise ValueError('Неверные номера или количество слайдов согласованного плана')
        background = image_data(chosen_background(artifacts))
        context, pictures, user_assets = await self._context(db, session_id, folder, numbers, background)
        width, height = context['canvas']['width'], context['canvas']['height']
        bgfile = folder / ('background.png' if background[1] == 'image/png' else 'background.jpg')
        if not bgfile.exists():
            bgfile.write_bytes(background[0])

        final_html, final_pptx = folder / 'presentation.html', folder / 'presentation.pptx'
        completed = folder / 'completed.json'
        if completed.exists() and final_html.exists() and final_pptx.exists():
            meta = read_json(completed)
            if (meta.get('export_version') == EXPORT_VERSION
                    and meta.get('html_sha256') == sha(final_html.read_bytes())
                    and meta.get('pptx_sha256') == sha(final_pptx.read_bytes())):
                await db.save_artifact(session_id, 'editable_html', str(final_html))
                await db.save_artifact(session_id, 'editable_pptx', str(final_pptx))
                return final_html, final_pptx

        await status('Проектирую слайды для редактируемой презентации…')
        plan = await self._json_stage(folder, 'visual-plan', instructions.PLAN + '\nINPUT\n' + json.dumps(context, ensure_ascii=False),
                                      VisualPlan, lambda p: validate_plan(p, len(numbers)), background)

        await status('Верстаю слайды…')
        layout_input = {k: v for k, v in context.items() if k not in LAYOUT_OMITS}
        base = (instructions.HTML + '\nINPUT\n' + json.dumps(layout_input, ensure_ascii=False)
                + '\nVISUAL_PLAN\n' + plan.model_dump_json())

        def prompt_for(spec, extra=''):
            return base + slide_contract(spec, spec.number in pictures) + extra

        markups = await self._slides(folder, plan, numbers, prompt_for, pictures, background)
        result = None
        for round_ in range(2):
            deck = assemble(plan, markups, context.get('style_description', ''))
            assets = await self._illustrations(folder, deck, instructions, context, background, status)
            write(final_html, await asyncio.to_thread(package, deck, plan, assets, user_assets, background,
                                                     width, height, session_id, context.get('font_policy')))
            await status('Собираю PowerPoint и проверяю вёрстку…')
            result = await render(final_html, final_pptx, len(numbers), width, height, previews=True)
            issues = {int(k): list(v) for k, v in result['issues'].items()}
            # First round: every slide; second: only the redone ones (the rest did not change).
            review = await self._review(folder, plan, sorted(repaired) if round_ else numbers, instructions, round_)
            for n, problems in review.items():
                issues.setdefault(n, []).extend('Визуальная проверка: ' + p for p in problems)
            result['review'], result['issues'] = review, issues
            if not issues or round_:
                break
            await status(f'Дорабатываю слайды: {", ".join(map(str, sorted(issues)))}…')
            repaired = sorted(issues)

            def repair_prompt(spec, issues=issues):
                return prompt_for(spec, '\nИСПРАВЬ ВЁРСТКУ ЭТОГО СЛАЙДА по замечаниям проверки готового рендера. '
                                  'Image 2 — рендер твоей текущей версии, как его видит зритель: найди на нём '
                                  'каждое замечание и другие видимые дефекты. '
                                  'Сохрани тексты, форму, иконки и иллюстрацию.\nЗАМЕЧАНИЯ:\n- '
                                  + '\n- '.join(issues[spec.number])
                                  + '\nPREVIOUS_SLIDE\n' + markups[spec.number].model_dump_json())

            def render_of(n):
                return [('Image 2: current render of this slide.',
                         (folder / f'preview-slide-{n}.png').read_bytes(), 'image/png')]
            markups.update(await self._slides(folder, plan, repaired, repair_prompt, pictures,
                                              background, suffix='-repair', extra_for=render_of))
        if any(s['issues'] for s in result['textBounds']):
            raise ValueError('Текст выходит за границы слайдов: ' + json.dumps(result['textBounds'], ensure_ascii=False)[:3000])
        write(completed, {'html_sha256': sha(final_html.read_bytes()), 'pptx_sha256': sha(final_pptx.read_bytes()),
                          'slides': len(numbers), 'width': width, 'height': height, 'export_version': EXPORT_VERSION,
                          'unresolved_issues': result['issues']})
        await db.save_artifact(session_id, 'editable_html', str(final_html))
        await db.save_artifact(session_id, 'editable_pptx', str(final_pptx))
        return final_html, final_pptx
