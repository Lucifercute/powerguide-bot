"""Versioned, additive migrations and short SQLite transactions for the pilot.

Database connections belong to one HTTP request or one running job. Never hold a
transaction while awaiting Google, Telegram, file conversion or another process.
"""
import json


async def migrate(conn):
    await conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)')
    async with conn.execute('SELECT version FROM schema_migrations') as cur:
        versions = {r[0] for r in await cur.fetchall()}
    if 1 not in versions:
        async with conn.execute('PRAGMA table_info(sessions)') as cur:
            columns = {r[1] for r in await cur.fetchall()}
        for name, definition in {
            'origin': "TEXT NOT NULL DEFAULT 'chat'",
            'stage': "TEXT NOT NULL DEFAULT 'description'",
            'revision': 'INTEGER NOT NULL DEFAULT 0',
            'title': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                await conn.execute(f'ALTER TABLE sessions ADD COLUMN {name} {definition}')
        await conn.executescript('''
            CREATE TABLE IF NOT EXISTS presentation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress TEXT NOT NULL DEFAULT 'В очереди',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_live_job ON presentation_jobs(session_id)
                WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS generation_usage (
                session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                started_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS pilot_usage (
                user_id INTEGER NOT NULL, day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,day)
            );
        ''')
        await conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES (1)')
    if 2 not in versions:
        await conn.execute('''CREATE TABLE IF NOT EXISTS test_invitations (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL UNIQUE CHECK(user_id < 0),
            name TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        await conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES (2)')


class Conflict(ValueError):
    pass


class QuotaExceeded(ValueError):
    pass


async def row(db, sql, parameters=()):
    async with db.conn.execute(sql, parameters) as cur:
        result = await cur.fetchone()
    return dict(result) if result else None


async def rows(db, sql, parameters=()):
    async with db.conn.execute(sql, parameters) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def project(db, sid):
    return await row(db, 'SELECT * FROM sessions WHERE id=?', (sid,))


async def latest_job(db, sid):
    return await row(db, 'SELECT * FROM presentation_jobs WHERE session_id=? ORDER BY id DESC LIMIT 1', (sid,))


async def update_project(db, sid, **values):
    assert values.keys() <= {'stage', 'origin', 'title', 'waiting_for'}
    columns = ','.join(f'{name}=?' for name in values)
    await db.conn.execute(f'UPDATE sessions SET {columns},revision=revision+1 WHERE id=?', (*values.values(), sid))
    await db.commit()


async def enqueue(db, sid, action, daily_limit=3):
    """Caller holds BEGIN IMMEDIATE, including all prerequisite state changes."""
    p = await project(db, sid)
    if not p or (p['status'] != 'in_progress' and action != 'deliver'):
        raise Conflict('Презентация уже завершена или отменена')
    job = await latest_job(db, sid)
    if job and job['status'] in ('queued', 'running'):
        raise Conflict('Работа уже идёт')
    if action != 'deliver' and not await row(db, 'SELECT session_id FROM generation_usage WHERE session_id=?', (sid,)):
        usage = await row(db, "SELECT count FROM pilot_usage WHERE user_id=? AND day=date('now')", (p['user_id'],))
        if usage and usage['count'] >= daily_limit:
            raise QuotaExceeded('Достигнут суточный лимит новых генераций. Продолжите завтра (UTC).')
        await db.conn.execute("INSERT INTO pilot_usage(user_id,day,count) VALUES (?,date('now'),1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1", (p['user_id'],))
        await db.conn.execute('INSERT INTO generation_usage(session_id,user_id) VALUES (?,?)', (sid, p['user_id']))
    cur = await db.conn.execute('INSERT INTO presentation_jobs(session_id,action) VALUES (?,?)', (sid, action))
    return cur.lastrowid


async def cancel(db, sid):
    await db.finish_session(sid, 'cancelled')
    await db.conn.execute("UPDATE presentation_jobs SET status='cancelled',progress='Отменено',updated_at=datetime('now') WHERE session_id=? AND status IN ('queued','running')", (sid,))
    await db.commit()


async def result_files(db, sid):
    artifacts = await db.get_artifacts(sid)
    kinds = {'result_pdf': 'PDF', 'result_pptx': 'PowerPoint', 'editable_pptx': 'Редактируемый PowerPoint', 'editable_html': 'HTML'}
    mode = artifacts.get('generation_mode')
    if mode == 'standard':
        kinds = {k:v for k,v in kinds.items() if k.startswith('result_')}
    elif mode == 'editable':
        kinds = {k:v for k,v in kinds.items() if k.startswith('editable_')}
    return {kind: {'label': label, 'path': artifacts[kind]} for kind, label in kinds.items() if artifacts.get(kind)}


async def cleanup_deleted(db, files_dir, *, active_sessions=()):
    """Only the worker or a caller holding its OS lock may delete session files."""
    import asyncio
    import shutil
    pending = await rows(db, "SELECT session_id FROM artifacts WHERE kind='delete_requested'")
    for p in pending:
        sid = p['session_id']
        if sid in active_sessions:
            continue
        folder = files_dir / str(sid)
        if folder.is_symlink():
            folder.unlink()
        elif folder.is_dir():
            await asyncio.to_thread(shutil.rmtree, folder)
        async with db.transaction():
            await db.conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
