import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage
from pptx import Presentation
from bot.db import Database
from bot.export import build_editable_pptx
from bot.jobs import SessionJobs, session_locks
from bot.llm import Image, LLMError
from bot.pipeline import Pipeline, validate_descriptions, STYLE, PLAN, DESCRIPTIONS, slide_kind
from bot.user_images import UserImage

PLAN_TEXT = '\n\n'.join(
    f'СЛАЙД {n}\nЗАГОЛОВОК: Заголовок {n}\nТИП: текст\nСОДЕРЖАНИЕ: Доход {n},5 млн рублей'
    for n in range(1, 4)
)


def descriptions(numbers):
    return '\n\n'.join(f'Изображение {n}\nТекст на изображении: «Доход {n},5 млн рублей»' for n in numbers)


class DescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_missing_extra_duplicate_reordered_and_empty(self):
        for numbers in ([1], [1, 2, 3, 4], [1, 2, 2, 3], [2, 1, 3], []):
            with self.subTest(numbers=numbers), self.assertRaises(LLMError):
                validate_descriptions(PLAN_TEXT, descriptions(numbers))
        with self.assertRaises(LLMError):
            validate_descriptions(PLAN_TEXT, 'Изображение 1\nИзображение 2\nИзображение 3')

    async def test_reject_before_returning_to_cache(self):
        pipeline = Pipeline(SimpleNamespace(generate=AsyncMock(return_value=(descriptions([1]), 'fake'))), None, None, Path('.'), 6)
        with patch('bot.pipeline.load_prompt', return_value='test'), self.assertRaises(LLMError):
            await pipeline.descriptions(PLAN_TEXT)

    async def test_complete_response(self):
        self.assertEqual(list(validate_descriptions(PLAN_TEXT, descriptions([1, 2, 3]))), [1, 2, 3])


class DatabaseFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name)/'test.db')
        await self.db.connect()
        self.session = await self.db.start_session(42)

    async def asyncTearDown(self):
        await self.db.close()
        self.folder.cleanup()


class DatabaseTests(DatabaseFixture):
    async def test_cancelled_session_cannot_be_completed(self):
        await self.db.finish_session(self.session, 'cancelled')
        await self.db.finish_session(self.session)
        async with self.db.conn.execute('SELECT status FROM sessions WHERE id=?', (self.session,)) as cursor:
            self.assertEqual((await cursor.fetchone())['status'], 'cancelled')

    async def test_cancel_stops_inflight_task_and_next_request(self):
        jobs = SessionJobs()
        started = asyncio.Event()
        next_request = AsyncMock()

        @jobs.handle_cancellation
        async def work(message, db, session_id):
            jobs.add(session_id)
            try:
                started.set()
                await asyncio.Event().wait()
                await next_request()
            finally:
                jobs.discard(session_id)

        task = asyncio.create_task(work(AsyncMock(), self.db, self.session))
        await started.wait()
        await self.db.finish_session(self.session, 'cancelled')
        await jobs.cancel(self.session)
        self.assertTrue(task.done())
        next_request.assert_not_awaited()
        self.assertNotIn(self.session, jobs)

    async def test_restart_keeps_progress(self):
        await self.db.save_artifact(self.session, 'slide_01', '/slide.png')
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.get_artifact(self.session, 'slide_01'), '/slide.png')
        self.assertTrue(await self.db.is_active(self.session))

    async def test_failed_stage_can_retry(self):
        jobs = SessionJobs()
        message = AsyncMock()
        calls = 0

        @jobs.handle_cancellation
        async def work(message, db, session_id):
            nonlocal calls
            jobs.add(session_id)
            try:
                calls += 1
                if calls == 1:
                    raise OSError('temporary failure')
                await db.save_artifact(session_id, 'result', 'done')
            finally:
                jobs.discard(session_id)

        await work(message, self.db, self.session)
        self.assertNotIn(self.session, jobs)
        await work(message, self.db, self.session)
        self.assertEqual(await self.db.get_artifact(self.session, 'result'), 'done')

    async def test_done_waits_for_file_and_reads_its_result(self):
        from bot import handlers
        await self.db.set_waiting_for(self.session, handlers.WAIT_SOURCES)
        acquired, release = asyncio.Event(), asyncio.Event()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=AsyncMock(), answer=AsyncMock())

        async def upload():
            async with handlers._source_files_locks[self.session]:
                acquired.set()
                await release.wait()
                await self.db.save_artifact(self.session, 'uploaded', 'yes')

        async def analyze(*args):
            self.assertEqual(await self.db.get_artifact(self.session, 'uploaded'), 'yes')

        task = asyncio.create_task(upload())
        await acquired.wait()
        with patch.object(handlers, 'run_analysis', AsyncMock(side_effect=analyze)) as analysis:
            done = asyncio.create_task(handlers.on_sources_done(callback, self.db, None, None))
            await asyncio.sleep(0.01)
            analysis.assert_not_awaited()
            release.set()
            await asyncio.gather(task, done)
            analysis.assert_awaited_once()

    async def test_finished_slides_are_reused(self):
        from bot.generation import draw_slides
        await self.db.save_artifact(self.session, STYLE, 'test')
        await self.db.save_artifact(self.session, slide_kind(1), '/existing.png')
        pipeline = SimpleNamespace(slide=AsyncMock())
        failed = await draw_slides(AsyncMock(), self.db, pipeline, Path(self.folder.name), self.session, {1:'description'}, '16:9', Image(b'', 'image/png'))
        self.assertEqual(failed, [])
        pipeline.slide.assert_not_awaited()


