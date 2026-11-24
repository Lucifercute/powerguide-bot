import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage

from bot import generation_modes as modes
from bot.db import Database
from bot.editable.html import package, validate_deck
from bot.editable.models import Deck, SlideMarkup, VisualPlan
from bot.editable.prompts import PromptSet, load as load_private_prompts
from bot.editable.service import EditableService
from bot.pipeline import BACKGROUND, BACKGROUND_ACCEPTED, PLAN, STYLE, DESCRIPTIONS, user_image_kind
from bot.user_images import UserImage


def fixtures(width=1600, height=900):
    plan = VisualPlan.model_validate({'title': 'Проверка', 'slides': [
        {'number': 1, 'title': 'Портфолио', 'message': 'Проверка', 'visual_type': 'cover',
         'composition': 'Текст и изображение', 'nodes': [], 'edges': [], 'metrics': [], 'evidence': []},
        {'number': 2, 'title': 'Процесс', 'message': 'Два шага', 'visual_type': 'process',
         'composition': 'Узлы и линия', 'nodes': [{'id': 'a', 'label': 'Вход', 'detail': ''}, {'id': 'b', 'label': 'Выход', 'detail': ''}],
         'edges': [{'source': 'a', 'target': 'b', 'label': ''}], 'metrics': [], 'evidence': []},
    ]})
    deck = Deck.model_validate({'title': 'Проверка', 'images': [
        {'id': 'cover', 'slide': 1, 'aspect_ratio': '1:1', 'prompt': 'Conceptual illustration', 'alt': 'Иллюстрация'}],
        'html': f'''<!DOCTYPE html><html><head><style>
        *{{box-sizing:border-box}}body{{margin:0;font-family:Arial}}section.slide{{width:{width}px;height:{height}px;position:relative;padding:60px}}
        .badge{{border:2px solid #123456;border-radius:999px;width:700px;height:40px;font-size:22px}}
        h1{{font-size:48px}}.node{{position:absolute;top:250px;font-size:24px;border:2px solid blue;padding:12px}}
        </style></head><body><div id="presentation-viewport">
        <section class="slide" data-slide="1" data-visual-type="cover"><div class="badge">ПОРТФОЛИО</div><h1>Проверка 12,5%</h1>
        <img data-asset-id="cover" style="width:160px;height:160px;object-fit:contain"></section>
        <section class="slide" data-slide="2" data-visual-type="process"><h1>Процесс</h1>
        <div data-infographic="process"><div data-node="a" class="node" style="left:100px">Вход</div>
        <div data-node="b" class="node" style="left:600px">Выход</div>
        <div data-connector="a-b" style="position:absolute;left:220px;top:275px;width:380px;height:2px;background:blue"></div></div>
        <img data-user-slide="2" style="position:absolute;left:80px;top:400px;width:300px;height:200px;object-fit:contain">
        </section></div></body></html>'''})
    return plan, deck


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root/'test.db')
        await self.db.connect()
        self.sid = await self.db.start_session(42)
        await self.db.save_artifact(self.sid, BACKGROUND_ACCEPTED, '')
        self.message = AsyncMock()
        self.pipeline = SimpleNamespace(editable_enabled=True, editable_service=SimpleNamespace(generate=AsyncMock()))
        # The choice between modes exists only with standard generation turned on (off by default).
        env = patch.dict('os.environ', {'STANDARD_GENERATION_ENABLED': '1'})
        env.start()
        self.addCleanup(env.stop)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_default_is_editable_without_a_question(self):
        with patch.dict('os.environ', {'STANDARD_GENERATION_ENABLED': '0'}), \
                patch.object(modes, 'run_editable', AsyncMock()) as editable, \
                patch.object(modes, 'run_slides', AsyncMock()) as old:
            await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
            editable.assert_awaited_once()
            old.assert_not_awaited()
            self.message.answer.assert_not_awaited()  # no mode question
            self.assertEqual(await self.db.get_artifact(self.sid, modes.MODE), 'editable')
            self.assertIsNone(await self.db.get_waiting_for(self.sid))
            self.assertEqual(modes.failed_keyboard(self.sid).inline_keyboard[1:], [])  # no standard fallback

    def callback(self, data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=42), answer=AsyncMock(), message=self.message)

    async def test_waits_for_choice_then_standard_uses_old_generator(self):
        with patch.object(modes, 'run_slides', AsyncMock()) as old:
            await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
            old.assert_not_awaited()
            self.assertEqual(await self.db.get_waiting_for(self.sid), modes.WAIT_MODE)
            await modes.on_generation_mode(self.callback(f'genmode:{self.sid}:standard'), self.db, self.pipeline, self.root)
            old.assert_awaited_once()
            self.assertEqual(await self.db.get_artifact(self.sid, modes.MODE), 'standard')

    async def test_old_generation_and_feature_disabled_skip_choice(self):
        for progress in (False, True):
            await self.db.delete_artifact(self.sid, modes.MODE)
            self.pipeline.editable_enabled = progress
            if progress:
                await self.db.save_artifact(self.sid, DESCRIPTIONS, 'saved')
            with patch.object(modes, 'run_slides', AsyncMock()) as old:
                await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
                old.assert_awaited_once()

    async def test_stale_and_duplicate_choice_do_not_start_jobs(self):
        await self.db.set_waiting_for(self.sid, modes.WAIT_MODE)
        with patch.object(modes, 'continue_generation', AsyncMock()) as run:
            await modes.on_generation_mode(self.callback(f'genmode:{self.sid+1}:editable'), self.db, self.pipeline, self.root)
            run.assert_not_awaited()
            cb = self.callback(f'genmode:{self.sid}:editable')
            await asyncio.gather(*(modes.on_generation_mode(cb, self.db, self.pipeline, self.root) for _ in range(2)))
            run.assert_awaited_once()
            await modes.on_generation_mode(self.callback(f'genmode:{self.sid}:standard'), self.db, self.pipeline, self.root)
            self.assertEqual(await self.db.get_artifact(self.sid, modes.MODE), 'editable')

    async def test_failed_editable_resumes_after_database_reopen_and_can_fallback(self):
        await self.db.save_artifact(self.sid, modes.MODE, 'editable')
        self.pipeline.editable_service.generate.side_effect = RuntimeError('test export failure')
        with self.assertLogs(modes.log, level='ERROR'):
            await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
        self.assertEqual(await self.db.get_waiting_for(self.sid), modes.WAIT_FAILED)
        await self.db.close()
        await self.db.connect()
        with patch.object(modes, 'run_editable', AsyncMock()) as new:
            await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
            new.assert_awaited_once()
        await self.db.set_waiting_for(self.sid, modes.WAIT_FAILED)
        with patch.object(modes, 'run_slides', AsyncMock()) as old:
            await modes.on_generation_mode(self.callback(f'genfallback:{self.sid}'), self.db, self.pipeline, self.root)
            old.assert_awaited_once()

    async def test_preset_and_custom_background_offer_choice(self):
        from bot import handlers
        from bot.presets import PresetStore
        p = self.root/'presets/1'
        p.mkdir(parents=True)
        for name in ('preview.jpg', 'background.png'):
            PILImage.new('RGB', (160, 90), 'white').save(p/name)
        (p/'style.md').write_text('Стиль')
        presets = PresetStore(self.root/'presets')
        # Follow the real preceding dialogue step. It persists slide_images_done
        # even when the user did not attach any photos.
        await handlers.finish_slide_images(self.message, self.db, presets, self.sid)
        self.assertIsNotNone(await self.db.get_artifact(self.sid, handlers.SLIDE_IMAGES_DONE))
        with patch.object(modes, 'run_slides', AsyncMock()) as old:
            await handlers.on_style_preset(self.callback('style_preset:1'), self.db, self.pipeline, self.root, presets)
            old.assert_not_awaited()
        self.assertEqual(await self.db.get_waiting_for(self.sid), modes.WAIT_MODE)
        await self.db.delete_artifact(self.sid, BACKGROUND_ACCEPTED)
        await self.db.set_waiting_for(self.sid, None)
        await handlers.on_background_accept(self.callback(handlers.BACKGROUND_ACCEPT), self.db, self.pipeline, self.root)
        self.assertEqual(await self.db.get_waiting_for(self.sid), modes.WAIT_MODE)

    async def test_only_actual_render_artifacts_count_as_old_progress(self):
        # Preserve old in-progress jobs, without treating the dialogue marker
        # slide_images_done as a rendered slide.
        for kind in ('descriptions', 'slide_01', 'slide_prompt_02', 'slide_marked_03'):
            with self.subTest(kind=kind):
                await self.db.delete_artifact(self.sid, modes.MODE)
                await self.db.save_artifact(self.sid, 'slide_images_done', '')
                await self.db.save_artifact(self.sid, kind, 'saved')
                with patch.object(modes, 'run_slides', AsyncMock()) as old:
                    await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
                    old.assert_awaited_once()
                await self.db.delete_artifact(self.sid, kind)

    async def test_real_style_flow_offers_and_runs_editable_branch(self):
        from bot import handlers
        from bot.presets import PresetStore
        await handlers.finish_slide_images(self.message, self.db, PresetStore(self.root/'no-presets'), self.sid)
        await modes.continue_generation(self.message, self.db, self.pipeline, self.root, self.sid)
        self.assertIsNone(await self.db.get_artifact(self.sid, modes.MODE))
        keyboard = self.message.answer.await_args.kwargs['reply_markup']
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(callbacks, [f'genmode:{self.sid}:editable', f'genmode:{self.sid}:standard'])
        with patch.object(modes, 'run_editable', AsyncMock()) as new, patch.object(modes, 'run_slides', AsyncMock()) as old:
            await modes.on_generation_mode(self.callback(callbacks[0]), self.db, self.pipeline, self.root)
            new.assert_awaited_once()
            old.assert_not_awaited()
        self.assertEqual(await self.db.get_artifact(self.sid, modes.MODE), 'editable')

    async def test_delivery_retry_does_not_resend_completed_document(self):
        html, pptx = self.root/'test.html', self.root/'test.pptx'
        html.write_text('test'); pptx.write_bytes(b'test')
        self.pipeline.editable_service.generate.return_value = (html, pptx)
        self.message.answer_document.side_effect = [None, RuntimeError('network'), None]
        with self.assertLogs(modes.log, level='ERROR'):
            await modes.run_editable(self.message, self.db, self.pipeline, self.root, self.sid)
        self.assertTrue(await self.db.is_active(self.sid))
        await modes.run_editable(self.message, self.db, self.pipeline, self.root, self.sid)
        self.assertEqual([c.args[0].filename for c in self.message.answer_document.await_args_list],
                         ['Презентация_редактируемая.pptx', 'Презентация.html', 'Презентация.html'])
        self.assertFalse(await self.db.is_active(self.sid))

    async def test_review_finding_sends_the_render_with_the_repair(self):
        bg = self.root/'bg.png'; PILImage.new('RGB', (160, 90), 'white').save(bg)
        await self.db.save_artifact(self.sid, BACKGROUND, str(bg))
        await self.db.save_artifact(self.sid, STYLE, 'light')
        await self.db.save_artifact(self.sid, PLAN.kind, 'СЛАЙД 1\nЗАГОЛОВОК: Портфолио\nСОДЕРЖАНИЕ: Проверка 12,5%\nСЛАЙД 2\nЗАГОЛОВОК: Процесс\nСОДЕРЖАНИЕ: Вход → Выход')
        await self.db.save_artifact(self.sid, user_image_kind(2), UserImage(str(bg),160,90).to_json())
        plan, _ = fixtures()
        plan.slides[0].illustration = 'Портфолио'
        slide1 = SlideMarkup(css='h1{font-size:48px}', html='<section class="slide" data-slide="1" data-visual-type="cover">'
                             '<h1>Проверка</h1><img data-asset-id="slide-1-image" style="width:160px;height:160px"></section>',
                             image_prompt='Conceptual illustration', image_alt='Иллюстрация')
        slide2 = SlideMarkup(css='.node{border:2px solid blue}', html='<section class="slide" data-slide="2" data-visual-type="process">'
                             '<h1>Процесс</h1><div data-infographic="process"><div data-node="a" class="node">Вход</div>'
                             '<div data-node="b" class="node">Выход</div><div data-connector="a-b"></div></div>'
                             '<img data-user-slide="2" style="width:300px;height:200px;object-fit:contain"></section>')
        answers = [(plan.model_dump_json(), 'fake'), (slide1.model_dump_json(), 'fake'), (slide2.model_dump_json(), 'fake'),
                   (slide2.model_dump_json(), 'fake')]
        llm = SimpleNamespace(generate=AsyncMock(side_effect=answers))
        reviews = {1: '{"problems": []}', 2: '{"problems": ["Подпись наезжает на узел справа"]}'}
        async def review(parts, *_, **__):
            return reviews[int(parts[0].rsplit('"title": "', 1)[1].startswith('Процесс')) + 1], 'flash'
        reviewer = SimpleNamespace(generate=AsyncMock(side_effect=review))
        images = SimpleNamespace(generate=AsyncMock(return_value=(SimpleNamespace(data=bg.read_bytes(),mime_type='image/png'),'fake')))
        service = EditableService(llm, images, prompt_set=PromptSet('test plan', 'test html', 'test image', 'test review'),
                                  parallel=1, reviewer=reviewer)
        async def render_ok(source, output, *_, **__):
            output.write_bytes(b'pptx')
            for n in (1, 2):
                (source.parent/f'preview-slide-{n}.png').write_bytes(f'render {n} {llm.generate.await_count}'.encode())
            return {'issues': {}, 'textBounds': []}
        with patch('bot.editable.service.chrome_path', return_value='/fake'), patch('bot.editable.service.render', AsyncMock(side_effect=render_ok)):
            await service.generate(self.db,self.sid,self.root,AsyncMock())
        self.assertEqual(llm.generate.await_count, 4)          # plan, two slides, one repair
        repair = llm.generate.await_args_list[3].args[0]
        self.assertIn('Подпись наезжает на узел справа', repair[0])
        self.assertIn('Image 2: current render of this slide.', repair)
        self.assertEqual(reviewer.generate.await_count, 3)     # both slides, then the repaired one

    async def test_plan_and_layout_use_their_own_models_and_thinking(self):
        bg = self.root/'bg.png'; PILImage.new('RGB', (160, 90), 'white').save(bg)
        await self.db.save_artifact(self.sid, BACKGROUND, str(bg))
        await self.db.save_artifact(self.sid, STYLE, 'light')
        await self.db.save_artifact(self.sid, PLAN.kind, 'СЛАЙД 1\nЗАГОЛОВОК: Портфолио\nСОДЕРЖАНИЕ: Проверка 12,5%\nСЛАЙД 2\nЗАГОЛОВОК: Процесс\nСОДЕРЖАНИЕ: Вход → Выход')
        await self.db.save_artifact(self.sid, user_image_kind(2), UserImage(str(bg),160,90).to_json())
        plan, _ = fixtures()
        slides = {'1': SlideMarkup(css='h1{font-size:48px}', html='<section class="slide" data-slide="1" data-visual-type="cover">'
                                   '<h1>Проверка</h1></section>'),
                  '2': SlideMarkup(css='.node{border:2px solid blue}', html='<section class="slide" data-slide="2" data-visual-type="process">'
                                   '<h1>Процесс</h1><div data-infographic="process"><div data-node="a" class="node">Вход</div>'
                                   '<div data-node="b" class="node">Выход</div><div data-connector="a-b"></div></div>'
                                   '<img data-user-slide="2" style="width:300px;height:200px;object-fit:contain"></section>')}
        planner = SimpleNamespace(generate=AsyncMock(return_value=(plan.model_dump_json(), 'pro')))
        async def layout(parts, *_, **__):
            number = parts[0].split('TARGET_SLIDE\n{"data-slide": "', 1)[1][0]
            return slides[number].model_dump_json(), 'flash'
        llm = SimpleNamespace(generate=AsyncMock(side_effect=layout))
        images = SimpleNamespace(generate=AsyncMock())
        service = EditableService(llm, images, prompt_set=PromptSet('test plan', 'test html', 'test image', ''),
                                  thinking='medium', parallel=2, planner=planner, plan_thinking='high')
        async def render_ok(source, output, *_, **__):
            output.write_bytes(b'pptx')
            return {'issues': {}, 'textBounds': []}
        with patch('bot.editable.service.chrome_path', return_value='/fake'), patch('bot.editable.service.render', AsyncMock(side_effect=render_ok)):
            await service.generate(self.db, self.sid, self.root, AsyncMock())
        self.assertEqual(planner.generate.await_count, 1)
        self.assertEqual(planner.generate.await_args.args[1].thinking_config.thinking_level.name, 'HIGH')
        self.assertEqual(llm.generate.await_count, 2)          # two slides, no repairs
        for call in llm.generate.await_args_list:
            self.assertEqual(call.args[1].thinking_config.thinking_level.name, 'MEDIUM')

    async def test_cached_generation_retries_export_without_model_calls(self):
        bg = self.root/'bg.png'; PILImage.new('RGB', (160, 90), 'white').save(bg)
        await self.db.save_artifact(self.sid, BACKGROUND, str(bg))
        await self.db.save_artifact(self.sid, STYLE, 'light')
        await self.db.save_artifact(self.sid, PLAN.kind, 'СЛАЙД 1\nЗАГОЛОВОК: Портфолио\nСОДЕРЖАНИЕ: Проверка 12,5%\nСЛАЙД 2\nЗАГОЛОВОК: Процесс\nСОДЕРЖАНИЕ: Вход → Выход')
        await self.db.save_artifact(self.sid, user_image_kind(2), UserImage(str(bg),160,90).to_json())
        plan, _ = fixtures()
        plan.slides[0].illustration = 'Портфолио'
        slides = [SlideMarkup(css='h1{font-size:48px}', html='<section class="slide" data-slide="1" data-visual-type="cover">'
                              '<h1>Проверка</h1><img data-asset-id="slide-1-image" style="width:160px;height:160px"></section>',
                              image_prompt='Conceptual illustration', image_alt='Иллюстрация'),
                  SlideMarkup(css='.node{border:2px solid blue}', html='<section class="slide" data-slide="2" data-visual-type="process">'
                              '<h1>Процесс</h1><div data-infographic="process"><div data-node="a" class="node">Вход</div>'
                              '<div data-node="b" class="node">Выход</div><div data-connector="a-b"></div></div>'
                              '<img data-user-slide="2" style="width:300px;height:200px;object-fit:contain"></section>')]
        answers = [(plan.model_dump_json(), 'fake')] + [(s.model_dump_json(), 'fake') for s in slides]
        llm = SimpleNamespace(generate=AsyncMock(side_effect=answers))
        images = SimpleNamespace(generate=AsyncMock(return_value=(SimpleNamespace(data=bg.read_bytes(),mime_type='image/png'),'fake')))
        service = EditableService(llm, images, prompt_set=PromptSet('test plan', 'test html', 'test image'), parallel=1)
        async def render_ok(source, output, *_, **__):
            output.write_bytes(b'pptx')
            return {'issues': {}, 'textBounds': []}
        with patch('bot.editable.service.chrome_path', return_value='/fake'), patch('bot.editable.service.render', AsyncMock(side_effect=RuntimeError('browser down'))):
            with self.assertRaises(RuntimeError):
                await service.generate(self.db,self.sid,self.root,AsyncMock())
        with patch('bot.editable.service.chrome_path', return_value='/fake'), patch('bot.editable.service.render', AsyncMock(side_effect=render_ok)) as renderer:
            await service.generate(self.db,self.sid,self.root,AsyncMock())
            await service.generate(self.db,self.sid,self.root,AsyncMock())
            renderer.assert_awaited_once()
        self.assertEqual(llm.generate.await_count,3)
        images.generate.assert_awaited_once()


