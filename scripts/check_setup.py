"""Check local prerequisites without contacting Telegram or model APIs."""
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPTS = (
    '00_survey_analysis', '01_tz', '02_data', '02a_search_queries',
    '03-1_plan', '03-2_plan_check', '04_background', '04a_background_fields',
    '04b_background_edit', '04c_background_redraw', '04d_background_fields_redraw',
    '05_style', '06_descriptions', '06a_user_images', '07_slide',
    '07a_slide_reference', '07b_user_image_area', '07c_no_user_image', '08_text_check',
)


def main():
    load_dotenv(ROOT / '.env')
    errors = []
    for name in ('BOT_TOKEN', 'GEMINI_API_KEY', 'VERTEX_API_KEY'):
        value = os.getenv(name, '')
        if not value or 'your-' in value:
            errors.append(f'{name}: заполните своим значением в .env')
    if not (os.getenv('GEMINI_TEXT_MODELS', '').strip() or os.getenv('VERTEX_TEXT_MODELS', '').strip()):
        errors.append('Задайте GEMINI_TEXT_MODELS или VERTEX_TEXT_MODELS')
    prompts = Path(os.getenv('PROMPTS_DIR', 'prompts'))
    if not prompts.is_absolute():
        prompts = ROOT / prompts
    missing = [name + '.md' for name in PROMPTS if not (prompts / (name + '.md')).is_file()]
    if missing:
        errors.append('Не хватает рабочих промптов (они не публикуются): ' + ', '.join(missing))
    enabled = os.getenv('EDITABLE_PPTX_ENABLED', '0').lower() in {'1', 'true', 'yes'}
    if enabled:
        from bot.editable.prompts import load as load_editable_prompts
        try:
            load_editable_prompts(prompts)
        except RuntimeError as error:
            errors.append(str(error))
        from bot.editable.browser import chrome_path
        try:
            print('Chrome:', chrome_path())
        except RuntimeError as error:
            errors.append(str(error))
    print('Выбор редактируемого PPTX:', 'включён' if enabled else 'выключен')
    from bot.presets import PresetStore
    presets = Path(os.getenv('PRESETS_DIR', 'presets'))
    if not presets.is_absolute():
        presets = ROOT / presets
    print('Готовых стилей:', len(PresetStore(presets).load()), '(без них можно использовать свой референс)')
    office = os.getenv('LIBREOFFICE_PATH') or shutil.which('soffice')
    if not office and Path('/Applications/LibreOffice.app/Contents/MacOS/soffice').is_file():
        office = '/Applications/LibreOffice.app/Contents/MacOS/soffice'
    print('LibreOffice:', office or 'не найден; Word/PowerPoint будут читаться как текст')
    for error in errors:
        print('Нужно настроить:', error)
    if not errors:
        print('Локальные файлы и настройки готовы. Доступность ключей и моделей проверяется при запуске.')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
