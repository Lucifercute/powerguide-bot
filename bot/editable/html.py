"""Validate generated markup and package offline HTML with the native exporter."""
import base64
import io
import json
import re
from html import escape
from pathlib import Path
from zipfile import ZipFile

from lxml import html
from pptx import Presentation
from pptx.util import Inches

from .models import Deck, VisualPlan, FORM_TYPES, Illustration, SlideMarkup
from .fonts import embed_fonts, font_bundle

GRAPHS = {'process', 'timeline', 'matrix', 'before_after', 'comparison',
          'bar_chart', 'stack_map', 'network', 'table'}


def normalize_markup(markup: SlideMarkup, spec) -> SlideMarkup:
    """A known form code used as the type is an alias: canonicalize it, keep everything else.
    A different design is still rejected by validation rather than relabelled as compliant."""
    if not spec.form or FORM_TYPES.get(spec.form) != spec.visual_type or spec.form == spec.visual_type:
        return markup
    doc = html.fragment_fromstring(markup.html, create_parent='div')
    changed = False
    for el in doc.xpath('.//section[@data-visual-type=$f]|.//*[@data-infographic=$f]', f=spec.form):
        el.set('data-visual-type' if el.tag == 'section' else 'data-infographic', spec.visual_type)
        changed = True
    if not changed:
        return markup
    inner = ''.join(html.tostring(child, encoding='unicode') for child in doc)
    return markup.model_copy(update={'html': inner})


def slide_contract(spec, user_slide=False) -> str:
    """Explicit invariants of the target slide, repeated in every request and repair."""
    row = {'data-slide': str(spec.number), 'data-visual-type': spec.visual_type, 'data-form': spec.form,
           'icons': {n.id: n.icon for n in spec.nodes if n.icon.strip()},
           'illustration': spec.illustration or None,
           'required_user_image': f'<img data-user-slide="{spec.number}" ...>' if user_slide else None,
           'illustration_img': f'<img data-asset-id="slide-{spec.number}-image" ...>' if spec.illustration.strip() else None}
    return ('\nTARGET_SLIDE\n' + json.dumps(row, ensure_ascii=False)
            + '\nСверстай только этот слайд. В data-visual-type и data-infographic — visual_type, НЕ код формы. '
            'Каждая иконка из icons — <svg data-icon="ID узла" viewBox="0 0 24 24">. '
            'Сохраняй все эти требования при исправлении любой ошибки.')


MAX_ILLUSTRATIONS = 3
ICON_SVG = './/*[local-name()="svg"][@data-icon=$id]'
HAND_LINES = ('.//*[@data-connector]|.//*[local-name()="line" or local-name()="path" or local-name()="polyline"]'
              '[not(ancestor::*[@data-icon])]')


