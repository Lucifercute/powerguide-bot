"""Native rounded image and caption clipping, independent of generated content."""
import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree
from PIL import Image

from bot.editable.html import template
from bot.editable.browser import render, chrome_path

EXPORTER = Path(__file__).resolve().parents[1] / 'bot/editable/export-pptx.js'
NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}


@unittest.skipUnless(shutil.which('node'), 'Node is required')
class GeometryTests(unittest.TestCase):
    def js(self, expression):
        return json.loads(subprocess.check_output(['node', '-e',
            'require(process.argv[1]);const P=PowerGuidePptx;console.log(JSON.stringify(' + expression + '));', str(EXPORTER)], text=True))

    def test_picture_and_shape_share_explicit_radius(self):
        xmls = self.js('(()=>{const o={x:0,y:0,w:600,h:500,radius:24,geometry:"roundRect"};return [P.pictureXML(o,2,"rId2"),P.shapeXML(o,3)];})()')
        for xml in xmls:
            root = etree.fromstring(f'<root xmlns:a="{NS["a"]}" xmlns:p="{NS["p"]}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">{xml}</root>')
            self.assertEqual(root.xpath('//a:gd[@name="adj"]/@fmla', namespaces=NS), ['val 4800'])

    def test_caption_only_has_bottom_corners_clipped(self):
        o = self.js('P.clipObjects([{type:"shape",x:0,y:420,w:600,h:80,geometry:"rect"}], [P.roundedPoints(600,500,Array(4).fill([24,24]))])[0]')
        points = o['paths'][0]['points']
        top = [x for x,y in points if abs(y) < .01]
        bottom = [x for x,y in points if abs(y-80) < .01]
        self.assertAlmostEqual(min(top), 0, places=4)
        self.assertAlmostEqual(max(top), 600, places=4)
        self.assertAlmostEqual(min(bottom), 24, places=4)
        self.assertAlmostEqual(max(bottom), 576, places=4)

    def test_contained_picture_is_not_rounded_artificially(self):
        o = self.js('P.clipObjects([{type:"image",x:0,y:50,w:200,h:100}], [P.roundedPoints(200,200,Array(4).fill([40,40]))])[0]')
        self.assertNotIn('clip', o)

    def test_nested_clips_intersect_and_outside_shapes_disappear(self):
        rows = self.js('P.clipObjects([{type:"image",x:0,y:0,w:600,h:500},{type:"shape",x:700,y:0,w:50,h:50}], [P.roundedPoints(600,500,Array(4).fill([24,24])), [[30,0],[600,0],[600,500],[30,500]]])')
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(min(x for x,y in rows[0]['clip']), 30-1e-6)

    def test_clipping_keeps_text_editable_and_trimmed_border_open(self):
        rows = self.js('P.clipObjects([{type:"text",text:"Caption",x:50,y:450,w:300,h:25},{type:"shape",x:0,y:420,w:600,h:80,paths:[{close:false,points:[[-40,0],[640,0]]}]}], [[[0,0],[600,0],[600,500],[0,500]]])')
        self.assertEqual(rows[0]['type'], 'text')
        self.assertFalse(rows[1]['paths'][0]['close'])
        self.assertAlmostEqual(rows[1]['paths'][0]['points'][0][0], 0)
        self.assertAlmostEqual(rows[1]['paths'][0]['points'][-1][0], 600)


class BrowserClippingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_css_collects_image_own_and_ancestor_clipping(self):
        try:
            chrome_path()
        except RuntimeError:
            self.skipTest('Chrome is required for CSS integration')
        image = io.BytesIO(); Image.new('RGB', (200,100), '#dd5544').save(image, format='PNG')
        uri = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        cases = [
            ('border-radius:24px;overflow:hidden', 'width:100%;height:100%;object-fit:cover'),
            ('border-radius:24px;overflow:visible', 'width:100%;height:100%;object-fit:cover'),
            ('', 'width:100%;height:100%;border-radius:24px;object-fit:cover'),
            ('width:200px;height:200px;border-radius:40px;overflow:hidden', 'width:100%;height:100%;object-fit:contain'),
            ('border:4px solid black;border-radius:24px;overflow:hidden', 'width:100%;height:100%;object-fit:cover'),
        ]
        slides = ''.join(f'''<section class="slide"><h1>Case {i}</h1>
            <div class="card" style="{parent}"><img alt="fixture" src="{uri}" style="{child}">
            <div class="caption">Editable caption</div></div></section>'''
            for i,(parent,child) in enumerate(cases))
        config = dict(width=1600,height=900,slideCount=len(cases))
        source = f'''<!DOCTYPE html><html><head><style>
        *{{box-sizing:border-box}}body{{margin:0}}.slide{{width:1600px;height:900px;position:relative}}
        .card{{position:absolute;left:100px;top:120px;width:600px;height:500px;background:white}}
        .caption{{position:absolute;bottom:0;left:0;width:100%;height:80px;background:white;padding:20px;font:20px Arial}}
        </style></head><body><div id="presentation-viewport">{slides}</div><script>
        POWERGUIDE_EXPORT_CONFIG={json.dumps(config)};POWERGUIDE_PPTX_TEMPLATE={json.dumps(template(len(cases),1600,900))};
        </script><script>{EXPORTER.read_text()}</script></body></html>'''
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp);(p/'test.html').write_text(source)
            await render(p/'test.html',p/'test.pptx',len(cases),1600,900)
            with ZipFile(p/'test.pptx') as z:
                roots = [etree.fromstring(z.read(f'ppt/slides/slide{i}.xml')) for i in range(1,len(cases)+1)]
            # Hidden overflow and the image's own radius clip; a visible parent
            # and a fully contained image must stay rectangular.
            for i, expected in enumerate([True,False,True,False,True]):
                self.assertEqual(bool(roots[i].xpath('//p:pic/p:spPr/a:custGeom',namespaces=NS)), expected, i)
            root = roots[0]
            caption = root.xpath('//p:sp[p:nvSpPr/p:cNvPr[@name="Editable caption"]]',namespaces=NS)[0]
            self.assertTrue(caption.xpath('.//a:custGeom',namespaces=NS))
            self.assertIn('Editable caption', ''.join(root.xpath('//a:t/text()',namespaces=NS)))


if __name__ == '__main__':
    unittest.main()
