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


class SlideFormTests(unittest.TestCase):
    def test_fields_round_trip_and_unsplittable_slide(self):
        from bot.plan import compose_slide, slide_fields
        text = compose_slide("Заголовок\nв две строки", "текст + иллюстрация", "- пункт 1\n- пункт 2", 3)
        self.assertEqual(text, "ЗАГОЛОВОК: Заголовок в две строки\nТИП: текст + иллюстрация\nСОДЕРЖАНИЕ:\n- пункт 1\n- пункт 2")
        fields = slide_fields(text)
        self.assertEqual((fields.title, fields.type, fields.content),
                         ("Заголовок в две строки", "текст + иллюстрация", "- пункт 1\n- пункт 2"))
        self.assertEqual(slide_fields("**ЗАГОЛОВОК:** Итоги\n**ТИП:** Текст+иллюстрация.\n**СОДЕРЖАНИЕ:** Факты").type,
                         "текст + иллюстрация")
        broken = slide_fields("Просто текст без полей")
        self.assertEqual((broken.title, broken.type, broken.content), ("", "", "Просто текст без полей"))