def validate_plan(plan: VisualPlan, count: int) -> None:
    if [s.number for s in plan.slides] != list(range(1, count + 1)):
        raise ValueError('Неверное количество или порядок слайдов визуального плана')
    for s in plan.slides:
        ids = {n.id for n in s.nodes}
        if len(ids) != len(s.nodes):
            raise ValueError('Повторяющиеся id узлов')
        if s.visual_type in {'process', 'timeline', 'network'} and (len(ids) < 2 or not s.edges):
            raise ValueError('Схеме нужны узлы и связи')
        if any(e.source not in ids or e.target not in ids for e in s.edges):
            raise ValueError('Связь с неизвестным узлом')
        if s.visual_type == 'stack_map' and len(ids) < 2:
            raise ValueError('Нужны минимум два слоя')
        if s.visual_type in {'bar_chart', 'before_after', 'comparison'} and len(s.metrics) < 2:
            raise ValueError('Графику нужны исходные метрики')
        if any(not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', i) for i in ids):
            raise ValueError('id узла — латиница, цифры, дефис, начинается с буквы')
    if sum(bool(s.illustration.strip()) for s in plan.slides) > MAX_ILLUSTRATIONS:
        raise ValueError(f'Иллюстраций не больше {MAX_ILLUSTRATIONS} на презентацию')


def _check_safety(doc) -> None:
    """Self-contained markup only: the code inserts every resource itself."""
    if doc.xpath('//script|//iframe|//canvas|//object|//embed|//link|//base|//form|//meta[@http-equiv]'):
        raise ValueError('Посторонний исполняемый или внешний контент')
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag.lower() in {'foreignobject', 'use', 'image', 'filter'}:
            raise ValueError('Неподдерживаемый SVG-элемент')
        if any(k.lower().startswith('on') for k in el.attrib):
            raise ValueError('Обработчики событий не разрешены')
        if any(k.lower() in {'src', 'href', 'srcset', 'action', 'data'} or k.lower().endswith('}href') for k in el.attrib):
            raise ValueError('Ресурсы вставляет код, ссылки не разрешены')
    css = '\n'.join(doc.xpath('//style/text()') + doc.xpath('//@style'))
    if re.search(r'@import|url\s*\(', css, re.I):
        raise ValueError('Внешние изображения/шрифты в CSS не разрешены')


def slide_issues(section, spec, user_slide=False, asset_id=None) -> list[str]:
    """Contract of one slide: type, infographic markers, pictures and planned icons."""
    n, errors = spec.number, []
    if section.get('data-visual-type') != spec.visual_type:
        errors.append(f'Слайд {n}: data-visual-type="{section.get("data-visual-type")}"; '
                      f'требуется data-visual-type="{spec.visual_type}". '
                      f'Код формы {spec.form!r} — отдельное поле data-form.')
    if user_slide and len(section.xpath('.//img[@data-user-slide=$n]', n=str(n))) != 1:
        errors.append(f'Слайд {n}: нужен ровно один <img data-user-slide="{n}"> '
                      'для фотографии пользователя, без src, с размерами и object-fit:contain.')
    if asset_id and len(section.xpath('.//img[@data-asset-id=$id]', id=asset_id)) != 1:
        errors.append(f'Слайд {n}: нужен ровно один <img data-asset-id="{asset_id}"> без src под иллюстрацию.')
    expected = int(bool(user_slide)) + int(bool(asset_id))
    found = len(section.xpath('.//img'))
    if found != expected:
        errors.append(f'Слайд {n}: img на слайде — {found}, нужно {expected}: '
                      'только иллюстрация и фотография пользователя, если они предусмотрены.')
    missing = [node.id for node in spec.nodes if node.icon.strip() and not section.xpath(ICON_SVG, id=node.id)]
    if missing:
        errors.append(f'Слайд {n}: нет иконок для узлов {", ".join(missing)} — нужен '
                      '<svg data-icon="ID узла" viewBox="0 0 24 24"> с линейным рисунком.')
    if spec.visual_type not in GRAPHS:
        return errors
    graphs = section.xpath('.//*[@data-infographic=$kind]', kind=spec.visual_type)
    if len(graphs) != 1:
        return errors + [f'Слайд {n}: нужен один контейнер data-infographic="{spec.visual_type}".']
    g = graphs[0]
    if spec.visual_type in {'process', 'network', 'timeline', 'stack_map'}:
        if len(g.xpath('.//*[@data-node]')) < 2:
            errors.append(f'Слайд {n}: у схемы нужны минимум два элемента data-node.')
        if spec.visual_type != 'stack_map' and not g.xpath(HAND_LINES):
            errors.append(f'Слайд {n}: у схемы нужны соединения data-connector или SVG line/path/polyline.')
    elif spec.visual_type in {'bar_chart', 'comparison', 'before_after'}:
        if len(g.xpath('.//*[@data-bar]')) < 2:
            errors.append(f'Слайд {n} ({spec.visual_type}): внутри data-infographic нужны '
                          'минимум два элемента data-bar, обозначающие величины (столбцы, секторы, точки).')
    elif len(g.xpath('.//tr')) < 2 and len(g.xpath('.//*[@data-cell]')) < 4:
        errors.append(f'Слайд {n}: нужны строки таблицы или минимум четыре элемента data-cell.')
    return errors


def validate_deck(deck: Deck, plan: VisualPlan, user_slides=()):
    doc = html.document_fromstring(deck.html)
    if doc.find('head') is None or doc.find('body') is None:
        raise ValueError('Требуется полный HTML с head и body')
    sections = doc.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," slide ")]')
    if [s.get('data-slide') for s in sections] != [str(s.number) for s in plan.slides]:
        raise ValueError('Неверные номера HTML-слайдов')
    viewports = doc.xpath('//*[@id="presentation-viewport"]')
    if len(viewports) != 1 or any(viewports[0] not in s.iterancestors() for s in sections):
        raise ValueError('Слайды должны находиться в presentation-viewport')
    _check_safety(doc)
    assets, ids = {}, set()
    for image in deck.images:
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9-]{0,63}', image.id) or image.id in ids:
            raise ValueError('Неверный id иллюстрации')
        ids.add(image.id)
        if image.slide not in range(1, len(sections) + 1) or image.slide in assets:
            raise ValueError('Неверный слайд иллюстрации')
        assets[image.slide] = image.id
    errors = []
    for spec, section in zip(plan.slides, sections):
        errors += slide_issues(section, spec, spec.number in user_slides, assets.get(spec.number))
    if errors:
        raise ValueError('\n'.join(errors))
    return doc


