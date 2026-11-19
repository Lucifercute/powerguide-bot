"""Resumable visual plan -> HTML -> illustrations -> editable PPTX."""
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
from .html import package, validate_deck, validate_plan, normalize_deck, deck_contract
from .models import Deck, VisualPlan

EXPORT_VERSION = 9

def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def write(path, value):
    """Atomic publication: a cancelled stage cannot leave a partial cached result."""
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


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
    def __init__(self, llm, images, prompts_dir=Path('prompts'), *, prompt_set=None, presets_dir=Path('presets')):
        self.llm, self.images = llm, images
        self.prompts_dir = Path(prompts_dir)
        self.prompt_set = prompt_set
        self.presets = PresetStore(Path(presets_dir))

    async def _json_stage(self, folder, name, prompt, schema, validate, background, *, normalize=None):
        final = folder / f'{name}.json'
        if final.exists():
            value = schema.model_validate_json(final.read_text())
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
            request = {'prompt': actual, 'background_sha256': sha(background[0]),
                       'background_file': 'background' + ('.png' if background[1] == 'image/png' else '.jpg'),
                       'schema': schema.model_json_schema(),
                       'config': {'response_mime_type': 'application/json', 'max_output_tokens': 30000,
                                  'thinking_level': 'LOW'}}
            request_path = prefix.with_suffix('.request.json')
            raw = prefix.with_suffix('.response.json')
            if not raw.exists():
                write(request_path, request)
                text, model = await self.llm.generate(
                    [actual, 'Image 1: exact background.', types.Part.from_bytes(data=background[0], mime_type=background[1])],
                    types.GenerateContentConfig(response_mime_type='application/json', response_schema=schema,
                                                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
                                                max_output_tokens=30000))
                write(raw, {'model': model, 'text': text})
            saved = json.loads(raw.read_text())
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

    async def generate(self, db, session_id, files_dir, status):
        # Check the local dependency before spending API quota.
        chrome_path()
        folder = files_dir / str(session_id) / 'editable'
        folder.mkdir(parents=True, exist_ok=True)
        saved_prompts = folder / 'prompt-set.json'
        if saved_prompts.exists():
            instructions = prompts.PromptSet(**json.loads(saved_prompts.read_text()))
        else:
            instructions = self.prompt_set or prompts.load(self.prompts_dir)
            write(saved_prompts, vars(instructions))
        artifacts = await db.get_artifacts(session_id)
        approved = artifacts[PLAN.kind]
        numbers = list(plan_slides(approved))
        if not 1 <= len(numbers) <= MAX_SLIDES or numbers != list(range(1, len(numbers) + 1)):
            raise ValueError('Неверные номера или количество слайдов согласованного плана')
        background = image_data(chosen_background(artifacts))
        ratio = await session_ratio(db, session_id)
        a, b = map(float, ratio.split(':'))
        width = 1600 if a >= b else 900
        height = round(width * b / a)
        pictures = await load_user_images(db, session_id)
        pictures = {n: v for n, v in pictures.items() if n in numbers}
        user_assets = {n: image_data(v.path) for n, v in pictures.items()}
        context = {'session_id': session_id, 'slide_count': len(numbers),
                   'canvas': {'width': width, 'height': height}, 'APPROVED_PLAN': approved,
                   'technical_brief': artifacts.get(TZ.kind, ''), 'factual_data': artifacts.get(DATA.kind, ''),
                   'style_description': artifacts.get(STYLE, ''), 'style_preset': artifacts.get(STYLE_PRESET),
                   'user_images': {str(n): {'caption': v.caption, 'width': v.width, 'height': v.height,
                                          'sha256': sha(user_assets[n][0])} for n, v in pictures.items()}}
        fingerprint = sha(json.dumps(context, ensure_ascii=False) + sha(background[0]))
        request = folder / 'input.json'
        if request.exists():
            saved = json.loads(request.read_text())
            if saved.get('source_fingerprint', saved['fingerprint']) != fingerprint:
                raise ValueError('Исходные материалы изменились после начала генерации')
            # Resume with the passport/font policy used by the initial request,
            # even if the preset files have since been updated.
            context = saved['context']
        else:
            source_fingerprint = fingerprint
            preset_number = context.get('style_preset')
            if preset_number and str(preset_number).isdigit():
                context.update(self.presets.html_options(int(preset_number)))
            fingerprint = sha(json.dumps(context, ensure_ascii=False) + sha(background[0]))
            write(request, {'fingerprint': fingerprint, 'source_fingerprint': source_fingerprint,
                            'context': context, 'background_sha256': sha(background[0])})
        bgfile = folder / ('background.png' if background[1] == 'image/png' else 'background.jpg')
        if not bgfile.exists():
            bgfile.write_bytes(background[0])
        final_html, final_pptx = folder / 'presentation.html', folder / 'presentation.pptx'
        completed = folder / 'completed.json'
        if completed.exists() and final_html.exists() and final_pptx.exists():
            meta = json.loads(completed.read_text())
            if (meta.get('export_version') == EXPORT_VERSION
                    and meta.get('html_sha256') == sha(final_html.read_bytes())
                    and meta.get('pptx_sha256') == sha(final_pptx.read_bytes())):
                await db.save_artifact(session_id, 'editable_html', str(final_html))
                await db.save_artifact(session_id, 'editable_pptx', str(final_pptx))
                return final_html, final_pptx
        await status('Проектирую слайды для редактируемой презентации…')
        plan = await self._json_stage(folder, 'visual-plan', instructions.PLAN + '\nINPUT\n' + json.dumps(context, ensure_ascii=False),
                                     VisualPlan, lambda p: validate_plan(p, len(numbers)), background)
        await status('Создаю оформление и инфографику…')
        prompt = instructions.HTML + '\nINPUT\n' + json.dumps(context, ensure_ascii=False) + '\nVISUAL_PLAN\n' + plan.model_dump_json()
        prompt += deck_contract(plan, pictures)
        deck = await self._json_stage(folder, 'deck', prompt, Deck, lambda d: validate_deck(d, plan, pictures), background,
                                      normalize=lambda d: normalize_deck(d, plan))
        for layout_attempt in range(2):
            if layout_attempt:
                deck = await self._json_stage(folder, 'layout-repair', prompt + '\nИсправь перечисленные ошибки вёрстки и композиции. '
                    'Сохрани все изображения и их id/prompts, содержание и инфографику.\n'
                    + (folder / 'layout-error.txt').read_text() + '\nPREVIOUS_DECK\n' + deck.model_dump_json(),
                    Deck, lambda d: validate_deck(d, plan, pictures), background,
                    normalize=lambda d: normalize_deck(d, plan))
            assets = {}
            for i, spec in enumerate(deck.images, 1):
                image_prompt = (instructions.IMAGE + '\nSTYLE_PASSPORT\n' + context.get('style_description', '')
                                + '\nILLUSTRATION\n' + spec.prompt)
                image_hash = sha(image_prompt + spec.aspect_ratio + sha(background[0]))
                meta_path = folder / f'image-{spec.id}.json'
                meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                path = folder / meta.get('file', '__missing__')
                if path.is_file() and meta.get('fingerprint') == image_hash:
                    assets[spec.id] = image_data(path)
                    continue
                await status(f'Создаю иллюстрации: {i} из {len(deck.images)}…')
                write(folder / f'image-{spec.id}.request.json', {'prompt': image_prompt,
                      'aspect_ratio': spec.aspect_ratio, 'background_sha256': sha(background[0])})
                image, model = await self.images.generate(
                    [image_prompt, types.Part.from_bytes(data=background[0], mime_type=background[1])], spec.aspect_ratio)
                path = folder / f'image-{spec.id}{mimetypes.guess_extension(image.mime_type) or ".png"}'
                temp = path.with_suffix(path.suffix + '.tmp')
                temp.write_bytes(image.data)
                temp.replace(path)
                assets[spec.id] = image_data(path)
                write(meta_path, {'file': path.name, 'fingerprint': image_hash, 'model': model, 'prompt': image_prompt})
            write(final_html, await asyncio.to_thread(package, deck, plan, assets, user_assets, background,
                                                     width, height, session_id, context.get('font_policy')))
            await status('Собираю PowerPoint и проверяю расположение текста…')
            try:
                await render(final_html, final_pptx, len(numbers), width, height)
            except ValueError as error:
                write(folder / 'layout-error.txt', str(error))
                if layout_attempt:
                    raise
                await status('Уточняю вёрстку и композицию слайдов…')
                continue
            break
        write(completed, {'html_sha256': sha(final_html.read_bytes()), 'pptx_sha256': sha(final_pptx.read_bytes()),
                          'slides': len(numbers), 'width': width, 'height': height, 'export_version': EXPORT_VERSION})
        await db.save_artifact(session_id, 'editable_html', str(final_html))
        await db.save_artifact(session_id, 'editable_pptx', str(final_pptx))
        return final_html, final_pptx
