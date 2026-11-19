"""Rendered checks: one text box per paragraph, minimum font size, empty lower part."""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from bot.editable.browser import render, chrome_path
from bot.editable.html import template
from bot.editable.layout import layout_issues


class LayoutRuleTests(unittest.TestCase):
    def test_small_and_top_heavy_content_are_reported(self):
        region = {'y': 100, 'h': 700}
        rows = [{'slide': 1, 'visualType': 'cover', 'region': region, 'content': {'y': 100, 'h': 50}},
                {'slide': 2, 'visualType': 'process', 'region': region, 'content': {'y': 120, 'h': 200}},
                {'slide': 3, 'visualType': 'text', 'region': region, 'content': {'y': 100, 'h': 420}},
                {'slide': 4, 'visualType': 'table', 'region': region, 'content': {'y': 130, 'h': 600}}]
        found = {i['slide']: i['reason'] for i in layout_issues(rows)}
        self.assertEqual(found, {2: 'undersized_content', 3: 'top_heavy'})


class BrowserLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_paragraph_is_one_box_and_small_text_is_a_slide_issue(self):
        try:chrome_path()
        except RuntimeError:self.skipTest('Chrome required')
        exporter = Path(__file__).resolve().parents[1]/'bot/editable/export-pptx.js'
        words = ' '.join(['длинный абзац текста'] * 12)
        source = f'''<!DOCTYPE html><html><head><style>body{{margin:0}}
            .slide{{width:1600px;height:900px;position:relative;background:#fff;font-family:Arial}}
            h1{{position:absolute;left:80px;top:40px;margin:0;font-size:48px}}
            p{{position:absolute;left:80px;top:200px;width:500px;margin:0;font-size:24px;line-height:36px}}
            small{{position:absolute;left:80px;top:500px;font-size:12px}}
            .mix{{left:800px;top:200px;width:500px}}
            h2{{position:absolute;left:800px;top:600px;width:220px;margin:0;font-size:60px;overflow-wrap:anywhere}}
            h3{{position:absolute;left:80px;top:700px;width:400px;margin:0;font-size:44px;font-weight:700;word-break:break-all}}
            i{{position:absolute;left:100px;top:206px;font-size:24px}}</style></head><body><div id="presentation-viewport">
            <section class="slide" data-slide="1" data-visual-type="text"><h1>Заголовок</h1><p>{words}</p><small>мелко</small>
            <p class="mix"><b>Метка:</b> {words}</p><h2>ПРОЕКТИРОВАНИЕ</h2><h3>ПРОЕКТИРОВАНИЕ</h3><i>наложение</i></section>
            </div><script>POWERGUIDE_EXPORT_CONFIG={{width:1600,height:900,slideCount:1}};
            POWERGUIDE_PPTX_TEMPLATE={json.dumps(template(1,1600,900))};</script><script>{exporter.read_text()}</script></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p/'test.html').write_text(source, encoding='utf-8')
            result = await render(p/'test.html', p/'test.pptx', 1, 1600, 900)
            with ZipFile(p/'test.pptx') as z:
                xml = z.read('ppt/slides/slide1.xml').decode()
            # The wrapped paragraph keeps its lines as breaks inside a single text box.
            # title, paragraph, small; label, its continuation line, the rest of that paragraph;
            # two one-word titles; the overlapping <i>
            self.assertEqual(xml.count('txBox="1"'), 3 + 3 + 2 + 1)
            label_end = xml.index('Метка:')
            continuation = xml.index('<a:t xml:space="preserve"> длинный', label_end)
            self.assertGreater(continuation, label_end)
            self.assertGreaterEqual(xml.count('<a:br>'), 2)
            self.assertIn('<a:spcPts val="2160"/>', xml)
            self.assertTrue(any('Кегль ниже минимума' in h for h in result['issues'][1]))
            # Words are never split. A word that fits at a smaller font is fixed in place;
            # one that cannot fit above the floor goes back to the model.
            self.assertFalse(any('разорвано' in h for h in result['issues'][1]))
            fit = result['wordFit'][0]
            self.assertEqual([w[0] for w in fit['fitted']], ['ПРОЕКТИРОВАНИЕ'])
            self.assertLess(fit['fitted'][0][2], 44)
            self.assertEqual([w[0] for w in fit['failed']], ['ПРОЕКТИРОВАНИЕ'])
            self.assertTrue(any('не помещается' in h for h in result['issues'][1]))
            # <i> sits on top of the first line of the long paragraph.
            self.assertIn('наложение', [word for pair in result['collisions'][0]['text'] for word in pair])
            self.assertTrue(any('наезжают' in h for h in result['issues'][1]))


if __name__ == '__main__':
    unittest.main()
