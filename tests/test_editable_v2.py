import base64
import io
import json
import struct
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fontTools.ttLib import TTFont
from lxml import etree

from bot.editable.fonts import FONT_DIR, P, R, font_bundle
from bot.editable.html import template
from bot.editable.models import SlidePlan
from bot.presets import PresetStore


class StylesAndFormsTests(unittest.TestCase):
    def test_html_passport_does_not_change_raster_preset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / '1'; root.mkdir()
            for name in ('preview.jpg', 'background.png'):
                (root / name).write_bytes(b'fixture')
            (root / 'style.md').write_text('raster style')
            (root / 'style_html.md').write_text('html style')
            (root / 'fonts.json').write_text('{"body":"Inter"}')
            store = PresetStore(root.parent)
            self.assertEqual(store.get(1).style, 'raster style')
            self.assertEqual(store.html_options(1), {'style_description': 'html style', 'font_policy': {'body': 'Inter'}})

    def test_old_plan_remains_valid_new_form_is_checked(self):
        data = dict(number=1, title='Доли', message='Структура', visual_type='bar_chart',
                    composition='Кольцо', nodes=[], edges=[], metrics=[], evidence=[])
        self.assertIsNone(SlidePlan(**data).form)
        data['composition'] = 'ФОРМА: donut. Доли целого.'
        self.assertEqual(SlidePlan(**data).form, 'donut')
        with self.assertRaises(ValueError):
            SlidePlan(**{**data, 'visual_type': 'process'})
        with self.assertRaises(ValueError):
            SlidePlan(**data, form='bar')


class EmbeddedFontTests(unittest.TestCase):
    def test_complete_cyrillic_fonts_and_relationships_are_embedded(self):
        bundle = font_bundle({'heading': 'Montserrat', 'body': 'Roboto', 'numbers': 'Exo 2'})
        files = {k: base64.b64decode(v) for k, v in template(1, 1600, 900, bundle['faces']).items()}
        p = etree.fromstring(files['ppt/presentation.xml'])
        self.assertEqual(p.get('embedTrueTypeFonts'), '1')
        self.assertEqual(p.get('saveSubsetFonts'), '0')
        rels = etree.fromstring(files['ppt/_rels/presentation.xml.rels'])
        targets = {r.get('Id'): r.get('Target') for r in rels}
        fonts = p.findall(f'{{{P}}}embeddedFontLst/{{{P}}}embeddedFont')
        self.assertEqual(len(fonts), 4)
        for entry in fonts:
            family = entry.find(f'{{{P}}}font').get('typeface')
            for slot, weight in [('regular', 400), ('bold', 700)]:
                rid = entry.find(f'{{{P}}}{slot}').get(f'{{{R}}}id')
                eot = files['ppt/' + targets[rid]]
                total, data_size, version, flags = struct.unpack_from('<4I', eot)
                # Windows/PowerPoint reject EOT 0x00010000; 0x00020002 is what Windows itself writes.
                self.assertEqual((total, version, flags), (len(eot), 0x20002, 0))
                self.assertEqual(struct.unpack_from('<H', eot, 34)[0], 0x504C)
                font = TTFont(io.BytesIO(eot[-data_size:]))
                self.assertNotIn('fvar', font)
                self.assertEqual(font['name'].getDebugName(1), family)
                self.assertEqual(font['OS/2'].usWeightClass, weight)
                self.assertEqual(font['OS/2'].fsType, 0)
                self.assertTrue(all(cp in font.getBestCmap() for cp in [*range(0x410, 0x450), 0x401, 0x451]))

    def test_browser_faces_match_embedded_fonts(self):
        manifest = json.loads((FONT_DIR / 'manifest.json').read_text())
        for family in ('Montserrat', 'Roboto', 'Exo 2', 'Open Sans', 'Inter'):
            for slot in ('regular', 'bold'):
                entry = manifest[family][slot]
                webfont = TTFont(FONT_DIR / entry['woff2'])
                desktop = TTFont(FONT_DIR / entry['ttf'])
                self.assertEqual(webfont.getBestCmap(), desktop.getBestCmap())
                self.assertEqual(webfont['hmtx'].metrics, desktop['hmtx'].metrics)


class ShapeExportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the JS geometry regression')
    def test_circle_has_no_rounded_rectangle_adjustment(self):
        exporter = Path(__file__).resolve().parents[1] / 'bot/editable/export-pptx.js'
        script = '''require(process.argv[1]);
          const base={x:0,y:0,w:44,h:44,radius:22};
          console.log(JSON.stringify(['ellipse','roundRect'].map(geometry=>PowerGuidePptx.shapeXML({...base,geometry},2))));'''
        shapes = json.loads(subprocess.check_output(['node', '-e', script, str(exporter)], text=True))
        ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ellipse, rounded = [etree.fromstring(f'<root {ns}>{s}</root>') for s in shapes]
        self.assertFalse(ellipse.xpath('//*[local-name()="avLst"]/*'))
        self.assertEqual(len(rounded.xpath('//*[local-name()="avLst"]/*')), 1)


if __name__ == '__main__':
    unittest.main()
