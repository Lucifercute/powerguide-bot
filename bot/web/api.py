"""Same-origin Mini App API. No model calls are made in HTTP handlers."""
import asyncio
import json
import mimetypes
import secrets
import shutil
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot.db import Database
from bot.pipeline import (
    DESCRIPTION, SOURCES_NOTE, SURVEY_CONFIRMED, PLAN, STYLE, STYLE_REF, STYLE_PRESET,
    BACKGROUND, BACKGROUND_ALT, BACKGROUND_ACCEPTED, BACKGROUND_CHOICE,
    BACKGROUND_ATTEMPT, BACKGROUND_COMMENT, RATIO_ANSWER_KEY, SKIPPED_ANSWER,
    user_image_kind, slide_kind,
)
from bot.plan import plan_slides, parse_slide_edit, replace_slide, missing_data
from bot.presets import PresetStore
from bot.questions import QuestionBank
from bot.user_images import UserImage
from bot.web.auth import current_user, telegram_user, issue_session, is_loopback
from bot.web.settings import WebSettings
from bot.web.storage import (Conflict, QuotaExceeded, row, rows, project, latest_job,
                             update_project, enqueue, cancel, result_files)
from bot.web.uploads import source_file, inspect_image


class Login(BaseModel):
    init_data: str = Field(max_length=16000)


