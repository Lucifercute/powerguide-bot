import base64
import binascii
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request


def telegram_user(init_data: str, token: str, now: float | None = None) -> int:
    """Validate the unmodified Telegram initData; reject ambiguous/expired input."""
    now = time.time() if now is None else now
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs) or 'hash' not in data or not token:
        raise ValueError('Некорректные данные входа')
    signature = data.pop('hash')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError('Подпись Telegram не прошла проверку')
    age = now - int(data['auth_date'])
    if age < -30 or age > 600:
        raise ValueError('Вход устарел. Откройте приложение заново из Telegram')
    uid = json.loads(data['user'])['id']
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError('Некорректный пользователь')
    return uid


def is_loopback(request: Request) -> bool:
    return (
        request.client is not None
        and request.client.host in {'127.0.0.1', '::1', 'testclient'}
        and request.url.hostname in {'localhost', '127.0.0.1', '::1', 'testserver'}
        and not any(h in request.headers for h in ('forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'))
    )


DOWNLOAD_TTL = 300


def _download_key(settings) -> bytes:
    # A separate key: a download link can never pass as a session cookie, and vice versa.
    return hmac.new(settings.secret.encode(), b'powerguide-download', hashlib.sha256).digest()


def issue_download(uid: int, sid: int, kind: str, settings, now: float | None = None) -> str:
    """Short-lived link for Telegram.WebApp.downloadFile: Telegram fetches it without our cookie."""
    data = {'uid': uid, 'sid': sid, 'kind': kind, 'exp': int(now if now is not None else time.time()) + DOWNLOAD_TTL}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return payload + '.' + hmac.new(_download_key(settings), payload.encode(), hashlib.sha256).hexdigest()


def download_claims(token: str, settings, now: float | None = None) -> dict:
    try:
        payload, signature = token.split('.')
        expected = hmac.new(_download_key(settings), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + '='*(-len(payload) % 4)))
        if data['exp'] < (time.time() if now is None else now):
            raise ValueError()
        return {'uid': int(data['uid']), 'sid': int(data['sid']), 'kind': str(data['kind'])}
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise HTTPException(404, 'Ссылка на файл устарела. Нажмите «Скачать» ещё раз') from None


def issue_session(uid, settings, *, dev=False, invitation=None):
    data = {'uid': uid, 'exp': int(time.time())+43200, 'dev': dev}
    if invitation:
        data.update(invite=invitation['id'], exp=min(data['exp'], invitation['expires_at']))
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    sig = hmac.new(settings.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + sig


async def current_user(request: Request):
    settings = request.app.state.settings
    token = request.cookies.get('powerguide_session', '')
    try:
        payload, signature = token.split('.')
        expected = hmac.new(settings.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + '='*(-len(payload) % 4)))
        if data['exp'] < time.time():
            raise ValueError()
        uid = int(data['uid'])
        if data.get('invite'):
            if not settings.invites_enabled or uid >= 0 or data.get('dev'):
                raise ValueError()
            from bot.db import Database
            from bot.web.invitations import active_invitation
            db = Database(settings.db_path)
            await db.connect(migrate=False)
            try:
                invitation = await active_invitation(db, identifier=data['invite'], uid=uid)
            finally:
                await db.close()
            if not invitation:
                raise HTTPException(401, 'Приглашение истекло или отключено. Попросите новую ссылку у владельца.')
            data['name'] = invitation['name']
        elif data.get('dev'):
            if not settings.dev_auth or not is_loopback(request) or uid != settings.dev_user_id:
                raise ValueError()
        elif uid not in settings.allowed_ids:
            raise HTTPException(403, 'Доступ открыт только участникам пилота')
        return data
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise HTTPException(401, 'Откройте приложение из Telegram или войдите локально') from None
