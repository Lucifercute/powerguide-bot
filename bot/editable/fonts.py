"""Offline Google Fonts for HTML and editable PowerPoint.

Build static font faces with scripts/build_editable_fonts.py. Full fonts (not
text subsets) keep Cyrillic available when the recipient edits the document.
"""
import base64
import json
from pathlib import Path

from lxml import etree

FONT_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'fonts'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'


def font_bundle(policy=None):
    policy = {'body': 'Inter', 'heading': 'Inter', 'numbers': 'Inter', **(policy or {})}
    manifest_path = FONT_DIR / 'manifest.json'
    if not manifest_path.is_file():
        raise RuntimeError('Не подготовлены шрифты: выполните scripts/build_editable_fonts.py')
    manifest = json.loads(manifest_path.read_text())
    families = list(dict.fromkeys([*policy.values(), 'Inter']))
    unknown = set(families) - manifest.keys()
    if unknown:
        raise ValueError('Шрифт отсутствует в локальном наборе: ' + ', '.join(sorted(unknown)))
    faces, css, coverage = [], [], {}
    for family in families:
        entry = manifest[family]
        coverage[family] = entry['coverage']
        for weight, slot in ((400, 'regular'), (700, 'bold')):
            face = entry[slot]
            data = (FONT_DIR / face['woff2']).read_bytes()
            encoded = base64.b64encode(data).decode()
            css.append(f'@font-face{{font-family:"{family}";font-style:normal;font-weight:{weight};'
                       f'font-display:block;src:url(data:font/woff2;base64,{encoded}) format("woff2");}}')
            faces.append((family, slot, (FONT_DIR / face['eot']).read_bytes()))
    css.append(f'''#presentation-viewport{{font-family:"{policy['body']}",Inter,sans-serif;}}
      #presentation-viewport h1,#presentation-viewport h2,#presentation-viewport h3,
      #presentation-viewport [data-font-role="heading"]{{font-family:"{policy['heading']}",Inter,sans-serif;}}
      #presentation-viewport [data-font-role="numbers"]{{font-family:"{policy['numbers']}",Inter,sans-serif;}}''')
    return {'css': '\n'.join(css), 'faces': faces, 'coverage': coverage, 'policy': policy}


def embed_fonts(files, faces):
    """Add EOT font parts and presentation relationships to an OOXML byte mapping."""
    presentation = etree.fromstring(files['ppt/presentation.xml'])
    rels = etree.fromstring(files['ppt/_rels/presentation.xml.rels'])
    types = etree.fromstring(files['[Content_Types].xml'])
    presentation.set('embedTrueTypeFonts', '1')
    presentation.set('saveSubsetFonts', '0')
    font_list = etree.Element(f'{{{P}}}embeddedFontLst')
    # CT_Presentation order: embeddedFontLst precedes custShowLst/defaultTextStyle.
    following = {'custShowLst', 'photoAlbum', 'custDataLst', 'kinsoku', 'defaultTextStyle', 'modifyVerifier', 'extLst'}
    position = next((i for i, child in enumerate(presentation)
                     if etree.QName(child).localname in following), len(presentation))
    presentation.insert(position, font_list)
    entries = {}
    for i, (family, slot, data) in enumerate(faces, 1):
        rid, name = f'rIdEmbeddedFont{i}', f'font{i}.fntdata'
        files[f'ppt/fonts/{name}'] = data
        etree.SubElement(rels, f'{{{REL}}}Relationship', Id=rid, Type=f'{R}/font', Target=f'fonts/{name}')
        if family not in entries:
            entries[family] = etree.SubElement(font_list, f'{{{P}}}embeddedFont')
            etree.SubElement(entries[family], f'{{{P}}}font', typeface=family)
        etree.SubElement(entries[family], f'{{{P}}}{slot}', {f'{{{R}}}id': rid})
    etree.SubElement(types, f'{{{CT}}}Default', Extension='fntdata', ContentType='application/x-fontdata')
    for path, root in [('ppt/presentation.xml', presentation),
                       ('ppt/_rels/presentation.xml.rels', rels), ('[Content_Types].xml', types)]:
        files[path] = etree.tostring(root, encoding='UTF-8', xml_declaration=True, standalone=True)
