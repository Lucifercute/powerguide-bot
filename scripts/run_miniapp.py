"""Local launcher. Does not deploy or modify .env / Telegram settings.

uv run python scripts/run_miniapp.py
uv run python scripts/run_miniapp.py --telegram --with-bot
"""
import argparse
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description='Локальный PowerGuide Mini App')
    parser.add_argument('--telegram', action='store_true', help='Отключить локальный вход для тестирования через HTTPS-туннель')
    parser.add_argument('--share', action='store_true', help='Браузерный тест по приглашениям через HTTPS-туннель')
    parser.add_argument('--with-bot', action='store_true', help='Запустить polling тестового бота (остановите прежний локальный процесс)')
    parser.add_argument('--no-worker', action='store_true', help='Только интерфейс и API, без обращений к моделям')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not (ROOT/'frontend/dist/index.html').is_file():
        parser.error('Сначала соберите интерфейс: cd frontend && npm ci && npm run build')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    env = os.environ.copy()
    env['WEB_DEV_AUTH'] = '0' if (args.telegram or args.share) else '1'
    env['WEB_INVITES_ENABLED'] = '1' if args.share else '0'
    env['WEB_SESSION_SECRET'] = env.get('WEB_SESSION_SECRET') or secrets.token_hex(32)
    env['PILOT_ENABLED'] = '1'
    if args.telegram and not env.get('PILOT_ALLOWED_IDS','').strip():
        parser.error('Для Telegram задайте PILOT_ALLOWED_IDS со списком разрешённых Telegram ID')
    if args.with_bot and args.no_worker:
        parser.error('--with-bot требует обработчик заданий')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            parser.error(f'Порт {args.port} занят. Остановите предыдущий процесс или укажите --port')
    commands = [[sys.executable,'-m','uvicorn','bot.web.api:create_app','--factory','--host','127.0.0.1',
                 '--port',str(args.port),'--no-access-log','--no-proxy-headers']]
    if not args.no_worker:
        commands.append([sys.executable,'-m','bot.web.worker'])
    if args.with_bot:
        commands.append([sys.executable,'-m','bot'])
    processes = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        for command in commands:
            processes.append(subprocess.Popen(command,cwd=ROOT,env=env))
        print(f'PowerGuide: http://127.0.0.1:{args.port}', flush=True)
        print('Режим приглашений. Локальный вход отключён.' if args.share else 'Локальный вход отключён.' if args.telegram else 'Режим локального тестирования. Для туннеля перезапустите с --telegram или --share.',flush=True)
        while not stopping:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('Один из процессов завершился. Смотрите сообщение выше.')
            time.sleep(.3)
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill();p.wait()


if __name__ == '__main__':
    main()
