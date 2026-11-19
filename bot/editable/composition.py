"""Deck-level checks based on painted content, not model-supplied form names."""
import os


def panel_check_enabled():
    """EDITABLE_PANEL_CHECK=0 turns off the 'too many text panels' check (it triggers most layout repairs)."""
    return os.getenv('EDITABLE_PANEL_CHECK', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


def composition_issues(rows):
    if not panel_check_enabled():
        return []
    content = [r for r in rows if r and r.get('visualType') not in (None, 'cover', 'closing')]
    # Small decks and a small number of necessary cards are legitimate.
    if len(content) < 4:
        return []
    heavy = [r for r in content if r['panelCount'] >= 3 or r['panelAreaRatio'] >= .14]
    if len(heavy) < 3 or len(heavy) / len(content) < .6:
        return []
    return [{'reason': 'repeated_text_panels', 'slides': [r['slide'] for r in heavy],
             'content_slides': len(content), 'panel_slides': len(heavy),
             'hint': 'Большинство содержательных слайдов оформлены крупными текстовыми плашками, '
                     'даже если коды форм разные. Сохрани все тексты, числа, формы, изображения и их id/prompts. '
                     'Оставь карточки там, где они нужны по смыслу; на остальных указанных слайдах '
                     'сделай другую реальную композицию: открытая типографика, крупные числа с подписями '
                     'прямо на фоне, текст рядом с изображением без общей текстовой панели, разделители '
                     'вместо отдельных заливок. Удаление только скруглений или смена data-form не решают проблему. '
                     'Не добавляй факты и не превращай несвязанные числа в общую диаграмму. '
                     'Сохрани контраст по паспорту; если в части зоны фон мешает чтению, сдвинь текст '
                     'в свободную область или оставь локальную небольшую подложку.'}]
