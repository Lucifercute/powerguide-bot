import json
from pathlib import Path
import tempfile
import unittest

from bot.editable.composition import composition_issues
from bot.editable.browser import render, chrome_path
from bot.editable.html import template


def row(n, *, panels=0, area=0, kind='text', form='text_paragraph'):
    return dict(slide=n, visualType=kind, form=form, panelCount=panels, panelAreaRatio=area)


class CompositionTests(unittest.TestCase):
    def test_different_names_do_not_hide_similar_panels(self):
        rows = [row(1,kind='cover'),row(2,panels=3,area=.3,form='cards'),
                row(3,panels=1,area=.23,form='text_image'),row(4,kind='process'),
                row(5,panels=4,area=.44,form='kpi'),row(6,kind='closing')]
        self.assertEqual(composition_issues(rows)[0]['slides'], [2,3,5])

    def test_open_kpi_image_and_connected_process_pass(self):
        rows = [row(1,panels=3,area=.3,form='cards'),row(2,form='text_image'),
                row(3,kind='process'),row(4,form='kpi')]
        self.assertEqual(composition_issues(rows), [])

    def test_a_few_panels_and_small_decks_are_not_forced_to_change(self):
        self.assertEqual(composition_issues([row(i,panels=3,area=.4) for i in range(3)]), [])
        self.assertEqual(composition_issues([row(i,panels=2,area=.08) for i in range(6)]), [])
        self.assertEqual(composition_issues([row(i,panels=4 if i<2 else 0,area=.3 if i<2 else 0) for i in range(4)]), [])

    def test_missing_metrics_and_service_slides_are_excluded(self):
        self.assertEqual(composition_issues([None,row(1,kind=None),row(2,panels=6,area=.5,kind='cover'),row(3,panels=6,area=.5,kind='closing')]), [])


class BrowserCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_computed_css_detects_repeated_panels_despite_different_forms(self):
        try:chrome_path()
        except RuntimeError:self.skipTest('Chrome required')
        exporter=Path(__file__).resolve().parents[1]/'bot/editable/export-pptx.js'
        sections=''.join(f'''<section class="slide" data-slide="{i}" data-visual-type="text" data-form="{form}">
            <h1>Example {i}</h1><div class="row" data-infographic="text">
            {''.join('<div class="panel">A sufficiently long text block to detect the actual painted card.</div>' for _ in range(3))}
            </div></section>''' for i,form in enumerate(['cards','kpi','text_image','text_list'],1))
        source=f'''<!DOCTYPE html><html><head><style>
            body{{margin:0}}.slide{{width:1600px;height:900px;position:relative;background:#eee}}
            h1{{margin:40px}}.row{{display:flex;gap:30px;margin:100px}}
            .panel{{width:360px;height:260px;background:#fff;border-radius:16px;padding:24px;font:24px Arial}}
            </style></head><body><div id="presentation-viewport">{sections}</div><script>
            POWERGUIDE_EXPORT_CONFIG={{width:1600,height:900,slideCount:4}};
            POWERGUIDE_PPTX_TEMPLATE={json.dumps(template(4,1600,900))};
            </script><script>{exporter.read_text()}</script></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory);(p/'test.html').write_text(source)
            with self.assertRaisesRegex(ValueError,'Однообразие композиции'):
                await render(p/'test.html',p/'test.pptx',4,1600,900)
            report=json.loads((p/'export-check.json').read_text())
            self.assertEqual([r['panelCount'] for r in report['composition']],[3]*4)
            self.assertEqual(report['compositionIssues'][0]['slides'],[1,2,3,4])
            self.assertFalse((p/'test.pptx').exists())


if __name__=='__main__':unittest.main()
