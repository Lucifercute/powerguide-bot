"""User-authored plans do not require the model's internal scheme notation."""
import unittest

from bot.plan import parse_slide_edit, SlideFormatError


class PlanEditTests(unittest.TestCase):
    def test_scheme_accepts_ordinary_description_without_inventing_structure(self):
        text = ('ЗАГОЛОВОК: Цикл работы\nТИП: схема\nСОДЕРЖАНИЕ:\n'
                '* Подготовить материалы.\n* Проверить результат.\n* Исправить ошибки.')
        self.assertEqual(parse_slide_edit(text, 6), text)

    def test_scheme_preserves_existing_explicit_structure(self):
        text = ('ЗАГОЛОВОК: Процесс\nТИП: схема\nСОДЕРЖАНИЕ:\nФорма: этапы\n'
                'Блоки: Подготовка; Проверка; Результат\nСвязи: Подготовка → Проверка → Результат')
        self.assertEqual(parse_slide_edit(text, 1), text)

    def test_relaxed_scheme_check_still_rejects_invalid_slide_structure(self):
        cases = [
            'ЗАГОЛОВОК: Процесс\nТИП: схема\nСОДЕРЖАНИЕ: ',
            'ЗАГОЛОВОК: \nТИП: схема\nСОДЕРЖАНИЕ: Факты',
            'ЗАГОЛОВОК: Процесс\nСОДЕРЖАНИЕ: Факты\nТИП: схема',
            'СЛАЙД 2\nЗАГОЛОВОК: Процесс\nТИП: схема\nСОДЕРЖАНИЕ: Факты',
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(SlideFormatError):
                parse_slide_edit(text, 1)
