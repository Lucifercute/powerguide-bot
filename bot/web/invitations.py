"""Revocable browser test accounts. Only a token digest is saved in SQLite."""
import hashlib
import secrets
import time

from bot.web.storage import row, rows, cancel


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invitation(db, name='Тестировщик', hours=48):
    if not 1 <= hours <= 168:
        raise ValueError('Срок приглашения: от 1 до 168 часов')
    token, identifier = secrets.token_urlsafe(32), secrets.token_hex(12)
    async with db.transaction():
        # Negative IDs cannot collide with Telegram users or the local preview.
        account = await row(db, 'SELECT min(user_id) AS lowest FROM test_invitations')
        uid = min(-1, (account['lowest'] or 0)-1)
        expiry = int(time.time()) + hours*3600
        await db.conn.execute('INSERT INTO test_invitations(id,token_hash,user_id,name,expires_at) VALUES (?,?,?,?,?)',
                              (identifier, digest(token), uid, name.strip()[:80] or 'Тестировщик', expiry))
    return {'id': identifier, 'token': token, 'user_id': uid, 'expires_at': expiry}


async def active_invitation(db, *, token=None, identifier=None, uid=None):
    if token is not None:
        invitation = await row(db, 'SELECT * FROM test_invitations WHERE token_hash=?', (digest(token),))
    else:
        invitation = await row(db, 'SELECT * FROM test_invitations WHERE id=? AND user_id=?', (identifier, uid))
    if not invitation or invitation['revoked'] or invitation['expires_at'] <= time.time():
        return None
    return invitation


async def revoke_invitation(db, identifier):
    async with db.transaction():
        invitation = await row(db, 'SELECT * FROM test_invitations WHERE id=?', (identifier,))
        if not invitation:
            raise ValueError('Приглашение не найдено')
        await db.conn.execute('UPDATE test_invitations SET revoked=1 WHERE id=?', (identifier,))
        for p in await rows(db, "SELECT id FROM sessions WHERE user_id=? AND status='in_progress'", (invitation['user_id'],)):
            await cancel(db, p['id'])
