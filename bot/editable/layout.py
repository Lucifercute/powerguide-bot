"""Stable layout regions and typography roles around model-authored content."""
import re
from lxml import html

LAYOUT_CSS = '''
#presentation-viewport section.slide[data-visual-type="cover"] {
  justify-content:center!important;
}
#presentation-viewport [data-pg-layout-frame] {
  flex:1 1 0!important; min-height:0; display:flex; flex-direction:column;
  justify-content:center; align-items:stretch;
}
#presentation-viewport [data-pg-layout-frame] > [data-infographic] {
  flex-shrink:0; margin-top:0!important; margin-bottom:0!important;
}
'''


def prepare_layout(doc):
    """Wrap standalone infographics without flattening authored multicolumn layouts."""
    for section in doc.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," slide ")]'):
        if not section.xpath('.//*[@data-layout-main]'):
            direct = section.xpath('./*[@data-infographic]')
            if len(direct) == 1:
                graph = direct[0]
                frame = html.Element('div', attrib={'data-layout-main': '', 'data-pg-layout-frame': ''})
                section.replace(graph, frame)
                frame.append(graph)
            else:
                graphs = section.xpath('.//*[@data-infographic]')
                if len(graphs) == 1:
                    frame = graphs[0]
                    while frame.getparent() is not section:
                        frame = frame.getparent()
                    frame.set('data-layout-main', '')
        for el in section.iterdescendants():
            if not isinstance(el.tag, str) or el.get('data-font-role'):
                continue
            classes = el.get('class', '').lower()
            words = set(re.split(r'[-_\s]+', classes))
            if el.tag.lower() in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'th'} or words & {'title', 'heading', 'header'}:
                el.set('data-font-role', 'heading')
            elif words & {'num', 'number', 'numbers', 'value', 'val'}:
                el.set('data-font-role', 'numbers')
            elif not len(el) and re.match(r'^\s*[±+−-]?\d', el.text or ''):
                el.set('data-font-role', 'numbers')


def layout_issues(rows):
    """Detect small content inside a large layout frame, not just the frame size."""
    issues = []
    for row in rows:
        if row.get('exempt') or not row.get('region') or not row.get('content'):
            continue
        region, content = row['region'], row['content']
        if region['h'] <= 0:
            continue
        coverage = content['h'] / region['h']
        lower = (region['y'] + region['h'] - content['y'] - content['h']) / region['h']
        upper = (content['y'] - region['y']) / region['h']
        if coverage < .45:
            issues.append({'slide': row['slide'], 'reason': 'undersized_infographic',
                           'height_coverage': round(coverage, 3),
                           'hint': 'Увеличь сами узлы, диаграмму, строки таблицы и читаемый текст; '
                                   'содержимое должно занимать минимум 45% высоты основной зоны. '
                                   'Растягивание пустого контейнера не исправляет проблему.'})
        elif lower > .30 and lower - upper > .20:
            issues.append({'slide': row['slide'], 'reason': 'top_heavy',
                           'lower_gap': round(lower, 3),
                           'hint': 'Сбалансируй содержимое основной зоны по вертикали.'})
    return issues
