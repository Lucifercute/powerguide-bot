"""Validate generated markup and package offline HTML with the native exporter."""
import base64
import io
import json
import re
from pathlib import Path
from zipfile import ZipFile

from lxml import html
from pptx import Presentation
from pptx.util import Inches

from .models import Deck, VisualPlan

GRAPHS = {'process', 'timeline', 'matrix', 'before_after', 'comparison',
          'bar_chart', 'stack_map', 'network', 'table'}


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
    ids = set()
    for image in deck.images:
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9-]{0,63}', image.id) or image.id in ids:
            raise ValueError('Неверный id иллюстрации')
        ids.add(image.id)
        if image.slide not in range(1, len(sections) + 1):
            raise ValueError('Неверный слайд иллюстрации')
        if len(sections[image.slide - 1].xpath('.//img[@data-asset-id=$id]', id=image.id)) != 1:
            raise ValueError('Отсутствует место для иллюстрации')
    for number in user_slides:
        if len(sections[number - 1].xpath('.//img[@data-user-slide=$n]', n=str(number))) != 1:
            raise ValueError('Пропущена фотография пользователя')
    images = doc.xpath('//img')
    if len(images) != len(ids) + len(user_slides):
        raise ValueError('Лишние или повторяющиеся изображения')
    for el in images:
        if el.get('data-asset-id') not in ids and el.get('data-user-slide') not in {str(n) for n in user_slides}:
            raise ValueError('Неизвестное изображение')
    for spec, section in zip(plan.slides, sections):
        if section.get('data-visual-type') != spec.visual_type:
            raise ValueError('Тип визуализации не соответствует плану')
        if spec.visual_type not in GRAPHS:
            continue
        graphs = section.xpath('.//*[@data-infographic=$kind]', kind=spec.visual_type)
        if len(graphs) != 1:
            raise ValueError(f'Нет инфографики на слайде {spec.number}')
        g = graphs[0]
        if spec.visual_type in {'process', 'network', 'timeline', 'stack_map'}:
            if len(g.xpath('.//*[@data-node]')) < 2:
                raise ValueError('У схемы нет узлов')
            if spec.visual_type != 'stack_map' and not g.xpath('.//*[@data-connector]|.//*[local-name()="line" or local-name()="path" or local-name()="polyline"]'):
                raise ValueError('У схемы нет соединений')
        elif spec.visual_type in {'bar_chart', 'comparison', 'before_after'}:
            if len(g.xpath('.//*[@data-bar]')) < 2:
                raise ValueError('Нет геометрии диаграммы')
        elif len(g.xpath('.//tr')) < 2 and len(g.xpath('.//*[@data-cell]')) < 4:
            raise ValueError('Нет строк и столбцов таблицы')
    return doc


def uri(data: bytes, mime: str) -> str:
    return f'data:{mime};base64,' + base64.b64encode(data).decode()


def template(count, width, height):
    p = Presentation()
    p.slide_width, p.slide_height = Inches(width / 120), Inches(height / 120)
    for _ in range(count):
        p.slides.add_slide(p.slide_layouts[6])
    out = io.BytesIO()
    p.save(out)
    with ZipFile(out) as z:
        return {n: base64.b64encode(z.read(n)).decode() for n in z.namelist()}


def package(deck, plan, assets, user_images, background, width, height, session_id):
    doc = validate_deck(deck, plan, user_images)
    for spec in deck.images:
        el = doc.xpath('//img[@data-asset-id=$id]', id=spec.id)[0]
        el.set('src', uri(*assets[spec.id]))
        el.set('alt', spec.alt)
    for number, value in user_images.items():
        doc.xpath('//img[@data-user-slide=$n]', n=str(number))[0].set('src', uri(*value))
    head, body = doc.find('head'), doc.find('body')
    policy = html.Element('meta', attrib={'http-equiv': 'Content-Security-Policy', 'content':
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'none'; font-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"})
    head.insert(0, policy)
    style = html.Element('style')
    style.text = f'''
    :root{{--exact-preset-background:url("{uri(*background)}");}}
    html,body{{margin:0;overflow:hidden;background:#222;}}
    #presentation-viewport{{width:{width}px!important;height:{height}px!important;background:transparent!important;}}
    #presentation-viewport,#presentation-viewport *{{font-family:Arial,sans-serif!important;}}
    section.slide{{box-sizing:border-box;width:{width}px!important;height:{height}px!important;
      background-image:var(--exact-preset-background)!important;background-size:100% 100%!important;
      background-repeat:no-repeat!important;background-color:transparent!important;}}
    #pg-nav{{position:fixed;bottom:12px;right:20px;z-index:9999;background:#172536;color:white;padding:10px;font:16px Arial;}}
    #pg-nav button{{padding:8px 16px;margin:0 8px;cursor:pointer;}}
    @media print{{@page{{size:{width}px {height}px;margin:0;}}
      html,body{{overflow:visible;background:white;}}#pg-nav{{display:none!important;}}
      #presentation-viewport{{transform:none!important;position:static!important;height:auto!important;}}
      section.slide{{display:block!important;position:relative!important;break-after:page;}}}}
    '''
    head.append(style)
    nav = html.Element('div', id='pg-nav')
    nav.text = ''
    body.append(nav)
    config = {'sessionId': session_id, 'slideCount': len(plan.slides), 'width': width, 'height': height,
              'fileName': f'presentation-{session_id}-editable.pptx'}
    script = html.Element('script')
    script.text = ('globalThis.POWERGUIDE_EXPORT_CONFIG=' + json.dumps(config) + ';\n'
                   'globalThis.POWERGUIDE_PPTX_TEMPLATE=' + json.dumps(template(len(plan.slides), width, height)) + ';\n')
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