class MarkupTests(unittest.TestCase):
    def test_private_prompts_are_required_only_for_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'editable_visual_plan.md'):
                load_private_prompts(Path(directory))

    def test_private_prompt_loader_preserves_provided_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = ['test plan\n', 'test html\n', 'test image prefix: ']
            for filename, value in zip(['editable_visual_plan.md', 'editable_html.md', 'editable_image.md'], values):
                (root / filename).write_text(value)
            loaded = load_private_prompts(root)
            self.assertEqual([loaded.PLAN, loaded.HTML, loaded.IMAGE], values)

    def test_reject_scripts_remote_assets_missing_photos_and_infographics(self):
        plan, deck = fixtures()
        validate_deck(deck, plan, {2})
        for old, new in [('</body>', '<script>fetch("https://example.com")</script></body>'),
                         ('data-asset-id="cover"','data-asset-id="cover" src="file:///etc/passwd"'),
                         ('data-user-slide="2"','data-user-slide="3"'),
                         ('data-connector="a-b"','class="plain"')]:
            bad = deck.model_copy(update={'html': deck.html.replace(old,new)})
            with self.subTest(new=new), self.assertRaises(ValueError): validate_deck(bad,plan,{2})

    def test_self_contained_package_contains_original_background(self):
        plan, deck = fixtures()
        picture=(b'original-background-bytes','image/png')
        result=package(deck,plan,{'cover':picture},{2:picture},picture,1600,900,123)
        self.assertIn('b3JpZ2luYWwtYmFja2dyb3VuZC1ieXRlcw==',result)
        self.assertIn('presentation-123-editable.pptx',result)
        self.assertIn('Content-Security-Policy',result)


if __name__ == '__main__':
    unittest.main()
