"""Diagram links are drawn by the code between measured data-node boxes."""
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from bot.editable.browser import render, chrome_path
from bot.editable.html import template


class CodeLinksTests(unittest.IsolatedAsyncioTestCase):
    async def test_links_join_nodes_and_missing_nodes_are_reported(self):
        try:chrome_path()
        except RuntimeError:self.skipTest('Chrome required')
        exporter = Path(__file__).resolve().parents[1]/'bot/editable/export-pptx.js'
        links = json.dumps([['a', 'b', 'ведёт'], ['b', 'c', ''], ['c', 'ghost', '']], ensure_ascii=False)
        source = f'''<!DOCTYPE html><html><head><style>body{{margin:0}}
            .slide{{width:1600px;height:900px;position:relative;background:#fff;font-family:Arial}}
            h1{{margin:0;padding:40px 80px;font-size:48px}}
            .g{{display:flex;gap:220px;padding:120px 80px;--link-color:#0a7;--link-width:4px}}
            .n{{width:240px;padding:24px;border:2px solid #0a7;border-radius:16px;font-size:24px}}
            .pg-link-label{{position:absolute;white-space:nowrap;font-size:20px;background:#fff}}</style></head><body>
            <div id="presentation-viewport"><section class="slide" data-slide="1" data-visual-type="process" data-form="steps">
            <h1>Схема</h1><div class="g" data-infographic="process" data-links='{links}'>
            <div class="n" data-node="a">Первый</div><div class="n" data-node="b">Второй</div><div class="n" data-node="c">Третий</div>
            </div></section></div><script>POWERGUIDE_EXPORT_CONFIG={{width:1600,height:900,slideCount:1}};
            POWERGUIDE_PPTX_TEMPLATE={json.dumps(template(1,1600,900))};</script><script>{exporter.read_text()}</script></body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p/'test.html').write_text(source, encoding='utf-8')
            result = await render(p/'test.html', p/'test.pptx', 1, 1600, 900)
            with ZipFile(p/'test.pptx') as z:
                xml = z.read('ppt/slides/slide1.xml').decode()
        self.assertIn('ведёт', xml)                       # the label became an editable text box
        self.assertGreaterEqual(xml.count('<a:custGeom>'), 4)  # two lines and two arrowheads
        self.assertEqual(result['missingNodes'][0]['items'], ['ghost'])
        self.assertEqual(result['collisions'][0]['dangling'], [])
        self.assertEqual(result['collisions'][0]['text'], [])
        self.assertTrue(any('ghost' in h for h in result['issues'][1]))


if __name__ == '__main__':
    unittest.main()
