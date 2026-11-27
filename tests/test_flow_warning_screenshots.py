import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / 'packaging'
sys.path[:0] = [str(ROOT / name) for name in ('windows', 'ubuntu/snapshot/workbench',
                                            'ubuntu/snapshot/flow/source/src')]
from flow_warning_archive import current_window_screenshots, purge_reviewed_screenshots, read_png, records, sync_index, view_screenshot_once
from flow_warning_desktop import capture_name, sync_once
from foreground_input import InputNotSubmitted

try:
    from studiowright.studio import ImageRequest, VideoRequest, Studio, FLOW_ROOT, PlaywrightTimeout, SubmissionRejectedUnusual
    from studiowright.store import Store
except ImportError:
    Studio = None  # The console backend dependencies are installed on Ubuntu.


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMuoAAAAASUVORK5CYII='
)
JOB_ID = '123456789abc'


@unittest.skipIf(Studio is None, 'Flow console runtime unavailable')
class Capture(unittest.IsolatedAsyncioTestCase):
    async def test_local_flow_input_timeout_records_error_without_screenshot(self):
        job = {'project_id': 'project-a', 'params': '{"origin_console_task":"task-a","count":1}',
               'result': '{"stage":"preparing","media_ids":[]}'}
        studio = Studio.__new__(Studio)
        studio.store = SimpleNamespace(job=Mock(return_value=job), update_job=Mock())
        studio._capture_failure_page = AsyncMock()
        await studio._fail(JOB_ID, InputNotSubmitted('prompt_paste_timeout'), FLOW_ROOT)
        studio._capture_failure_page.assert_not_awaited()
        saved = studio.store.update_job.call_args.kwargs
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['error'], 'InputNotSubmitted: prompt_paste_timeout')
        self.assertNotIn('warning_screenshot', saved['result'])

    async def test_generation_saves_failure_before_cooldown_stops_chrome(self):
        for request, screenshot_available in ((ImageRequest('test', project='project-a'), True),
                                               (VideoRequest('test', project='project-a'), True),
                                               (ImageRequest('test', project='project-a'), False)):
            with self.subTest(request=type(request).__name__, screenshot=screenshot_available), \
                 tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict('os.environ', {'STUDIOWRIGHT_HOME': str(root)}):
                    store = Store()
                    studio = Studio('capture-test', store)
                    studio.console_task_id = 'task-a'
                    page = SimpleNamespace(url=FLOW_ROOT + 'project/project-a', is_closed=Mock(return_value=False))

                    async def screenshot(**_):
                        self.assertFalse(page.is_closed(), 'Chrome stopped before the screenshot')
                        if not screenshot_available:
                            raise OSError('capture unavailable')
                        return PNG

                    page.screenshot = AsyncMock(side_effect=screenshot)

                    async def reject(*_, on_submit, **__):
                        on_submit()
                        raise SubmissionRejectedUnusual('PUBLIC_ERROR_UNUSUAL_ACTIVITY')

                    flow = SimpleNamespace(page=page, reset=AsyncMock(), apply_image_options=AsyncMock(),
                                           apply_video_options=AsyncMock(return_value=None),
                                           generate_images=reject, generate_video=reject)
                    studio._generation_page = AsyncMock(return_value=(None, flow))

                    def stop_chrome(args, **_):
                        self.assertEqual(args, ['systemctl', '--user', 'stop', 'studiowright-browser-capture-test.service'])
                        page.is_closed.return_value = True
                        with store.connect() as db:
                            job = db.execute('SELECT * FROM jobs').fetchone()
                        self.assertEqual(job['status'], 'failed')
                        result = json.loads(job['result'])
                        self.assertEqual(result['retry_evidence'], 'provider_rejected_unusual')
                        if screenshot_available:
                            self.assertEqual((root / 'incidents' / result['warning_screenshot']).read_bytes(), PNG)
                        else:
                            self.assertNotIn('warning_screenshot', result)

                    generate = studio.image if isinstance(request, ImageRequest) else studio.video
                    with patch('subprocess.run', side_effect=stop_chrome) as stop:
                        with self.assertRaisesRegex(SubmissionRejectedUnusual, 'PUBLIC_ERROR_UNUSUAL_ACTIVITY'):
                            await generate(request)
                    page.screenshot.assert_awaited_once()
                    stop.assert_called_once()
                    self.assertEqual(studio.governor.status()['refusals'], 1)

    async def test_failure_uses_the_active_project_page_and_preserves_retry_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {'project_id': 'project-a', 'params': '{"origin_console_task":"task-a","count":1}',
                   'result': '{"stage":"submitting","media_ids":[]}'}
            studio = Studio.__new__(Studio)
            studio.store = SimpleNamespace(job=Mock(return_value=job), update_job=Mock())
            studio.governor = Mock()
            page = SimpleNamespace(url=FLOW_ROOT + 'project/project-a', screenshot=AsyncMock(return_value=PNG))
            error = SubmissionRejectedUnusual('PUBLIC_ERROR_UNUSUAL_ACTIVITY')
            with patch('studiowright.studio.paths.incidents_dir', return_value=Path(directory)):
                await studio._fail(JOB_ID, error, FLOW_ROOT, page=page)
            self.assertEqual((Path(directory) / (JOB_ID + '.png')).read_bytes(), PNG)
            result = studio.store.update_job.call_args.kwargs['result']
            self.assertEqual(result['warning_screenshot'], JOB_ID + '.png')
            self.assertEqual(result['retry_evidence'], 'provider_rejected_unusual')

            page.url = FLOW_ROOT + 'project/project-b'
            page.screenshot.reset_mock()
            await studio._fail(JOB_ID, error, FLOW_ROOT, page=page)
            page.screenshot.assert_not_called()
            self.assertNotIn('warning_screenshot', studio.store.update_job.call_args.kwargs['result'])

    async def test_screenshot_timeout_falls_back_to_the_same_tab(self):
        studio = Studio.__new__(Studio)
        capture = SimpleNamespace(
            send=AsyncMock(return_value={'data': base64.b64encode(PNG).decode()}),
            detach=AsyncMock(),
        )
        page = SimpleNamespace(url=FLOW_ROOT + 'project/project-a',
                               screenshot=AsyncMock(side_effect=PlaywrightTimeout('capture timed out')),
                               is_closed=Mock(return_value=False),
                               context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=capture)))
        self.assertEqual(await studio._incident_png(page, '/project/project-a'), PNG)
        page.context.new_cdp_session.assert_awaited_once_with(page)
        capture.detach.assert_awaited_once()

        page.url = FLOW_ROOT + 'project/project-b'
        page.context.new_cdp_session.reset_mock()
        with self.assertRaises(PlaywrightTimeout):
            await studio._incident_png(page, '/project/project-a')
        page.context.new_cdp_session.assert_not_awaited()


