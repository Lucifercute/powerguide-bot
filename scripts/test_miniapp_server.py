"""Ephemeral browser-test server. A real worker runs deterministic TEST model fakes.

No Google requests, Telegram messages or real user materials are involved.
"""
from pathlib import Path
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.chdir(ROOT)

if __name__ == '__main__':
    import uvicorn
    import asyncio
    import io
    from contextlib import asynccontextmanager
    from PIL import Image as PILImage
    from bot.llm import Image
    from bot.questions import QuestionBank
    from bot.web.worker import Worker
    from bot.web.api import create_app
    from bot.web.settings import WebSettings
    with tempfile.TemporaryDirectory(prefix='powerguide-ui-test-') as folder:
        root = Path(folder)
        settings = WebSettings(db_path=root/'test.db', files_dir=root/'sessions',
                               presets_dir=root/'presets', dev_auth=True, secret='browser-test-secret-xxxxxxxxxxxxxxxx',
                               editable_enabled=True, standard_enabled=False, invites_enabled=True)
        sys.path.insert(0,str(ROOT/'tests'))
        from test_miniapp import fake_pipeline
        raw=io.BytesIO();PILImage.new('RGB',(320,180),'white').save(raw,format='PNG')
        pipeline=fake_pipeline(Image(raw.getvalue(),'image/png'))

        async def editable(db,sid,folder,status):
            # Deterministic stand-in for the editable PPTX service (same mode as production).
            await status('Собираю PowerPoint и проверяю вёрстку…')
            folder=folder/str(sid)/'editable';folder.mkdir(parents=True,exist_ok=True)
            html=folder/'presentation.html';pptx=folder/'presentation.pptx'
            html.write_text('<html><body><section class="slide">ТЕСТ</section></body></html>')
            pptx.write_bytes(b'PK\x03\x04 TEST')
            await db.save_artifact(sid,'editable_html',str(html));await db.save_artifact(sid,'editable_pptx',str(pptx))
            return html,pptx
        pipeline.editable_service.generate.side_effect=editable
        app=create_app(settings)
        original_lifespan=app.router.lifespan_context

        # Test-only fixtures: these endpoints do not exist in the production API.
        @app.get('/_test/invitation')
        async def test_invitation():
            return app.state.browser_invitation
        app.router.routes.insert(0, app.router.routes.pop())  # before the SPA static mount

        @asynccontextmanager
        async def lifespan(app):
            async with original_lifespan(app):
                from bot.db import Database
                from bot.web.invitations import create_invitation
                db=Database(settings.db_path);await db.connect(migrate=False)
                try:app.state.browser_invitation=await create_invitation(db,'Друг — тест')
                finally:await db.close()
                worker=Worker(settings,pipeline,QuestionBank(settings.questions_path))
                task=asyncio.create_task(worker.run())
                try:yield
                finally:
                    worker.stopping.set()
                    await task

        app.router.lifespan_context=lifespan
        uvicorn.run(app,host='127.0.0.1',port=8765,access_log=False,proxy_headers=False)
