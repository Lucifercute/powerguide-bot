"""Pilot contract tests. Models, search and Telegram are fakes; no paid calls."""
import asyncio
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import httpx
from PIL import Image as PILImage

from bot.db import Database
from bot.llm import Image
from bot.pipeline import BackgroundFields, Pipeline, PLAN
from bot.questions import QuestionBank
from bot.web.api import create_app
from bot.web.auth import telegram_user, issue_session
from bot.web.engine import PresentationEngine
from bot.web.settings import WebSettings
from bot.web.storage import project, latest_job, enqueue, row, cleanup_deleted, Conflict
from bot.web.worker import Worker

PLAN_TEXT = '\n\n'.join(f'СЛАЙД {n}\nЗАГОЛОВОК: Проверка {n}\nТИП: текст\nСОДЕРЖАНИЕ: Доход 12,5 млн рублей' for n in (1,2))
DESCRIPTIONS = '\n\n'.join(f'Изображение {n}\nТекст на изображении: «Доход 12,5 млн рублей»' for n in (1,2))


def signed_data(token, uid=42, timestamp=None):
    data = {'auth_date': str(int(time.time()) if timestamp is None else timestamp), 'user': json.dumps({'id':uid})}
    secret = hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    data['hash'] = hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)


def fake_pipeline(image):
    return SimpleNamespace(
        analyze_survey=AsyncMock(return_value=({'about.type':'Компания'},'fake')),
        run_step=AsyncMock(side_effect=lambda step,*a: (PLAN_TEXT if step.kind=='plan' else 'Тестовые данные','fake')),
        find_sources=AsyncMock(return_value=''),
        background_fields=AsyncMock(return_value=BackgroundFields(topic='test',palette='white',style='simple',mood='calm')),
        background=AsyncMock(return_value=(image,'fake')),
        edit_background=AsyncMock(return_value=(image,'fake')),
        style=AsyncMock(return_value=('simple','fake')),descriptions=AsyncMock(return_value=(DESCRIPTIONS,'fake')),
        slide_prompt=Mock(return_value='prompt'),slide=AsyncMock(return_value=(image,'fake')),
        check_slide_text=AsyncMock(return_value=([],'fake')),
        editable_service=SimpleNamespace(generate=AsyncMock()),
        )


class TelegramAuthTests(unittest.TestCase):
    def test_valid_expired_future_tampered_and_duplicate_initdata(self):
        token = '123:test'
        self.assertEqual(telegram_user(signed_data(token),token),42)
        for data in (signed_data(token,timestamp=int(time.time())-601), signed_data(token,timestamp=int(time.time())+100),
                     signed_data(token)+'&auth_date=1', signed_data(token).replace('42','43')):
            with self.assertRaises(ValueError):
                telegram_user(data,token)


class MiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = WebSettings(db_path=self.root/'bot.db',files_dir=self.root/'sessions',
            presets_dir=self.root/'presets', questions_path=Path('questions.yaml'),
            secret='x'*40,bot_token='123:test',allowed_ids=frozenset({42,43}),dev_auth=True,
            editable_enabled=True,frontend=self.root/'absent')
        self.app = create_app(self.settings)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('127.0.0.1',123)),base_url='http://127.0.0.1')
        response = await self.client.post('/api/v1/auth/dev')
        self.assertEqual(response.status_code,200)
        self.db = Database(self.settings.db_path)
        await self.db.connect(migrate=False)
        raw = io.BytesIO(); PILImage.new('RGB',(320,180),'white').save(raw,format='PNG')
        self.image = Image(raw.getvalue(),'image/png')
        self.pipeline = fake_pipeline(self.image)
        self.worker = Worker(self.settings,self.pipeline,QuestionBank(self.settings.questions_path))

    async def asyncTearDown(self):
        await self.client.aclose(); await self.db.close(); await self.life.__aexit__(None,None,None)
        self.tmp.cleanup()

    async def create(self):
        response = await self.client.post('/api/v1/projects',json={})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    async def command(self,p,action,**values):
        response = await self.client.post(f'/api/v1/projects/{p["id"]}/commands',json={'revision':p['revision'],'action':action,**values})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def get(self,p):
        response = await self.client.get(f'/api/v1/projects/{p["id"]}')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def work(self):
        job = await self.worker.claim(self.db)
        self.assertIsNotNone(job)
        await self.worker.run_job(job)
        return job

    async def to_plan(self):
        p = await self.create()
        p = await self.command(p,'analyze',description='Презентация компании')
        self.assertTrue(p['busy'])
        await self.work(); p = await self.get(p)
        self.assertEqual(p['stage'],'survey')
        self.assertEqual(p['answers']['about.type'],'Компания')
        p = await self.command(p,'confirm_survey',answers={'about.type':'Компания','source.research':'Нет'})
        await self.work(); p = await self.get(p)
        self.assertEqual(p['stage'],'plan'); self.assertEqual(len(p['slides']),2)
        return p

    async def to_mode(self):
        p = await self.to_plan()
        p = await self.command(p,'accept_plan')
        p = await self.command(p,'select_style')
        await self.work(); p = await self.get(p)
        self.assertEqual(p['stage'],'background')
        image = await self.client.get(p['backgrounds'][0]['url'])
        self.assertEqual(image.status_code,200)
        return await self.command(p,'accept_background')

    async def test_source_comments_reach_analysis_with_the_correct_files(self):
        p = await self.create()
        for contents in (b'First document', b'Second document'):
            response = await self.client.post(
                f'/api/v1/projects/{p["id"]}/uploads',
                params={'kind': 'source', 'revision': p['revision']},
                files={'file': ('data.txt', contents, 'text/plain')},
            )
            self.assertEqual(response.status_code, 200, response.text)
            p = response.json()

        p = await self.command(p, 'save_sources', description='Presentation', note='General instructions')
        p = await self.command(p, 'analyze', description='Presentation', sources_confirmed=True, source_comments={1: 'Use section 1', 2: 'Check section 2'})
        await self.work()
        analysis_args = self.pipeline.analyze_survey.await_args.args
        self.assertEqual(analysis_args[2], 'General instructions')
        parts = await Pipeline._file_parts(SimpleNamespace(llm=SimpleNamespace(can_upload=False)), analysis_args[3])
        self.assertIn('Use section 1', parts[1])
        self.assertIn('First document', parts[1])
        self.assertIn('Check section 2', parts[2])
        self.assertIn('Second document', parts[2])

    async def test_delete_sources_preserves_other_comments_and_frees_upload_slot(self):
        p = await self.create()

        async def upload(project, content):
            response = await self.client.post(
                f'/api/v1/projects/{project["id"]}/uploads',
                params={'kind': 'source', 'revision': project['revision']},
                files={'file': ('same.txt', content, 'text/plain')},
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        p = await upload(p, b'First')
        p = await upload(p, b'Second')
        first, second = await self.db.get_source_files(p['id'])
        p = await self.command(p, 'delete_source', source_position=first.position)
        self.assertFalse(first.path.exists())
        self.assertFalse(p['sources_confirmed'])
        p = await upload(p, b'Third')
        third = p['sources'][-1]['position']
        self.assertNotEqual(third, second.position)
        p = await self.command(p, 'save_sources', description='Presentation', sources_confirmed=True,
                               source_comments={second.position: 'Keep this comment'})
        p = await self.command(p, 'delete_source', source_position=third)
        self.assertTrue(p['sources_confirmed'])
        self.assertEqual(p['sources'], [{'position': second.position, 'name': 'same.txt',
                                         'comment': 'Keep this comment'}])
        self.assertTrue(second.path.exists())
        p = await self.command(p, 'delete_source', source_position=second.position)
        self.assertEqual(p['sources'], [])
        self.assertFalse(second.path.exists())

    async def test_complete_standard_pipeline_and_history_downloads(self):
        p = await self.to_mode()
        p = await self.command(p,'generate',mode='standard')
        await self.work(); p = await self.get(p)
        self.assertEqual(p['status'],'completed')
        self.assertEqual(len(p['files']),2); self.assertEqual(len(p['previews']),2)
        for file in p['files']:
            r = await self.client.get(file['url']); self.assertEqual(r.status_code,200)
            self.assertIn('attachment',r.headers['content-disposition'])
            self.assertTrue(r.content.startswith(b'%PDF' if file['kind']=='result_pdf' else b'PK'))
        self.assertEqual(self.pipeline.slide.await_count,2)
        await self.work()  # independent notification job, disabled for local identity
        self.assertEqual(self.pipeline.slide.await_count,2)
        self.assertEqual((await self.client.get('/api/v1/me')).json()['used_today'],1)

    async def test_editable_result_preview_is_sandboxed_and_delivery_is_separate(self):
        async def editable(db,sid,folder,status):
            folder = folder/str(sid); folder.mkdir(parents=True,exist_ok=True)
            html = folder/'presentation.html'; pptx=folder/'presentation.pptx'
            html.write_text('<html><body><script>parent.evil=1</script><section class="slide" onclick="alert(1)">Test</section></body></html>')
            pptx.write_bytes(b'PK_TEST_FIXTURE')
            await db.save_artifact(sid,'editable_html',str(html)); await db.save_artifact(sid,'editable_pptx',str(pptx))
            return html,pptx
        self.pipeline.editable_service.generate.side_effect=editable
        p=await self.to_mode(); p=await self.command(p,'generate',mode='editable')
        await self.work(); p=await self.get(p)
        self.assertEqual(p['status'],'completed')
        r=await self.client.get(p['html_preview'])
        self.assertNotIn('<script',r.text); self.assertNotIn('onclick',r.text)
        self.assertIn('sandbox',r.headers['content-security-policy'])
        self.assertEqual((await latest_job(self.db,p['id']))['action'],'deliver')

    async def test_uploads_edit_plan_image_and_stale_commands(self):
        p=await self.create()
        r=await self.client.post(f'/api/v1/projects/{p["id"]}/uploads?kind=source&revision={p["revision"]}',files={'file':('data.txt','Факты'.encode(),'text/plain')})
        self.assertEqual(r.status_code,200,r.text); p=r.json(); self.assertEqual(p['sources'][0]['name'],'data.txt')
        r=await self.client.post(f'/api/v1/projects/{p["id"]}/uploads?kind=source&revision={p["revision"]}',files={'file':('bad.png',b'not image','image/png')})
        self.assertEqual(r.status_code,422)
        old=p['revision']; p=await self.command(p,'analyze',description='Описание')
        r=await self.client.post(f'/api/v1/projects/{p["id"]}/commands',json={'revision':old,'action':'analyze','description':'Описание'})
        self.assertEqual(r.status_code,409)
        await self.work();p=await self.get(p);p=await self.command(p,'confirm_survey',answers={})
        await self.work();p=await self.get(p)
        r=await self.client.post(f'/api/v1/projects/{p["id"]}/uploads?kind=slide&number=1&caption=Фото&revision={p["revision"]}',files={'file':('photo.png',self.image.data,'image/png')})
        self.assertEqual(r.status_code,200,r.text);p=r.json()
        self.assertEqual((await self.client.get(p['slides'][0]['image'])).status_code,200)
        p=await self.command(p,'edit_slide',number=1,text='ЗАГОЛОВОК: Новый\nТИП: текст\nСОДЕРЖАНИЕ: Факты')
        self.assertIn('Новый',p['slides'][0]['text'])
        p=await self.command(p,'delete_image',number=1)
        self.assertIsNone(p['slides'][0]['image'])

    async def test_failed_and_interrupted_resume_reuse_artifacts_and_quota(self):
        p=await self.to_mode();p=await self.command(p,'generate',mode='standard')
        self.pipeline.slide.side_effect=RuntimeError('provider failed')
        with self.assertLogs('bot.web.worker',level='ERROR'): await self.work()
        p=await self.get(p);self.assertIn('retry',p['actions'])
        self.pipeline.slide.side_effect=None
        p=await self.command(p,'retry');job=await self.worker.claim(self.db)
        # Simulate process exit after claim. Recovery requires an explicit retry.
        await self.db.conn.execute("UPDATE presentation_jobs SET status='interrupted' WHERE id=?",(job['id'],));await self.db.commit()
        p=await self.get(p);self.assertFalse(p['busy'])
        p=await self.command(p,'retry');await self.work();p=await self.get(p)
        self.assertEqual(p['status'],'completed')
        self.assertEqual(self.pipeline.descriptions.await_count,1)
        self.assertEqual((await self.client.get('/api/v1/me')).json()['used_today'],1)

    async def test_accept_original_background_after_failed_change(self):
        p=await self.to_plan();p=await self.command(p,'accept_plan');p=await self.command(p,'select_style')
        await self.work();p=await self.get(p)
        self.pipeline.edit_background.side_effect=RuntimeError('provider failed')
        p=await self.command(p,'change_background',change='edit',text='Исправить фон')
        with self.assertLogs('bot.web.worker',level='ERROR'):await self.work()
        p=await self.get(p);self.assertIn('accept_background',p['actions'])
        p=await self.command(p,'accept_background',choice='1')
        self.assertEqual(p['stage'],'mode');self.assertEqual(p['actions'],['generate'])
        self.assertEqual(p['job']['status'],'superseded')

    async def test_ownership_csrf_and_no_static_filesystem_access(self):
        p=await self.create()
        token=issue_session(43,self.settings)
        self.client.cookies.set('powerguide_session',token)
        r=await self.client.get(f'/api/v1/projects/{p["id"]}');self.assertEqual(r.status_code,404)
        r=await self.client.delete(f'/api/v1/projects/{p["id"]}');self.assertEqual(r.status_code,404)
        r=await self.client.post('/api/v1/projects',json={},headers={'origin':'https://attacker.example'})
        self.assertEqual(r.status_code,403)
        r=await self.client.get('/data/bot.db');self.assertEqual(r.status_code,404)

    async def test_local_auth_blocked_for_tunnels_and_non_loopback(self):
        r=await self.client.post('/api/v1/auth/dev',headers={'x-forwarded-for':'127.0.0.1'})
        self.assertEqual(r.status_code,403)
        r=await self.client.get('/api/v1/projects',headers={'host':'public.example'})
        self.assertEqual(r.status_code,401)
        external=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('192.0.2.1',80)),base_url='http://127.0.0.1')
        try:self.assertEqual((await external.post('/api/v1/auth/dev')).status_code,403)
        finally:await external.aclose()

    async def test_telegram_allowlist_and_session_cookie(self):
        r=await self.client.post('/api/v1/auth/telegram',json={'init_data':signed_data(self.settings.bot_token,44)})
        self.assertEqual(r.status_code,403)
        r=await self.client.post('/api/v1/auth/telegram',json={'init_data':signed_data(self.settings.bot_token,42)})
        self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['set-cookie']);self.assertIn('HttpOnly',r.headers['set-cookie'])
        # TLS terminates at the local tunnel, while Uvicorn receives plain HTTP.
        r=await self.client.post('/api/v1/auth/telegram',json={'init_data':signed_data(self.settings.bot_token,42)},
            headers={'host':'pilot.example','origin':'https://pilot.example','x-forwarded-proto':'https'})
        self.assertEqual(r.status_code,200)

    async def test_single_active_confirmation_quota_and_deletion(self):
        self.settings.daily_limit=1
        p=await self.create();p=await self.command(p,'analyze',description='Первая')
        r=await self.client.post('/api/v1/projects',json={});self.assertEqual(r.status_code,409)
        r=await self.client.post('/api/v1/projects',json={'replace_active':True});self.assertEqual(r.status_code,201)
        new=r.json();self.assertEqual((await project(self.db,p['id']))['status'],'cancelled')
        self.assertEqual((await latest_job(self.db,p['id']))['status'],'cancelled')
        r=await self.client.post(f'/api/v1/projects/{new["id"]}/commands',json={'action':'analyze','revision':new['revision'],'description':'Вторая'})
        self.assertEqual(r.status_code,429)
        folder=self.settings.files_dir/str(p['id']);folder.mkdir(parents=True);(folder/'source.txt').write_text('private')
        r=await self.client.delete(f'/api/v1/projects/{p["id"]}');self.assertEqual(r.status_code,204)
        self.assertFalse(folder.exists());self.assertIsNone(await project(self.db,p['id']))
        # Deleting a project must not reset the user's daily spending allowance.
        self.assertEqual((await self.client.get('/api/v1/me')).json()['used_today'],1)

    async def test_duplicate_claims_and_job_rejection(self):
        p=await self.create();await self.command(p,'analyze',description='Test')
        other=Database(self.settings.db_path);await other.connect(migrate=False)
        try:
            claims=await asyncio.gather(self.worker.claim(self.db),self.worker.claim(other))
            self.assertEqual(sum(j is not None for j in claims),1)
            async with self.db.transaction():
                with self.assertRaises(Conflict): await enqueue(self.db,p['id'],'analyze')
        finally:await other.close()

    async def test_chat_origin_cannot_be_edited_in_app(self):
        sid=await self.db.start_session(1)
        p=(await self.client.get(f'/api/v1/projects/{sid}')).json()
        self.assertEqual(p['stage'],'chat')
        r=await self.client.post(f'/api/v1/projects/{sid}/commands',json={'action':'analyze','revision':0,'description':'test'})
        self.assertEqual(r.status_code,409)

    async def test_chat_generation_queues_without_calling_models(self):
        from bot import handlers
        sid=await self.db.start_session(42)
        self.db.queue_settings=self.settings
        message=AsyncMock()
        await handlers.run_analysis(message,self.db,QuestionBank(self.settings.questions_path),self.pipeline,sid)
        self.pipeline.analyze_survey.assert_not_awaited()
        self.assertEqual((await latest_job(self.db,sid))['action'],'analyze')
        await self.work()
        self.pipeline.analyze_survey.assert_awaited_once()
        self.assertEqual((await project(self.db,sid))['stage'],'survey')

    async def test_worker_cancels_inflight_request_before_deleting_files(self):
        started,stopped=asyncio.Event(),asyncio.Event()
        async def analyze(*args):
            started.set()
            try:await asyncio.Event().wait()
            finally:stopped.set()
        self.pipeline.analyze_survey.side_effect=analyze
        p=await self.create();p=await self.command(p,'analyze',description='Долгая генерация')
        task=asyncio.create_task(self.worker.run())
        try:
            await asyncio.wait_for(started.wait(),5)
            second=Worker(self.settings,self.pipeline,QuestionBank(self.settings.questions_path))
            with self.assertRaisesRegex(RuntimeError,'уже запущен'):
                await second.run()
            folder=self.settings.files_dir/str(p['id']);folder.mkdir(parents=True);(folder/'data.txt').write_text('private')
            r=await self.client.delete(f'/api/v1/projects/{p["id"]}')
            self.assertEqual(r.status_code,204)
            await asyncio.wait_for(stopped.wait(),5)
            for _ in range(30):
                if await project(self.db,p['id']) is None:break
                await asyncio.sleep(.1)
            self.assertIsNone(await project(self.db,p['id']));self.assertFalse(folder.exists())
        finally:
            self.worker.stopping.set();await asyncio.wait_for(task,5)

    async def test_worker_restart_requires_button_for_running_jobs(self):
        p=await self.create();await self.command(p,'analyze',description='Тест перезапуска')
        await self.worker.claim(self.db)
        task=asyncio.create_task(self.worker.run())
        try:
            for _ in range(50):
                if (await latest_job(self.db,p['id']))['status']=='interrupted':break
                await asyncio.sleep(.05)
            self.assertEqual((await latest_job(self.db,p['id']))['status'],'interrupted')
            self.pipeline.analyze_survey.assert_not_awaited()
            p=await self.get(p);self.assertIn('retry',p['actions'])
        finally:
            self.worker.stopping.set();await asyncio.wait_for(task,5)

    async def test_notification_failure_keeps_completed_presentation(self):
        from aiogram import Bot
        from aiogram.methods import SendDocument
        from aiogram.types import Message, Chat
        from datetime import datetime, timezone
        class RecordingBot(Bot):
            def __init__(self):
                super().__init__('123:test')
                self.fail=True;self.sent=[]
            async def __call__(self,method,**kwargs):
                if isinstance(method,SendDocument) and self.fail:
                    raise RuntimeError('Telegram unavailable')
                self.sent.append(method)
                return Message(message_id=len(self.sent),date=datetime.now(timezone.utc),chat=Chat(id=1,type='private')).as_(self)
        bot=RecordingBot();self.worker.bot=bot
        try:
            p=await self.to_mode();await self.db.delete_artifact(p['id'],'notification_disabled')
            await self.command(p,'generate',mode='standard');await self.work()
            with self.assertLogs('bot.web.worker',level='ERROR'):await self.work()
            p=await self.get(p);self.assertEqual(p['status'],'completed');self.assertEqual(len(p['files']),2)
            self.assertEqual((await latest_job(self.db,p['id']))['status'],'failed')
            bot.fail=False
            async with self.db.transaction():await enqueue(self.db,p['id'],'deliver')
            await self.work()
            self.assertEqual(self.pipeline.slide.await_count,2)
            self.assertEqual(sum(isinstance(m,SendDocument) for m in bot.sent),2)
        finally:await bot.session.close()

    async def test_concurrent_startup_migrations_preserve_old_sessions(self):
        path=self.root/'fresh.db'
        connections=[Database(path) for _ in range(3)]
        try:
            await asyncio.gather(*(db.connect() for db in connections))
            self.assertEqual((await row(connections[0],'SELECT count(*) AS n FROM schema_migrations'))['n'],2)
            sid=await connections[0].start_session(42)
        finally:
            for db in connections:await db.close()
        reopened=Database(path);await reopened.connect()
        try:self.assertEqual((await project(reopened,sid))['origin'],'chat')
        finally:await reopened.close()

    async def test_streamed_body_limit_and_upload_auth_before_parsing(self):
        self.client.cookies.clear()
        r=await self.client.post('/api/v1/projects/1/uploads?kind=source&revision=0',content=b'invalid multipart')
        self.assertEqual(r.status_code,401)
        await self.client.post('/api/v1/auth/dev')
        async def body():
            for _ in range(22):yield b'x'*1_000_000
        r=await self.client.post('/api/v1/projects',content=body(),headers={'content-type':'application/json'})
        self.assertEqual(r.status_code,413,r.text)

    async def test_invitation_isolated_persistent_account_and_revocation(self):
        from bot.web.invitations import create_invitation, revoke_invitation
        self.settings.invites_enabled=True
        owner=await self.create()
        invitation=await create_invitation(self.db,'Друг',48)
        self.assertLess(invitation['user_id'],0)
        stored=await row(self.db,'SELECT * FROM test_invitations WHERE id=?',(invitation['id'],))
        self.assertNotIn(invitation['token'],str(stored))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('192.0.2.10',123)),base_url='https://pilot.example') as guest:
            r=await guest.post('/api/v1/auth/invite',json={'token':invitation['token']})
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn('Secure',r.headers['set-cookie']);self.assertIn('HttpOnly',r.headers['set-cookie'])
            me=(await guest.get('/api/v1/me')).json()
            self.assertTrue(me['is_guest']);self.assertEqual(me['name'],'Друг')
            self.assertEqual((await guest.get('/api/v1/projects')).json(),[])
            self.assertEqual((await guest.get(f'/api/v1/projects/{owner["id"]}')).status_code,404)
            self.assertEqual((await guest.delete(f'/api/v1/projects/{owner["id"]}')).status_code,404)
            p=(await guest.post('/api/v1/projects',json={})).json()
            self.assertIsNotNone(await self.db.get_artifact(p['id'],'notification_disabled'))
            self.assertEqual((await self.client.get(f'/api/v1/projects/{p["id"]}')).status_code,404)
            r=await guest.post('/api/v1/auth/invite',json={'token':invitation['token']})
            self.assertEqual(r.status_code,200)
            self.assertEqual((await guest.get('/api/v1/projects')).json()[0]['id'],p['id'])
            r=await guest.post(f'/api/v1/projects/{p["id"]}/commands',json={'revision':p['revision'],'action':'analyze','description':'Тест друга'})
            self.assertEqual(r.status_code,200,r.text)
            await revoke_invitation(self.db,invitation['id'])
            self.assertEqual((await guest.get('/api/v1/projects')).status_code,401)
            self.assertEqual((await guest.post('/api/v1/auth/invite',json={'token':invitation['token']})).status_code,401)
            self.assertEqual((await latest_job(self.db,p['id']))['status'],'cancelled')
            self.assertEqual((await project(self.db,owner['id']))['status'],'in_progress')

    async def test_invitation_disabled_expired_invalid_and_forged_account(self):
        from bot.web.invitations import create_invitation
        invitation=await create_invitation(self.db)
        other=await create_invitation(self.db)
        self.assertNotEqual(invitation['user_id'],other['user_id'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://pilot.example') as guest:
            self.assertEqual((await guest.post('/api/v1/auth/invite',json={'token':invitation['token']})).status_code,403)
            self.settings.invites_enabled=True
            self.assertEqual((await guest.post('/api/v1/auth/invite',json={'token':'x'*43})).status_code,401)
            forged=issue_session(other['user_id'],self.settings,invitation=invitation)
            guest.cookies.set('powerguide_session',forged)
            self.assertEqual((await guest.get('/api/v1/projects')).status_code,401)
            guest.cookies.clear()
            await self.db.conn.execute('UPDATE test_invitations SET expires_at=1 WHERE id=?',(invitation['id'],));await self.db.commit()
            self.assertEqual((await guest.post('/api/v1/auth/invite',json={'token':invitation['token']})).status_code,401)


if __name__=='__main__':unittest.main()
