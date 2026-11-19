"""Build regular/bold Google Font faces, WOFF2 and uncompressed EOT v1.

Sources and OFL licenses live in assets/fonts. No network requests are made.
EOT header: https://www.w3.org/submissions/EOT/#EOTHeader
"""
import io
import json
from pathlib import Path
import struct

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1] / 'assets' / 'fonts'


def eot(data):
    font = TTFont(io.BytesIO(data))
    os2 = font['OS/2']
    if os2.fsType & 0x202 or (os2.fsType & 4 and not os2.fsType & 8):
        raise ValueError('Font does not permit editable outline embedding')
    panose = bytes(getattr(os2.panose, n) for n in ('bFamilyType', 'bSerifStyle', 'bWeight',
        'bProportion', 'bContrast', 'bStrokeVariation', 'bArmStyle', 'bLetterForm', 'bMidline', 'bXHeight'))
    header = struct.pack('<4I10sBBIHH7I4I', 0, len(data), 0x10000, 0, panose, 1,
        bool(os2.fsSelection & 1), os2.usWeightClass, os2.fsType, 0x504C,
        *[getattr(os2, f'ulUnicodeRange{i}') for i in range(1, 5)],
        getattr(os2, 'ulCodePageRange1', 0), getattr(os2, 'ulCodePageRange2', 0),
        font['head'].checkSumAdjustment, 0, 0, 0, 0)
    for number in (1, 2, 5, 4):
        value = (font['name'].getDebugName(number) or '').encode('utf-16-le')
        header += struct.pack('<HH', 0, len(value)) + value
    return struct.pack('<I', len(header) + len(data)) + header[4:] + data


def ranges(points):
    result = []
    for p in sorted(points):
        if result and result[-1][1] + 1 == p:
            result[-1][1] = p
        else:
            result.append([p, p])
    return result


def main():
    manifest = {}
    out = ROOT / 'built'
    out.mkdir(exist_ok=True)
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name != 'built'):
        sources = list(folder.glob('*.ttf'))
        if not sources:
            continue
        for weight, slot in ((400, 'regular'), (700, 'bold')):
            source = next((p for p in sources if '[' in p.name), None)
            source = source or next(p for p in sources if p.stem.endswith('-' + slot.title()))
            font = TTFont(source, recalcTimestamp=False)
            family = font['name'].getDebugName(16) or font['name'].getDebugName(1)
            if 'fvar' in font:
                axes = {a.axisTag: (weight if a.axisTag == 'wght' else a.defaultValue) for a in font['fvar'].axes}
                font = instantiateVariableFont(font, axes, inplace=True, updateFontNames=True)
            # Legacy R/B style linking must use the same family name for both faces.
            for number, value in ((1, family), (2, slot.title()), (16, family), (17, slot.title()),
                                  (4, family + ' ' + slot.title()), (6, family.replace(' ', '') + '-' + slot.title())):
                for platform, encoding, language in ((3, 1, 0x409), (1, 0, 0)):
                    font['name'].setName(value, number, platform, encoding, language)
            font['OS/2'].usWeightClass = weight
            font['OS/2'].fsSelection = (font['OS/2'].fsSelection & ~0x61) | (0x20 if weight == 700 else 0x40)
            font['head'].macStyle = (font['head'].macStyle & ~3) | (1 if weight == 700 else 0)
            buffer = io.BytesIO(); font.save(buffer); data = buffer.getvalue()
            name = folder.name + '-' + slot
            (out / (name + '.ttf')).write_bytes(data)
            (out / (name + '.eot')).write_bytes(eot(data))
            font.flavor = 'woff2'; font.save(out / (name + '.woff2'))
            manifest.setdefault(family, {'coverage': ranges(font.getBestCmap())})[slot] = {
                k: 'built/' + name + '.' + k for k in ('ttf', 'eot', 'woff2')}
        print(family, 'built', flush=True)
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
