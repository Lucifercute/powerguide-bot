"""Local invitation management; no public admin endpoint or Telegram account needed."""
import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    os.chdir(ROOT)
    from dotenv import load_dotenv
    from bot.db import Database
    from bot.web.invitations import create_invitation, revoke_invitation
    from bot.web.storage import rows
    load_dotenv(ROOT/'.env')
    parser = argparse.ArgumentParser(description='Персональные ссылки для браузерного теста')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--url', required=True, help='HTTPS-адрес локального туннеля')
    create.add_argument('--name', default='Тестировщик')
    create.add_argument('--hours', type=int, default=48)
    create.add_argument('--output', type=Path, help='Сохранить ссылку в локальный файл с правами 600')
    revoke = commands.add_parser('revoke')
    revoke.add_argument('id')
    commands.add_parser('list')
    args = parser.parse_args()
    if args.command == 'create':
        url = urlsplit(args.url)
        if url.scheme != 'https' or not url.netloc or url.username or url.query or url.fragment or url.path not in ('','/'):
            parser.error('--url должен быть HTTPS-адресом без пути, параметров и пароля')
    db = Database(Path(os.getenv('DB_PATH', 'data/bot.db')))
    await db.connect()
    try:
        if args.command == 'create':
            invitation = await create_invitation(db, args.name, args.hours)
            link = args.url.rstrip('/') + '/#invite=' + invitation['token']
            expiry = datetime.fromtimestamp(invitation['expires_at'], timezone.utc).isoformat()
            result = f"ID: {invitation['id']}\nДействует до: {expiry}\n{link}\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
                    f.write(result)
                args.output.chmod(0o600)
                print(f"Приглашение {invitation['id']} сохранено: {args.output}")
            else:
                print(result, end='')
        elif args.command == 'revoke':
            await revoke_invitation(db, args.id)
            print('Приглашение отключено, активные задания аккаунта отменены.')
        else:
            for item in await rows(db, 'SELECT id,name,expires_at,revoked FROM test_invitations ORDER BY created_at DESC'):
                expiry = datetime.fromtimestamp(item['expires_at'], timezone.utc).isoformat()
                print(item['id'], item['name'], expiry, 'отключено' if item['revoked'] else 'выдано')
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