def asset_id(number: int) -> str:
    return f'slide-{number}-image'


def validate_markup(markup: SlideMarkup, spec, user_slide=False) -> None:
    """One slide answer: a single section, safe markup, the slide contract."""
    fragment = html.document_fromstring(
        f'<html><head><style>{markup.css}</style></head><body>{markup.html}</body></html>')
    sections = fragment.xpath('//section')
    if len(sections) != 1 or 'slide' not in sections[0].get('class', '').split() \
            or sections[0].get('data-slide') != str(spec.number):
        raise ValueError(f'Нужен ровно один <section class="slide" data-slide="{spec.number}">')
    _check_safety(fragment)
    if re.search(r'@keyframes|@font-face|@media', markup.css, re.I):
        raise ValueError('В CSS слайда не нужны @keyframes, @font-face и @media')
    wants_image = bool(spec.illustration.strip())
    if wants_image != bool(markup.image_prompt.strip()):
        raise ValueError('Для иллюстрации из визуального плана нужен image_prompt' if wants_image else
                         'image_prompt заполняется только на слайде с иллюстрацией из визуального плана')
    errors = slide_issues(sections[0], spec, user_slide, asset_id(spec.number) if wants_image else None)
    if errors:
        raise ValueError('\n'.join(errors))


_ROOT = re.compile(r':root\s*\{[^{}]*\}')
_SELF = re.compile(r'(^|[{},;])(\s*)(?:section)?\.slide(?:\[data-slide=["\']?\d+["\']?\])?(?=[\s.:{,\[>+~#]|$)')


def scope_css(css: str, number: int) -> tuple[str, list[str]]:
    """Nest a slide's rules under its section; selectors of the section itself become &."""
    roots = _ROOT.findall(css)
    body = _SELF.sub(lambda m: m.group(1) + m.group(2) + '&', _ROOT.sub('', css))
    return f'#presentation-viewport section.slide[data-slide="{number}"]{{\n{body}\n}}', roots


def assemble(plan: VisualPlan, markups: dict, style_description: str = '') -> Deck:
    """Join independently laid-out slides into one deck with shared design tokens."""
    roots = _ROOT.findall(style_description or '')
    rules, sections, images = [], [], []
    for spec in plan.slides:
        markup = markups[spec.number]
        scoped, slide_roots = scope_css(markup.css, spec.number)
        roots = roots or slide_roots  # tokens from the passport win; else the first slide's
        rules.append(scoped)
        sections.append(markup.html)
        if markup.image_prompt.strip():
            images.append(Illustration(id=asset_id(spec.number), slide=spec.number,
                                       aspect_ratio=markup.image_aspect_ratio, prompt=markup.image_prompt,
                                       alt=markup.image_alt or spec.title))
    base = ('*,*::before,*::after{box-sizing:border-box}'
            # The slide frame belongs to the code: a slide rule may not move or resize the section itself
            # (an absolute, inset section shifts the HTML view and its render away from the PPTX).
            '#presentation-viewport section.slide{position:relative!important;inset:auto!important;'
            'margin:0!important;transform:none!important;overflow:hidden}')
    css = '\n'.join([*dict.fromkeys(roots), base, *rules])
    document = (f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>{escape(plan.title)}</title>'
                f'<style>{css}</style></head><body><div id="presentation-viewport">'
                + '\n'.join(sections) + '</div></body></html>')
    return Deck(title=plan.title, html=document, images=images)


def uri(data: bytes, mime: str) -> str:
    return f'data:{mime};base64,' + base64.b64encode(data).decode()


def template(count, width, height, faces=()):
    p = Presentation()
    p.slide_width, p.slide_height = Inches(width / 120), Inches(height / 120)
    for _ in range(count):
        p.slides.add_slide(p.slide_layouts[6])
    out = io.BytesIO()
    p.save(out)
    with ZipFile(out) as z:
        files = {n: z.read(n) for n in z.namelist()}
    if faces:
        embed_fonts(files, faces)
    return {n: base64.b64encode(data).decode() for n, data in files.items()}


