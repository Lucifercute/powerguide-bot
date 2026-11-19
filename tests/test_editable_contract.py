"""Regression: uploaded photo, form codes and icons must survive per-slide validation/retry."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.editable.html import assemble, normalize_markup, scope_css, slide_contract, validate_deck, validate_markup
from bot.editable.models import SlideMarkup, VisualPlan
from bot.editable.service import EditableService


def fixture(*, photo=True, kind='text_image', icon=''):
    plan = VisualPlan.model_validate({'title': 'Test', 'slides': [
        dict(number=1, title='Cover', message='', visual_type='cover', form='cover',
             composition='', nodes=[], edges=[], metrics=[], evidence=[]),
        dict(number=2, title='History', message='', visual_type='text', form='text_image',
             composition='', nodes=[{'id': 'item-1', 'label': 'A', 'detail': '', 'icon': icon}],
             edges=[], metrics=[], evidence=[]),
    ]})
    image = '<img data-user-slide="1" style="width:200px;height:150px;object-fit:contain">' if photo else ''
    svg = '<svg data-icon="item-1" viewBox="0 0 24 24"><path d="M4 4L20 20" stroke="red"/></svg>' if icon else ''
    cover = SlideMarkup(css='h1{color:red}', html=f'<section class="slide" data-slide="1" data-visual-type="cover"><h1>Cover</h1>{image}</section>')
    second = SlideMarkup(css='&{display:flex}', html=f'<section class="slide" data-slide="2" data-visual-type="{kind}"><h1>History</h1>{svg}<p>Saved content</p></section>')
    return plan, {1: cover, 2: second}


class ContractTests(unittest.TestCase):
    def test_exact_form_alias_is_canonicalized_and_photo_kept(self):
        plan, slides = fixture()
        fixed = normalize_markup(slides[2], plan.slides[1])
        self.assertIn('data-visual-type="text"', fixed.html)
        self.assertIn('Saved content', fixed.html)
        validate_markup(fixed, plan.slides[1])
        validate_markup(slides[1], plan.slides[0], user_slide=True)
        self.assertIs(normalize_markup(fixed, plan.slides[1]), fixed)

    def test_different_form_and_missing_photo_are_reported(self):
        plan, slides = fixture(photo=False, kind='cards')
        with self.assertRaisesRegex(ValueError, 'data-user-slide="1"'):
            validate_markup(slides[1], plan.slides[0], user_slide=True)
        with self.assertRaisesRegex(ValueError, 'data-visual-type="text"'):
            validate_markup(normalize_markup(slides[2], plan.slides[1]), plan.slides[1])

    def test_planned_icon_is_required(self):
        plan, slides = fixture(icon='clock')
        validate_markup(normalize_markup(slides[2], plan.slides[1]), plan.slides[1])
        bare = slides[2].model_copy(update={'html': slides[2].html.replace('data-icon="item-1"', 'data-icon="other"')})
        with self.assertRaisesRegex(ValueError, 'иконок'):
            validate_markup(normalize_markup(bare, plan.slides[1]), plan.slides[1])

    def test_scripts_and_foreign_slides_are_rejected(self):
        plan, slides = fixture()
        bad = slides[2].model_copy(update={'html': slides[2].html + '<script>alert(1)</script>'})
        with self.assertRaisesRegex(ValueError, 'исполняемый'):
            validate_markup(normalize_markup(bad, plan.slides[1]), plan.slides[1])
        wrong = slides[2].model_copy(update={'html': slides[2].html.replace('data-slide="2"', 'data-slide="3"')})
        with self.assertRaisesRegex(ValueError, 'section'):
            validate_markup(wrong, plan.slides[1])

    def test_assembled_deck_scopes_css_and_passes_deck_validation(self):
        plan, slides = fixture()
        slides = {n: normalize_markup(m, plan.slides[n - 1]) for n, m in slides.items()}
        deck = assemble(plan, slides, 'ТОКЕНЫ\n:root{--accent:#f00}')
        self.assertIn(':root{--accent:#f00}', deck.html)
        self.assertIn('section.slide[data-slide="2"]{\n&{display:flex}', deck.html)
        validate_deck(deck, plan, {1})

    def test_section_selectors_become_self_reference(self):
        css, roots = scope_css('section.slide{a:1} .slide .x{b:2} :root{--c:3}', 4)
        self.assertIn('&{a:1}', css)
        self.assertIn('& .x{b:2}', css)
        self.assertEqual(roots, [':root{--c:3}'])

    def test_prompt_contract_names_type_form_icons_and_photo(self):
        plan, _ = fixture(icon='clock')
        row = json.loads(slide_contract(plan.slides[1]).split('\n')[2])
        self.assertEqual(row['data-visual-type'], 'text')
        self.assertEqual(row['data-form'], 'text_image')
        self.assertEqual(row['icons'], {'item-1': 'clock'})
        cover = json.loads(slide_contract(plan.slides[0], True).split('\n')[2])
        self.assertEqual(cover['required_user_image'], '<img data-user-slide="1" ...>')


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_response_recovers_without_new_model_request(self):
        plan, good = fixture()
        _, bad = fixture(photo=False)
        llm = SimpleNamespace(generate=AsyncMock(side_effect=AssertionError('Unexpected API call')))
        service = EditableService(llm, None)
        spec = plan.slides[0]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for number, markup in enumerate([bad[1], good[1]], 1):
                (folder / f'slide-1-attempt-{number}.response.json').write_text(
                    json.dumps({'model': 'fixture', 'text': markup.model_dump_json()}))
            raw = (folder / 'slide-1-attempt-2.response.json').read_bytes()
            result = await service._json_stage(folder, 'slide-1', 'Test', SlideMarkup,
                lambda m: validate_markup(m, spec, True), (b'background', 'image/png'),
                normalize=lambda m: normalize_markup(m, spec))
            validate_markup(result, spec, True)
            self.assertEqual(raw, (folder / 'slide-1-attempt-2.response.json').read_bytes())
            cached = SlideMarkup.model_validate_json((folder / 'slide-1.json').read_text(encoding='utf-8'))
            validate_markup(cached, spec, True)
            llm.generate.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