class InvitationLogin(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class CreateProject(BaseModel):
    replace_active: bool = False


class Command(BaseModel):
    revision: int
    action: Literal['save_sources', 'analyze', 'confirm_survey', 'edit_slide', 'accept_plan',
                    'select_style', 'change_background', 'accept_background', 'generate',
                    'retry', 'cancel', 'deliver', 'delete_image']
    description: str = Field(default='', max_length=20000)
    note: str = Field(default='', max_length=20000)
    ratio: Literal['16:9', '4:3'] = '16:9'
    answers: dict[str, str] = Field(default_factory=dict, max_length=50)
    number: int = Field(default=1, ge=1, le=10)
    text: str = Field(default='', max_length=20000)
    preset: int | None = None
    change: Literal['edit', 'redraw'] = 'redraw'
    choice: Literal['1', '2'] = '1'
    mode: Literal['standard', 'editable'] = 'standard'


def create_app(settings: WebSettings | None = None):
    settings = settings or WebSettings.load()

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings.db_path)
        await db.connect()
        await db.close()
        yield

    app = FastAPI(title='PowerGuide API', version='1.0', lifespan=lifespan)
    from bot.web.body_limit import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware)
    app.state.settings = settings
    login_attempts = defaultdict(deque)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            # A local HTTPS tunnel terminates TLS before forwarding HTTP. Do not
            # trust forwarded identities, but accept HTTPS for this exact Host.
            same_origins = {str(request.base_url).rstrip('/'), 'https://' + request.headers.get('host','')}
            if origin and origin.rstrip('/') not in same_origins:
                return JSONResponse({'detail': 'Запрос с другого сайта запрещён'}, status_code=403)
            try:
                if int(request.headers.get('content-length', '0')) > 21_000_000:
                    return JSONResponse({'detail': 'Файл больше 20 МБ'}, status_code=413)
            except ValueError:
                return JSONResponse({'detail': 'Неверный размер запроса'}, status_code=400)
            if request.url.path.endswith('/uploads'):
                try:
                    await current_user(request)
                except HTTPException as error:
                    return JSONResponse({'detail': error.detail}, status_code=error.status_code)
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(QuotaExceeded)
    async def quota(request, error):
        return JSONResponse({'detail': str(error)}, status_code=429)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=422)

    async def database():
        db = Database(settings.db_path)
        await db.connect(migrate=False)
        try:
            yield db
        finally:
            await db.close()

    async def owned(db, sid, user):
        p = await project(db, sid)
        if not p or p['user_id'] != user['uid'] or await db.get_artifact(sid, 'delete_requested') is not None:
            raise HTTPException(404, 'Презентация не найдена')
        return p

    def loopback_auth_guard(request):
        if not settings.dev_auth or not is_loopback(request):
            raise HTTPException(403, 'Локальный вход здесь недоступен')

    def session_response(uid, *, dev=False, invitation=None):
        response = JSONResponse({'user_id': uid, 'is_dev': dev})
        response.set_cookie('powerguide_session', issue_session(uid, settings, dev=dev, invitation=invitation),
                            httponly=True, secure=not dev, samesite='lax', max_age=43200, path='/api')
        return response

    @app.post('/api/v1/auth/invite')
    async def invitation_login(body: InvitationLogin, request: Request, db=Depends(database)):
        if not settings.invites_enabled:
            raise HTTPException(403, 'Вход по приглашениям отключён')
        # A guest session is only issued over HTTPS; loopback HTTP is for tests.
        if request.url.scheme != 'https' and not (is_loopback(request) or request.headers.get('x-forwarded-proto') == 'https'):
            raise HTTPException(403, 'Откройте приглашение по HTTPS-ссылке')
        key = ('invite', request.client.host if request.client else 'unknown')
        attempts, now = login_attempts[key], time.monotonic()
        while attempts and attempts[0] < now-60:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(429, 'Слишком много попыток входа. Попробуйте через минуту.')
        attempts.append(now)
        from bot.web.invitations import active_invitation
        invitation = await active_invitation(db, token=body.token)
        if not invitation:
            raise HTTPException(401, 'Приглашение истекло или отключено. Попросите новую ссылку у владельца.')
        return session_response(invitation['user_id'], invitation=invitation)

    @app.get('/api/v1/config')
    async def config(request: Request):
        return {'dev_login': settings.dev_auth and is_loopback(request),
                'modes': [m for m, enabled in [('standard', settings.standard_enabled), ('editable', settings.editable_enabled)] if enabled],
                'max_slides': 10, 'daily_limit': settings.daily_limit}

    @app.post('/api/v1/auth/telegram')
    async def login(body: Login, request: Request):
        key = request.client.host if request.client else 'unknown'
        attempts = login_attempts[key]
        now = time.monotonic()
        while attempts and attempts[0] < now-60:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(429, 'Слишком много попыток входа')
        attempts.append(now)
        try:
            uid = telegram_user(body.init_data, settings.bot_token)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, 'Не удалось проверить вход. Откройте приложение заново из Telegram') from None
        if uid not in settings.allowed_ids:
            raise HTTPException(403, 'Доступ открыт только участникам пилота')
        return session_response(uid)

    @app.post('/api/v1/auth/dev')
    async def dev_login(request: Request):
        loopback_auth_guard(request)
        return session_response(settings.dev_user_id, dev=True)

    @app.post('/api/v1/auth/logout')
    async def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie('powerguide_session', path='/api')
        return response

    @app.get('/api/v1/me')
    async def me(user=Depends(current_user), db=Depends(database)):
        usage = await row(db, "SELECT count FROM pilot_usage WHERE user_id=? AND day=date('now')", (user['uid'],))
        return {'user_id': user['uid'], 'is_dev': user.get('dev', False), 'is_guest': bool(user.get('invite')),
                'name': user.get('name', ''), 'used_today': usage['count'] if usage else 0, 'daily_limit': settings.daily_limit}

    async def view(db, p):
        sid = p['id']
        a = await db.get_artifacts(sid)
        job = await latest_job(db, sid)
        busy = bool(job and job['status'] in ('queued', 'running'))
        slides = plan_slides(a.get(PLAN.kind, ''))
        missing, general = missing_data(a.get(PLAN.kind, ''))
        files = await result_files(db, sid)
        root = f'/api/v1/projects/{sid}'
        stage = p['stage'] if p['origin'] == 'miniapp' else 'chat'
        if p['status'] == 'completed':
            stage = 'complete'
        if p['status'] == 'cancelled':
            stage = 'cancelled'
        actions = []
        if p['status'] == 'in_progress' and p['origin'] == 'miniapp' and not busy:
            actions = {'sources': ['save_sources','analyze'], 'survey': ['confirm_survey'],
                       'plan': ['edit_slide','delete_image','accept_plan'], 'style': ['select_style'],
                       'background': ['change_background','accept_background'], 'mode': ['generate']}.get(stage, [])
        if job and job['status'] in ('failed', 'interrupted'):
            if (p['origin'] == 'miniapp' and p['status'] == 'in_progress') or (p['status'] == 'completed' and job['action']=='deliver'):
                actions = ['deliver'] if job['action'] == 'deliver' else ['retry']
                if job['action'] == 'background_change':
                    actions.append('accept_background')
                if job['action'] == 'generate' and a.get('generation_mode') == 'editable' and settings.standard_enabled:
                    actions.append('generate')
        previews = []
        for n in slides:
            if a.get(slide_kind(n)):
                previews.append({'number': n, 'url': root+f'/assets/{slide_kind(n)}'})
        return {
            'id': sid, 'title': p['title'] or a.get(DESCRIPTION, '')[:80] or f'Презентация {sid}',
            'origin': p['origin'], 'status': p['status'], 'stage': stage, 'revision': p['revision'],
            'created_at': p['created_at'], 'description': a.get(DESCRIPTION, ''), 'note': a.get(SOURCES_NOTE, ''),
            'answers': await db.get_answers(sid), 'questions': [{'key': q.key, 'text': q.text} for q in QuestionBank(settings.questions_path).load().questions],
            'sources': [{'name': f.name} for f in await db.get_source_files(sid)],
            'slides': [{'number': n, 'text': text, 'missing': missing.get(n, []),
                        'image': root+f'/assets/{user_image_kind(n)}' if user_image_kind(n) in a else None,
                        'caption': UserImage.from_json(a[user_image_kind(n)]).caption if user_image_kind(n) in a else ''} for n, text in slides.items()],
            'notes': general, 'job': {k: job[k] for k in ('id','action','status','progress','error')} if job else None,
            'busy': busy, 'actions': actions,
            'backgrounds': [{'choice': choice, 'url': root+f'/assets/{kind}'} for choice, kind in [('1',BACKGROUND),('2',BACKGROUND_ALT)] if a.get(kind)],
            'can_change_background': BACKGROUND_ATTEMPT not in a,
            'reference': root+'/assets/'+STYLE_REF if a.get(STYLE_REF) else None,
            'mode': a.get('generation_mode'), 'files': [{'kind': k,'label': v['label'],'url': root+'/files/'+k} for k,v in files.items() if Path(v['path']).is_file()],
            'previews': previews, 'html_preview': root+'/preview' if a.get('editable_html') and p['status']=='completed' else None,
            'text_report': a.get('text_report', ''),
        }

    @app.get('/api/v1/projects')
    async def projects(user=Depends(current_user), db=Depends(database)):
        ps = await rows(db, "SELECT * FROM sessions WHERE user_id=? AND NOT EXISTS (SELECT 1 FROM artifacts WHERE session_id=sessions.id AND kind='delete_requested') ORDER BY id DESC", (user['uid'],))
        return [{'id': p['id'], 'title': p['title'] or f'Презентация {p["id"]}', 'status': p['status'], 'origin': p['origin'], 'created_at': p['created_at']} for p in ps]

    @app.post('/api/v1/projects', status_code=201)
    async def create(body: CreateProject, user=Depends(current_user), db=Depends(database)):
        async with db.transaction():
            previous = await db.get_active_session(user['uid'])
            if previous:
                if not body.replace_active:
                    raise Conflict('Есть активная презентация. Подтвердите её отмену перед созданием новой.')
                await cancel(db, previous)
            sid = await db.start_session(user['uid'])
            await update_project(db, sid, origin='miniapp', stage='sources', waiting_for='source_files')
            if user.get('dev') or user.get('invite'):
                await db.save_artifact(sid, 'notification_disabled', '1')
        return await view(db, await project(db, sid))

    @app.get('/api/v1/projects/{sid}')
    async def get_project(sid: int, user=Depends(current_user), db=Depends(database)):
        return await view(db, await owned(db, sid, user))

    async def editable(db, p, revision=None):
        if p['origin'] != 'miniapp':
            raise Conflict('Продолжите эту презентацию в чате бота')
        if p['status'] != 'in_progress':
            raise Conflict('Презентация уже завершена или отменена')
        if revision is not None and p['revision'] != revision:
            raise Conflict('Проект изменился. Обновите экран и повторите действие.')
        job = await latest_job(db, p['id'])
        if job and job['status'] in ('queued', 'running'):
            raise Conflict('Работа уже идёт. Дождитесь завершения шага.')

    @app.post('/api/v1/projects/{sid}/commands')
    async def command(sid: int, body: Command, user=Depends(current_user), db=Depends(database)):
        async with db.transaction():
            p = await owned(db, sid, user)
            if body.action == 'cancel':
                if p['origin'] != 'miniapp':
                    raise Conflict('Отмените эту презентацию в чате или при создании новой')
                await cancel(db, sid)
            elif body.action == 'deliver':
                if p['status'] != 'completed' or user.get('dev') or user.get('invite'):
                    raise Conflict('Отправка доступна для готовой презентации участника Telegram')
                await enqueue(db, sid, 'deliver', settings.daily_limit)
            else:
                await editable(db, p, body.revision)
                a = await db.get_artifacts(sid)
                job = await latest_job(db, sid)
                failed = job and job['status'] in ('failed', 'interrupted')
                action, stage = body.action, p['stage']
                if failed and action not in ('retry','generate','accept_background'):
                    raise Conflict('Сначала продолжите прерванный шаг')
                async def queue(name):
                    await enqueue(db, sid, name, settings.daily_limit)
                if action in ('save_sources','analyze') and stage == 'sources':
                    if not body.description.strip():
                        raise ValueError('Опишите, о чём будет презентация')
                    await db.save_artifact(sid, DESCRIPTION, body.description.strip(), 'user')
                    await db.save_artifact(sid, SOURCES_NOTE, body.note.strip(), 'user')
                    await db.save_answer(sid, RATIO_ANSWER_KEY, 'Формат слайдов', body.ratio)
                    await update_project(db, sid, title=body.description.strip()[:80])
                    if action == 'analyze':
                        await queue('analyze')
                elif action == 'confirm_survey' and stage == 'survey':
                    questions = QuestionBank(settings.questions_path).load().questions
                    valid = {q.key for q in questions}
                    if set(body.answers)-valid or any(len(v)>10000 for v in body.answers.values()):
                        raise ValueError('Неверные ответы опросника')
                    for q in questions:
                        await db.save_answer(sid, q.key, q.text, body.answers.get(q.key,'').strip() or SKIPPED_ANSWER)
                    await db.save_artifact(sid, SURVEY_CONFIRMED, '', 'user')
                    await queue('plan')
                elif action == 'edit_slide' and stage == 'plan':
                    if body.number not in plan_slides(a[PLAN.kind]):
                        raise ValueError('Слайд не найден')
                    text = parse_slide_edit(body.text, body.number)
                    await db.save_artifact(sid, PLAN.kind, replace_slide(a[PLAN.kind], body.number, text), 'user')
                elif action == 'delete_image' and stage == 'plan':
                    await db.delete_artifact(sid, user_image_kind(body.number))
                elif action == 'accept_plan' and stage == 'plan':
                    await db.save_artifact(sid, 'slide_images_done', '', 'user')
                    await update_project(db, sid, stage='style', waiting_for='style_ref')
                elif action == 'select_style' and stage == 'style':
                    if body.preset is not None:
                        preset = PresetStore(settings.presets_dir).get(body.preset)
                        if not preset:
                            raise ValueError('Стиль больше недоступен')
                        await db.save_artifact(sid, STYLE_PRESET, str(preset.number), 'user')
                        await db.save_artifact(sid, STYLE, preset.style, 'preset')
                        await db.save_artifact(sid, BACKGROUND, str(preset.background), 'preset')
                        await db.save_artifact(sid, BACKGROUND_ACCEPTED, '', 'user')
                        await update_project(db, sid, stage='mode', waiting_for='generation_mode')
                    else:
                        if STYLE_REF not in a:
                            await db.save_artifact(sid, STYLE_REF, '', 'user')
                        await queue('background')
                elif action == 'change_background' and stage == 'background' and BACKGROUND_ATTEMPT not in a:
                    if body.change == 'edit' and not body.text.strip():
                        raise ValueError('Опишите, что изменить в фоне')
                    await db.save_artifact(sid, BACKGROUND_ATTEMPT, body.change, 'user')
                    await db.save_artifact(sid, BACKGROUND_COMMENT, body.text.strip(), 'user')
                    await queue('background_change')
                elif action == 'accept_background' and stage == 'background':
                    if body.choice == '2' and BACKGROUND_ALT not in a:
                        raise ValueError('Второго фона ещё нет')
                    await db.save_artifact(sid, BACKGROUND_CHOICE, body.choice, 'user')
                    await db.save_artifact(sid, BACKGROUND_ACCEPTED, '', 'user')
                    await db.conn.execute("UPDATE presentation_jobs SET status='superseded' WHERE session_id=? AND action='background_change' AND status IN ('failed','interrupted')", (sid,))
                    await update_project(db, sid, stage='mode', waiting_for='generation_mode')
                elif action == 'generate' and (stage == 'mode' or (failed and job['action']=='generate' and a.get('generation_mode')=='editable' and body.mode=='standard')):
                    if (body.mode == 'editable' and not settings.editable_enabled) or (body.mode == 'standard' and not settings.standard_enabled):
                        raise ValueError('Этот режим отключён')
                    await db.save_artifact(sid, 'generation_mode', body.mode, 'user')
                    await queue('generate')
                elif action == 'retry' and failed and job['action'] != 'deliver':
                    await queue(job['action'])
                else:
                    raise Conflict('Это действие сейчас недоступно')
                await update_project(db, sid, stage=(await project(db, sid))['stage'])
        return await view(db, await project(db, sid))

    @app.get('/api/v1/presets')
    async def presets(user=Depends(current_user)):
        return [{'number': p.number, 'preview': f'/api/v1/presets/{p.number}/preview'} for p in PresetStore(settings.presets_dir).load()]

    @app.get('/api/v1/presets/{number}/preview')
    async def preset_preview(number: int, user=Depends(current_user)):
        preset = PresetStore(settings.presets_dir).get(number)
        if not preset:
            raise HTTPException(404)
        return FileResponse(preset.preview, media_type='image/jpeg')

    @app.post('/api/v1/projects/{sid}/uploads')
    async def upload(sid: int, kind: Literal['source','reference','slide'], revision: int,
                     number: int = 1, caption: str = '', file: UploadFile = File(...),
                     user=Depends(current_user), db=Depends(database)):
        p = await owned(db, sid, user)
        await editable(db, p, revision)
        target_stage = {'source':'sources','reference':'style','slide':'plan'}[kind]
        if p['stage'] != target_stage:
            raise Conflict('Загрузка на этом шаге уже завершена')
        if kind == 'slide' and (number not in plan_slides((await db.get_artifacts(sid)).get(PLAN.kind,'')) or len(caption)>100):
            raise ValueError('Неверный слайд или подпись длиннее 100 символов')
        chunks, size = [], 0
        limit = 20_000_000 if kind=='source' else 10_000_000
        try:
            while chunk := await file.read(1024*1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f'Файл больше {limit//1_000_000} МБ')
                chunks.append(chunk)
        finally:
            await file.close()
        data = b''.join(chunks)
        if not data:
            raise ValueError('Файл пустой')
        folder = settings.files_dir / str(sid)
        if kind == 'source':
            from bot.config import _find_libreoffice
            import os
            source = await source_file(folder/'sources', file.filename or 'file', data, os.getenv('LIBREOFFICE_PATH') or _find_libreoffice())
            path = source.path
        else:
            dimensions, suffix = await asyncio.to_thread(inspect_image, data)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (secrets.token_hex(16)+suffix)
            path.write_bytes(data)
        try:
            async with db.transaction():
                p = await owned(db, sid, user)
                await editable(db, p, revision)
                if p['stage'] != target_stage:
                    raise Conflict('Шаг уже изменился')
                if kind == 'source':
                    if len(await db.get_source_files(sid)) >= 10:
                        raise ValueError('Можно загрузить не более 10 файлов')
                    await db.add_source_file(sid, source)
                elif kind == 'reference':
                    await db.save_artifact(sid, STYLE_REF, str(path), 'user')
                else:
                    await db.save_artifact(sid, user_image_kind(number), UserImage(str(path), *dimensions, caption or None).to_json(), 'user')
                await update_project(db, sid, stage=p['stage'])
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return await view(db, await project(db, sid))

    def safe_path(path, sid, *, preset=False):
        p = Path(path).resolve()
        roots = [settings.files_dir.resolve()/str(sid)]
        if preset:
            roots.append(settings.presets_dir.resolve())
        if not any(p.is_relative_to(root) for root in roots) or not p.is_file():
            raise HTTPException(404, 'Файл не найден')
        return p

    @app.get('/api/v1/projects/{sid}/assets/{kind}')
    async def asset(sid: int, kind: str, user=Depends(current_user), db=Depends(database)):
        await owned(db, sid, user)
        allowed = {BACKGROUND, BACKGROUND_ALT, STYLE_REF} | {user_image_kind(n) for n in range(1,11)} | {slide_kind(n) for n in range(1,11)}
        if kind not in allowed:
            raise HTTPException(404)
        value = await db.get_artifact(sid, kind)
        if not value:
            raise HTTPException(404)
        if kind.startswith('user_image_'):
            value = UserImage.from_json(value).path
        path = safe_path(value, sid, preset=kind in (BACKGROUND,BACKGROUND_ALT))
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])

    @app.get('/api/v1/projects/{sid}/files/{kind}')
    async def download(sid: int, kind: str, user=Depends(current_user), db=Depends(database)):
        p = await owned(db, sid, user)
        outputs = await result_files(db, sid)
        if kind not in outputs or p['status'] != 'completed':
            raise HTTPException(404)
        path = safe_path(outputs[kind]['path'], sid)
        return FileResponse(path, filename='PowerGuide'+path.suffix, content_disposition_type='attachment')

    @app.get('/api/v1/projects/{sid}/preview')
    async def preview(sid: int, user=Depends(current_user), db=Depends(database)):
        p = await owned(db, sid, user)
        value = await db.get_artifact(sid, 'editable_html')
        if p['status'] != 'completed' or not value:
            raise HTTPException(404)
        from lxml import html
        doc = html.fromstring(safe_path(value,sid).read_text())
        for element in doc.xpath('//script|//iframe|//object|//embed|//form|//meta[@http-equiv]|//*[@id="pg-nav"]|//*[@id="export-editable-pptx"]'):
            element.drop_tree()
        for el in doc.iter():
            if not isinstance(el.tag,str):
                continue
            for attr in list(el.attrib):
                if attr.lower().startswith('on'):
                    del el.attrib[attr]
        style = html.Element('style')
        style.text = 'section.slide{display:block!important;position:relative!important;margin:0 auto 20px!important}body{margin:0!important;overflow:auto!important}#presentation-viewport{height:auto!important;transform:none!important}'
        doc.append(style)
        return Response(html.tostring(doc,encoding='unicode'),media_type='text/html',headers={
            'Content-Security-Policy': "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'",
        })

    @app.delete('/api/v1/projects/{sid}', status_code=204)
    async def delete(sid: int, user=Depends(current_user), db=Depends(database)):
        async with db.transaction():
            await owned(db, sid, user)
            await cancel(db, sid)
            await db.save_artifact(sid, 'delete_requested', '1')
        from bot.web.storage import cleanup_deleted
        from bot.filelock import lock as file_lock
        with settings.db_path.with_suffix('.worker.lock').open('a') as lock:
            try:
                file_lock(lock, blocking=False)
            except BlockingIOError:
                pass  # Live worker removes the files after its task has stopped.
            else:
                await cleanup_deleted(db, settings.files_dir)
        return Response(status_code=204)

    # The API serves the production build; development Vite proxies /api to this app.
    if settings.frontend.is_dir():
        app.mount('/', StaticFiles(directory=settings.frontend, html=True), name='frontend')
    return app
