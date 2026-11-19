"""Regression: uploaded photo + form codes must survive HTML validation/retry."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lxml import html

from bot.editable.html import deck_contract, normalize_deck, validate_deck
from bot.editable.models import Deck, VisualPlan
from bot.editable.service import EditableService


def fixture(*, photo=True, kind='text_image'):
    plan = VisualPlan.model_validate({'title': 'Test', 'slides': [
        dict(number=1, title='Cover', message='', visual_type='cover', form='cover',
             composition='', nodes=[], edges=[], metrics=[], evidence=[]),
        dict(number=2, title='History', message='', visual_type='text', form='text_image',
             composition='', nodes=[], edges=[], metrics=[], evidence=[]),
    ]})
    image = '<img data-user-slide="1" style="width:200px;height:150px;object-fit:contain">' if photo else ''
    deck = Deck(title='Test', html=f'''<!DOCTYPE html><html><head><style>h1{{color:red}}</style></head>
        <body><div id="presentation-viewport">
        <section class="slide" data-slide="1" data-visual-type="cover"><h1>Cover</h1>{image}</section>
        <section class="slide" data-slide="2" data-visual-type="{kind}"><h1>History</h1><p>Saved content</p></section>
        </div></body></html>''')
    return plan, deck


class ContractTests(unittest.TestCase):
    def test_exact_form_alias_keeps_text_styles_and_photos(self):
        plan, deck = fixture()
        fixed = normalize_deck(deck, plan)
        doc = validate_deck(fixed, plan, {1})
        expected = html.document_fromstring(deck.html)
        expected.xpath('//section[@data-slide="2"]')[0].set('data-visual-type', 'text')
        self.assertEqual(html.tostring(doc), html.tostring(expected))
        self.assertIn('data-visual-type="text_image"', deck.html)
        self.assertIs(normalize_deck(fixed, plan), fixed)

    def test_different_form_and_missing_photo_are_reported_together(self):
        plan, deck = fixture(photo=False, kind='cards')
        with self.assertRaises(ValueError) as error:
            validate_deck(normalize_deck(deck, plan), plan, {1})
        self.assertIn('Слайд 1', str(error.exception))
        self.assertIn('data-user-slide="1"', str(error.exception))
        self.assertIn('Слайд 2', str(error.exception))
        self.assertIn('data-visual-type="text"', str(error.exception))

    def test_graph_alias_does_not_bypass_geometry_checks(self):
        plan, deck = fixture(photo=False, kind='steps')
        plan.slides[1] = plan.slides[1].model_copy(update={'visual_type': 'process', 'form': 'steps'})
        deck.html = deck.html.replace('<p>Saved content</p>', '<div data-infographic="steps"></div>')
        fixed = normalize_deck(deck, plan)
        with self.assertRaisesRegex(ValueError, 'data-node'):
            validate_deck(fixed, plan)
        fixed.html = fixed.html.replace('data-infographic="process">',
            'data-infographic="process"><div data-node="a">A</div><div data-node="b">B</div><hr data-connector="a-b">')
        validate_deck(fixed, plan)

    def test_alias_does_not_allow_scripts(self):
        plan, deck = fixture()
        deck.html = deck.html.replace('</body>', '<script>alert(1)</script></body>')
        with self.assertRaisesRegex(ValueError, 'исполняемый'):
            validate_deck(normalize_deck(deck, plan), plan, {1})

    def test_prompt_contract_distinguishes_type_form_and_photo(self):
        plan, _ = fixture()
        rows = json.loads(deck_contract(plan, {1}).split('\n')[-1])
        self.assertEqual(rows[0]['required_user_image'], '<img data-user-slide="1" ...>')
        self.assertEqual(rows[1]['data-visual-type'], 'text')
        self.assertEqual(rows[1]['data-form'], 'text_image')
        self.assertIsNone(rows[1]['required_user_image'])


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_response_recovers_without_new_model_request(self):
        plan, good = fixture()
        _, bad = fixture(photo=False)
        llm = SimpleNamespace(generate=AsyncMock(side_effect=AssertionError('Unexpected API call')))
        service = EditableService(llm, None)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for number, deck in enumerate([bad, good], 1):
                (folder / f'deck-attempt-{number}.response.json').write_text(
                    json.dumps({'model': 'fixture', 'text': deck.model_dump_json()}))
            raw = (folder / 'deck-attempt-2.response.json').read_bytes()
            result = await service._json_stage(folder, 'deck', 'Test', Deck,
                lambda d: validate_deck(d, plan, {1}), (b'background', 'image/png'),
                normalize=lambda d: normalize_deck(d, plan))
            validate_deck(result, plan, {1})
            self.assertEqual(raw, (folder / 'deck-attempt-2.response.json').read_bytes())
            cached = Deck.model_validate_json((folder / 'deck.json').read_text())
            validate_deck(cached, plan, {1})
            llm.generate.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