class ExportTests(unittest.TestCase):
    def test_native_editable_text_preserves_numbers_and_pictures(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'photo.png'
            PILImage.new('RGB', (200, 100), 'red').save(path)
            result = build_editable_pptx(PLAN_TEXT, '16:9', {2:UserImage(str(path), 200, 100, 'Фото')})
            deck = Presentation(io.BytesIO(result))
        self.assertEqual(len(deck.slides), 3)
        self.assertAlmostEqual(deck.slide_width/deck.slide_height, 16/9, places=4)
        for n, slide in enumerate(deck.slides, 1):
            text = '\n'.join(shape.text for shape in slide.shapes if shape.has_text_frame)
            self.assertIn(f'Доход {n},5 млн рублей', text)
        self.assertTrue(any(shape.shape_type == 13 for shape in deck.slides[1].shapes))
        self.assertFalse(any(shape.shape_type == 13 for shape in deck.slides[0].shapes))




class TextAccuracyTests(unittest.TestCase):
    def test_decimal_point_cannot_disappear(self):
        from bot.textcheck import compare
        self.assertTrue(compare('Цена 1.5 млн', 'Цена 15 млн'))
        self.assertTrue(compare('Доход 1,5 млн', 'Доход 15 млн'))

    def test_hyphenated_word_still_matches(self):
        from bot.textcheck import compare
        self.assertEqual(compare('кон-\nтроль', 'контроль'), [])


class TaskOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_does_not_start_second_request(self):
        jobs = SessionJobs()
        started, release = asyncio.Event(), asyncio.Event()
        message = AsyncMock()
        db = SimpleNamespace(is_active=AsyncMock(return_value=True))
        calls = []

        @jobs.handle_cancellation
        async def work(message, db, session_id):
            calls.append(session_id)
            started.set()
            await release.wait()

        first = asyncio.create_task(work(message, db, 1))
        await started.wait()
        await work(message, db, 1)
        self.assertEqual(calls, [1])
        release.set()
        await first
        self.assertNotIn(1, jobs)

    async def test_nested_stages_remain_owned_until_finished(self):
        jobs = SessionJobs()
        db = SimpleNamespace(is_active=AsyncMock(return_value=True))
        message = AsyncMock()

        @jobs.handle_cancellation
        async def inner(message, db, session_id):
            self.assertIn(session_id, jobs)

        @jobs.handle_cancellation
        async def outer(message, db, session_id):
            await inner(message, db, session_id)
            self.assertIn(session_id, jobs)

        await outer(message, db, 1)
        self.assertNotIn(1, jobs)

    async def test_shutdown_cancellation_propagates(self):
        jobs = SessionJobs()
        db = SimpleNamespace(is_active=AsyncMock(return_value=True))

        @jobs.handle_cancellation
        async def work(message, db, session_id):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await work(AsyncMock(), db, 1)
        self.assertNotIn(1, jobs)

    async def test_other_users_do_not_share_file_lock(self):
        locks = session_locks()
        async with locks[1]:
            async with asyncio.timeout(1):
                async with locks[2]:
                    pass


class HandlerIntegrationTests(DatabaseFixture):
    async def test_new_presentation_cancels_real_analysis(self):
        from bot import handlers
        started = asyncio.Event()
        stopped = asyncio.Event()
        message = AsyncMock()

        async def analyze(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        pipeline = SimpleNamespace(analyze_survey=analyze)
        bank = SimpleNamespace(load=lambda: SimpleNamespace(questions=[]))
        task = asyncio.create_task(handlers.run_analysis(message, self.db, bank, pipeline, self.session))
        await started.wait()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=AsyncMock(), answer=AsyncMock())
        await handlers.on_new_presentation(callback, self.db, bank)
        self.assertTrue(stopped.is_set())
        self.assertTrue(task.done())
        new = await self.db.get_active_session(42)
        self.assertNotEqual(new, self.session)
        self.assertEqual(await self.db.get_waiting_for(new), handlers.WAIT_DESCRIPTION)
        self.assertEqual(await self.db.get_artifacts(self.session), {})

    async def test_full_generation_delivers_pdf_and_native_pptx(self):
        from unittest.mock import Mock
        from bot import generation
        from bot.pipeline import BACKGROUND
        path = Path(self.folder.name)/'background.png'
        PILImage.new('RGB', (320, 180), 'white').save(path)
        await self.db.save_artifact(self.session, BACKGROUND, str(path))
        await self.db.save_artifact(self.session, STYLE, 'simple')
        await self.db.save_artifact(self.session, PLAN.kind, PLAN_TEXT)
        await self.db.save_artifact(self.session, DESCRIPTIONS, descriptions([1, 2, 3]))
        pipeline = SimpleNamespace(
            slide_prompt=Mock(return_value='slide prompt'),
            slide=AsyncMock(return_value=(Image(path.read_bytes(), 'image/png'), 'fake')),
            check_slide_text=AsyncMock(return_value=([], 'fake')),
        )
        message = AsyncMock()
        await generation.run_slides(message, self.db, pipeline, Path(self.folder.name), self.session)
        documents = [call.args[0] for call in message.answer_document.await_args_list]
        self.assertEqual([file.filename for file in documents], ['Презентация.pdf', 'Презентация_редактируемая.pptx'])
        self.assertTrue(documents[0].data.startswith(b'%PDF'))
        self.assertEqual(len(Presentation(io.BytesIO(documents[1].data)).slides), 3)
        self.assertFalse(await self.db.is_active(self.session))
        self.assertEqual(pipeline.slide.await_count, 3)

    async def test_real_upload_is_included_before_done(self):
        from bot import handlers
        await self.db.set_waiting_for(self.session, handlers.WAIT_SOURCES)
        downloading, release = asyncio.Event(), asyncio.Event()
        message = AsyncMock()

        async def download(*args):
            downloading.set()
            await release.wait()
            return io.BytesIO('Материалы заказчика'.encode())

        message.bot.download.side_effect = download
        task = asyncio.create_task(handlers.receive_source_file(
            message, self.db, Path(self.folder.name), self.session,
            SimpleNamespace(file_size=40), 'data.txt', 'text/plain',
        ))
        await downloading.wait()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=42), message=AsyncMock(), answer=AsyncMock())

        async def analysis(*args):
            sources = await self.db.get_source_files(self.session)
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].text, 'Материалы заказчика')

        with patch.object(handlers, 'run_analysis', AsyncMock(side_effect=analysis)) as analyze:
            done = asyncio.create_task(handlers.on_sources_done(callback, self.db, None, None))
            await asyncio.sleep(0.01)
            analyze.assert_not_awaited()
            release.set()
            await asyncio.gather(task, done)
            analyze.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
