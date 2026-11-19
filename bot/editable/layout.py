"""Rendered-layout findings per slide, phrased as instructions for the model."""
import json

# Smallest readable size on a 1600 px wide slide (scaled for other canvases).
MIN_FONT_PX = 20
EXEMPT = {'cover', 'closing'}


def layout_issues(rows):
    """Content that is small inside the free area below the title, or sits in its top part."""
    issues = []
    for row in rows:
        if not row or row.get('visualType') in EXEMPT or not row.get('region') or not row.get('content'):
            continue
        region, content = row['region'], row['content']
        if region['h'] <= 0:
            continue
        coverage = content['h'] / region['h']
        lower = (region['y'] + region['h'] - content['y'] - content['h']) / region['h']
        upper = (content['y'] - region['y']) / region['h']
        if coverage < .55:
            issues.append({'slide': row['slide'], 'reason': 'undersized_content', 'height_coverage': round(coverage, 3),
                           'hint': f'Содержимое занимает {coverage:.0%} высоты свободной зоны под заголовком. '
                                   'Увеличь сами элементы — узлы, диаграмму, числа, иконки, кегль основного текста — '
                                   'чтобы они занимали не меньше 60–70 % высоты зоны. Растягивание пустых контейнеров '
                                   'и добавление новых фактов не решают задачу.'})
        elif lower > .25 and lower - upper > .15:
            issues.append({'slide': row['slide'], 'reason': 'top_heavy', 'lower_gap': round(lower, 3),
                           'hint': f'Нижние {lower:.0%} свободной зоны пусты: содержимое прижато вверх. '
                                   'Распредели его по высоте зоны или увеличь масштаб главного элемента.'})
    return issues


def slide_issues(result) -> dict:
    """{slide: [hints]} from all rendered checks."""
    found = {}

    def add(slide, hint):
        found.setdefault(int(slide), []).append(hint)
    for row in result.get('textBounds', []):
        if row['issues']:
            add(row['slide'], 'Текст выходит за край слайда: ' + json.dumps(row['issues'][:5], ensure_ascii=False)
                + '. Сократи отступы и размер графики, не уменьшая кегль ниже минимума.')
    for row in result.get('smallText', []):
        if row['items']:
            add(row['slide'], f'Кегль ниже минимума {MIN_FONT_PX} px: '
                + json.dumps(row['items'][:6], ensure_ascii=False) + '. Подними кегль, при нехватке места — '
                'перестрой композицию или сократи отступы.')
    for row in result.get('brokenWords', []):
        if row['items']:
            add(row['slide'], 'Слово разорвано переносом посередине: ' + json.dumps(row['items'][:3], ensure_ascii=False)
                + '. Блоку не хватает ширины: расширь колонку или уменьши кегль в пределах паспорта.')
    for issue in result.get('layoutIssues', []):
        add(issue['slide'], issue['hint'])
    for issue in result.get('compositionIssues', []):
        for slide in issue['slides']:
            add(slide, issue['hint'])
    return found