class Delivery(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.incidents = self.root / 'data/incidents'
        self.incidents.mkdir(parents=True)
        (self.root / 'console-data').mkdir()
        self.desktop = self.root / 'desktop'
        self.desktop.mkdir()
        self.state = self.enterContext(closing(sqlite3.connect(':memory:')))
        self.jobs = self.enterContext(closing(sqlite3.connect(self.root / 'data/studiowright.db')))
        self.review_database = self.root / 'reviews.db'
        self.review = self.enterContext(closing(sqlite3.connect(self.review_database)))
        self.finished = 1795249440
        with closing(sqlite3.connect(self.root / 'console-data/console.db')) as db, db:
            db.execute('CREATE TABLE accounts(profile TEXT, email TEXT)')
            db.execute("INSERT INTO accounts VALUES('profile-a','new@example.test')")
        with self.jobs:
            self.jobs.execute('CREATE TABLE jobs(id TEXT, profile TEXT, kind TEXT, status TEXT, finished REAL, error TEXT, params TEXT, result TEXT)')
        with self.review:
            self.review.execute('CREATE TABLE events(profile TEXT, task_id TEXT, action TEXT, created REAL)')

    def capture(self, ident, *, profile='profile-a', marked=True, later=0):
        with self.jobs:
            self.jobs.execute("INSERT INTO jobs VALUES(?,?,'image','failed',?,?,?,?)",
                              (ident, profile, self.finished + later, 'PUBLIC_ERROR_UNUSUAL_ACTIVITY',
                               '{"origin_console_task":"task-a"}',
                               json.dumps({'warning_screenshot': ident + '.png'} if marked else {})))
        screenshot = self.incidents / (ident + '.png')
        screenshot.write_bytes(PNG)
        return screenshot

    def read(self, action, ident=None):
        if action == 'sync':
            return json.dumps(sync_index(self.root, review_database=self.review_database)).encode()
        return read_png(self.root, ident, review_database=self.review_database)

    def test_only_new_recorded_screenshots_are_synced_once(self):
        active = self.capture(JOB_ID)
        self.capture('0123456789ab', marked=False)
        windows = {'windows': [
            {'profile': 'profile-a', 'task_id': 'task-a'},
            {'profile': 'profile-b', 'task_id': 'task-a'},
        ]}
        self.assertEqual(current_window_screenshots(windows, self.root, review_database=self.review_database),
                         {'profile-a': {'id': JOB_ID, 'task_id': 'task-a'}})
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (1, 0, 0))
        saved = self.desktop / capture_name(next(records(self.root, review_database=self.review_database))[0])
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertIn('new_异常活动_task-a', saved.name)
        active.unlink()
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 0, 0))
        self.assertTrue(saved.is_file())  # A missing source without a review is not a deletion signal.
        saved.unlink()
        active.write_bytes(PNG)
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 0, 0))
        self.assertEqual(list(self.desktop.iterdir()), [])

    def test_only_confirmed_review_for_this_account_and_warning_removes_both_copies(self):
        warning = self.capture(JOB_ID)
        other_account = self.capture('aaaaaaaaaaaa', profile='profile-b')
        later_warning = self.capture('bbbbbbbbbbbb', later=20)
        history = self.capture('cccccccccccc', marked=False)
        initial = {row['id']: self.desktop / capture_name(row)
                   for row, _ in records(self.root, review_database=self.review_database)}
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (3, 0, 0))
        saved = initial[JOB_ID]
        personal_copy = self.desktop / 'saved-for-my-notes.png'
        personal_copy.write_bytes(PNG)
        with self.review:
            self.review.executemany('INSERT INTO events VALUES(?,?,?,?)', [
                ('profile-a', 'task-a', 'confirmed_rejection_released', self.finished + 3),
                ('profile-a', 'other-task', 'readiness_hold_reviewed_by_operator', self.finished + 4),
                ('profile-a', 'task-a', 'readiness_hold_reviewed_by_operator', self.finished - 1),
            ])
        self.assertEqual(self.read('read', JOB_ID), PNG)
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 0, 0))
        self.assertTrue(warning.is_file() and saved.is_file())

        with self.review:
            self.review.execute('INSERT INTO events VALUES(?,?,?,?)',
                                ('profile-a', 'task-a', 'readiness_hold_reviewed_by_operator', self.finished + 5))
        with patch('flow_warning_archive.FLOW_ROOT', self.root), \
             patch('flow_warning_archive.REVIEW_DATABASE', self.review_database):
            purge_reviewed_screenshots('profile-a', 'task-a')
        self.assertFalse(warning.exists())
        with self.assertRaises(FileNotFoundError):
            self.read('read', JOB_ID)
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 1, 0))
        self.assertFalse(saved.exists())
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 0, 0))
        self.assertTrue(all(path.is_file() for path in (other_account, later_warning, history, personal_copy,
                                                       initial['aaaaaaaaaaaa'], initial['bbbbbbbbbbbb'])))

    def test_destroy_exact_image_without_reviewing_task_or_removing_other_screenshots(self):
        source = self.capture(JOB_ID)
        unrelated = self.capture('aaaaaaaaaaaa', later=-1)
        synced = self.desktop / capture_name(next(
            row for row, _ in records(self.root, review_database=self.review_database) if row['id'] == JOB_ID))
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (2, 0, 0))
        self.assertEqual(self.read('read', JOB_ID), PNG)
        self.assertTrue(source.exists() and synced.exists())
        with closing(sqlite3.connect(self.root / 'console-data/console.db')) as db, db:
            db.execute("UPDATE accounts SET email='renamed@example.test'")

        self.assertEqual(view_screenshot_once(self.root, JOB_ID, review_database=self.review_database), PNG)
        with self.assertRaises(FileNotFoundError):
            view_screenshot_once(self.root, JOB_ID, review_database=self.review_database)
        self.assertFalse(source.exists())
        with self.assertRaises(FileNotFoundError):
            self.read('read', JOB_ID)
        self.assertEqual(sync_once(self.read, self.desktop, self.state), (0, 1, 0))
        self.assertFalse(synced.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.jobs.execute('SELECT status FROM jobs WHERE id=?', (JOB_ID,)).fetchone(), ('failed',))
        self.assertEqual(self.review.execute('SELECT count(*) FROM events').fetchone(), (0,))
        windows = {'windows': [{'profile': 'profile-a', 'task_id': 'task-a'}]}
        self.assertEqual(current_window_screenshots(windows, self.root, review_database=self.review_database),
                         {'profile-a': {'task_id': 'task-a', 'destroyed': True}})


if __name__ == '__main__':
    unittest.main()