def package(deck, plan, assets, user_images, background, width, height, session_id, font_policy=None):
    doc = validate_deck(deck, plan, user_images)
    fonts = font_bundle(font_policy)
    for spec in plan.slides:
        section = doc.xpath('//section[@data-slide=$number]', number=str(spec.number))[0]
        if spec.form:
            section.set('data-form', spec.form)
    for spec in deck.images:
        el = doc.xpath('//img[@data-asset-id=$id]', id=spec.id)[0]
        el.set('src', uri(*assets[spec.id]))
        el.set('alt', spec.alt)
    for number, value in user_images.items():
        doc.xpath('//img[@data-user-slide=$n]', n=str(number))[0].set('src', uri(*value))
    head, body = doc.find('head'), doc.find('body')
    policy = html.Element('meta', attrib={'http-equiv': 'Content-Security-Policy', 'content':
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'none'; font-src data:; frame-src 'none'; base-uri 'none'; form-action 'none'"})
    head.insert(0, policy)
    style = html.Element('style')
    style.text = f'''
    :root{{--exact-preset-background:url("{uri(*background)}");}}
    html,body{{margin:0;overflow:hidden;background:#222;}}
    #presentation-viewport{{width:{width}px!important;height:{height}px!important;background:transparent!important;}}
    section.slide{{box-sizing:border-box;width:{width}px!important;height:{height}px!important;
      background-image:var(--exact-preset-background)!important;background-size:cover!important;background-position:center!important;
      background-repeat:no-repeat!important;background-color:transparent!important;}}
    #pg-nav{{position:fixed;bottom:12px;right:20px;z-index:9999;background:#172536;color:white;padding:10px;font:16px Arial;}}
    #pg-nav button{{padding:8px 16px;margin:0 8px;cursor:pointer;}}
    @media print{{@page{{size:{width}px {height}px;margin:0;}}
      html,body{{overflow:visible;background:white;}}#pg-nav{{display:none!important;}}
      #presentation-viewport{{transform:none!important;position:static!important;height:auto!important;}}
      section.slide{{display:block!important;position:relative!important;break-after:page;}}}}
    '''
    style.text += fonts['css']
    head.append(style)
    nav = html.Element('div', id='pg-nav')
    nav.text = ''
    body.append(nav)
    config = {'sessionId': session_id, 'slideCount': len(plan.slides), 'width': width, 'height': height,
              'fileName': f'presentation-{session_id}-editable.pptx',
              'fontPolicy': fonts['policy'], 'fontCoverage': fonts['coverage']}
    script = html.Element('script')
    script.text = ('globalThis.POWERGUIDE_EXPORT_CONFIG=' + json.dumps(config) + ';\n'
                   'globalThis.POWERGUIDE_PPTX_TEMPLATE=' + json.dumps(template(len(plan.slides), width, height, fonts['faces'])) + ';\n')
    script.text += '''(()=>{const {width:w,height:h}=POWERGUIDE_EXPORT_CONFIG;
      const v=document.getElementById('presentation-viewport'),s=[...v.querySelectorAll('section.slide')];
      const displays=s.map(x=>getComputedStyle(x).display==='none'?'block':getComputedStyle(x).display);
      let n=0;const nav=document.getElementById('pg-nav');
      nav.innerHTML='<button id="pg-prev">← Назад</button><span></span><button id="pg-next">Вперёд →</button>';
      function show(i){n=Math.max(0,Math.min(s.length-1,i));s.forEach((x,j)=>{x.style.setProperty('display',j===n?displays[j]:'none','important');x.classList.toggle('active',j===n);});nav.querySelector('span').textContent=(n+1)+' / '+s.length;}
      function fit(){v.style.cssText+=';position:fixed;left:50%;top:50%;transform-origin:center;transform:translate(-50%,-50%) scale('+Math.min(innerWidth/w,innerHeight/h)+');';}
      document.getElementById('pg-prev').onclick=()=>show(n-1);document.getElementById('pg-next').onclick=()=>show(n+1);
      addEventListener('keydown',e=>{if(e.key==='ArrowRight')show(n+1);if(e.key==='ArrowLeft')show(n-1);if(e.key==='Home')show(0);if(e.key==='End')show(s.length-1);});
      addEventListener('resize',fit);show(0);fit();})();
    '''
    body.append(script)
    exporter = html.Element('script')
    exporter.text = Path(__file__).with_name('export-pptx.js').read_text()
    body.append(exporter)
    return '<!DOCTYPE html>\n' + html.tostring(doc, encoding='unicode', method='html')
