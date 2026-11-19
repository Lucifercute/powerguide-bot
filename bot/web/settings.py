import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class WebSettings:
    db_path: Path = Path('data/bot.db')
    files_dir: Path = Path('data/sessions')
    presets_dir: Path = Path('presets')
    questions_path: Path = Path('questions.yaml')
    bot_token: str = ''
    secret: str = ''
    allowed_ids: frozenset[int] = field(default_factory=frozenset)
    dev_auth: bool = False
    invites_enabled: bool = False
    dev_user_id: int = 1
    daily_limit: int = 3
    concurrency: int = 3
    editable_enabled: bool = False
    standard_enabled: bool = True
    miniapp_url: str = ''
    frontend: Path = Path('frontend/dist')

    @classmethod
    def load(cls):
        load_dotenv()
        dev = os.getenv('WEB_DEV_AUTH', '0') == '1'
        secret = os.getenv('WEB_SESSION_SECRET', '')
        if not secret and dev:
            secret = secrets.token_hex(32)
        if len(secret) < 32:
            raise RuntimeError('Задайте WEB_SESSION_SECRET длиной не менее 32 символов')
        if os.getenv('STANDARD_GENERATION_ENABLED', '1') != '1' and os.getenv('EDITABLE_PPTX_ENABLED', '0') != '1':
            raise RuntimeError('Включите хотя бы один режим генерации')
        return cls(
            db_path=Path(os.getenv('DB_PATH', 'data/bot.db')),
            files_dir=Path(os.getenv('FILES_DIR', 'data/sessions')),
            presets_dir=Path(os.getenv('PRESETS_DIR', 'presets')),
            questions_path=Path(os.getenv('QUESTIONS_PATH', 'questions.yaml')),
            bot_token=os.getenv('BOT_TOKEN', ''), secret=secret,
            allowed_ids=frozenset(int(x.strip()) for x in os.getenv('PILOT_ALLOWED_IDS', '').split(',') if x.strip()),
            dev_auth=dev, dev_user_id=int(os.getenv('WEB_DEV_USER_ID', '1')),
            invites_enabled=os.getenv('WEB_INVITES_ENABLED', '0') == '1',
            daily_limit=max(1, int(os.getenv('PILOT_DAILY_LIMIT', '3'))),
            concurrency=max(1, min(3, int(os.getenv('PILOT_CONCURRENCY', '3')))),
            editable_enabled=os.getenv('EDITABLE_PPTX_ENABLED', '0') == '1',
            standard_enabled=os.getenv('STANDARD_GENERATION_ENABLED', '1') == '1',
            miniapp_url=os.getenv('MINIAPP_URL', ''),
        )
