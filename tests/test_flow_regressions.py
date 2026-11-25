"""Exercise the packaged API implementation without a provider connection."""
import sys
import shutil
from contextlib import contextmanager
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from live_pool import Conflict, RealPool
from foreground_input import (
    FLOW_PREFIX_CHARACTERS, FLOW_PROMPT_TEXT, FLOW_SELECT_PREFIX,
    InputNotSubmitted, Target, paste_and_submit_reserved,
)


@contextmanager
def fixture_directory():
    parent = Path(__file__).resolve().parent
    root = parent / ('.fixture-' + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        assert root.resolve().parent == parent and root.name.startswith('.fixture-')
        shutil.rmtree(root)


class FlowPromptVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prompt = '第一行\n第二行\n\n第四行'
        self.editor = ''
        self.actions = []
        self.driver = SimpleNamespace(
            composer_text=AsyncMock(side_effect=lambda *_: self.editor),
            activate_and_confirm=AsyncMock(return_value=True),
            composer_ready_cdp=AsyncMock(return_value=True), confirm_focus=AsyncMock(return_value=True),
            copy_prompt_and_paste=AsyncMock(side_effect=self.paste),
        )
        self.composer = SimpleNamespace(evaluate=AsyncMock(side_effect=self.evaluate_composer))
        self.page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=lambda _, prompt: list(prompt[:5])),
            keyboard=SimpleNamespace(
                insert_text=AsyncMock(side_effect=self.insert), press=AsyncMock(side_effect=self.delete),
            ),
        )
        self.checkpoint = AsyncMock(side_effect=lambda: self.actions.append(('checkpoint', self.editor)))
        self.send = AsyncMock(side_effect=lambda: self.actions.append(('send', self.editor)))

    def insert(self, character):
        self.editor += character
        self.actions.append(('insert', character))

    def delete(self, key):
        self.assertEqual(key, 'Backspace')
        self.actions.append(('delete', self.editor))
        self.editor = ''

    def paste(self, *args):
        text = args[-1]
        self.editor += text
        self.actions.append(('paste', text))

    def evaluate_composer(self, script, expected=None):
        if script == FLOW_SELECT_PREFIX:
            return self.editor == expected
        self.assertEqual(script, FLOW_PROMPT_TEXT)
        return self.editor

    async def submit(self, prefix_length=2):
        with (
            fixture_directory() as directory,
            patch('foreground_input.Path.home', return_value=directory),
            patch('foreground_input._pacing_wait', new_callable=AsyncMock),
            patch('foreground_input.random.randint', return_value=prefix_length),
            patch('pointer_sequence.record_input_action'),
        ):
            await paste_and_submit_reserved(
                driver=self.driver, page=self.page, composer=self.composer,
                target=Target('fixture', 1, 1, 0), prompt=self.prompt,
                checkpoint=self.checkpoint, submit=self.send, hybrid=True,
            )

    async def test_flow_compares_the_paragraph_text_before_sending(self):
        await self.submit()
        self.page.evaluate.assert_awaited_once_with(FLOW_PREFIX_CHARACTERS, self.prompt)
        self.driver.copy_prompt_and_paste.assert_awaited_once()
        self.assertEqual(self.actions, [
            ('insert', '第'), ('insert', '一'), ('delete', '第一'), ('paste', self.prompt),
            ('checkpoint', self.prompt), ('send', self.prompt),
        ])

    async def test_incorrect_text_keeps_a_flow_task_before_submission(self):
        self.driver.copy_prompt_and_paste.side_effect = lambda *_: self.paste(self.prompt + '!')
        with patch('foreground_input.FLOW_PROMPT_TIMEOUT', .02):
            with self.assertRaisesRegex(InputNotSubmitted, '^prompt_input_timeout$'):
                await self.submit()
        self.checkpoint.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_long_prompt_deletes_only_the_first_five_characters(self):
        self.prompt = 'ABCDE' + 'x' * 267
        await self.submit(prefix_length=5)
        self.driver.copy_prompt_and_paste.assert_awaited_once()
        self.assertEqual(self.actions, [
            *(('insert', char) for char in 'ABCDE'), ('delete', 'ABCDE'), ('paste', self.prompt),
            ('checkpoint', self.prompt), ('send', self.prompt),
        ])

    async def test_one_character_prompt_is_pasted_without_deletion(self):
        self.prompt = '字'
        await self.submit()
        self.assertEqual(self.actions, [('paste', '字'), ('checkpoint', '字'), ('send', '字')])

    async def test_prefix_with_a_line_break_uses_temporary_paste(self):
        self.prompt = 'A\nB'
        await self.submit()
        self.assertEqual(self.actions, [
            ('paste', 'A\n'), ('delete', 'A\n'), ('paste', self.prompt),
            ('checkpoint', self.prompt), ('send', self.prompt),
        ])

    async def test_prefix_can_contain_a_multi_codepoint_character(self):
        prefix = '👩‍💻é'
        self.prompt = prefix + '之后的提示词'
        self.page.evaluate.side_effect = None
        self.page.evaluate.return_value = ['👩‍💻', 'é', '之', '后', '的']
        await self.submit()
        self.assertEqual(self.actions[:3], [('insert', '👩‍💻'), ('insert', 'é'), ('delete', prefix)])

    async def test_changed_prefix_is_never_deleted(self):
        def evaluate(script, expected=None):
            if script == FLOW_SELECT_PREFIX:
                self.editor += '意外草稿'
            return self.evaluate_composer(script, expected)

        self.composer.evaluate.side_effect = evaluate
        with self.assertRaisesRegex(InputNotSubmitted, '^flow_prefix_changed_before_deletion$'):
            await self.submit()
        self.page.keyboard.press.assert_not_awaited()
        self.driver.copy_prompt_and_paste.assert_not_awaited()
        self.send.assert_not_awaited()


class ImageCreditCompatibility(unittest.TestCase):
    def test_optional_credit_field_and_duplicate_submission(self):
        with fixture_directory() as directory:
            root = Path(directory)
            pool = RealPool(root / 'state', root, browser_inventory=lambda: None)
            project = str(uuid.uuid4())
            snapshot = {
                'projects': [{'key': 'project-key', 'source_profile': 'account-1', 'source_id': project}],
                'real_accounts': {'items': [{'profile': 'account-1', 'flow_login': 'verified'}]},
                'flow_models': {'image': [{'id': 'nano-banana-2.1'}]},
            }
            try:
                for credits in ('omitted', None, 0):
                    with self.subTest(credits=credits):
                        params = dict(prompt='Local fixture only', model='nano-banana-2.1',
                                      aspect='16:9', count=1, resolution='360p', duration=4)
                        if credits != 'omitted':
                            params['max_credits'] = credits
                        data = dict(project_key='project-key', kind='image', confirm=True, params=params)
                        calls = []
                        def send(body):
                            calls.append(body)
                            return dict(id='fixture-task', profile='account-1', project=project, kind='image')
                        rid = str(uuid.uuid4())
                        first, created = pool.submit_flow(data, rid, snapshot, send)
                        again, repeated = pool.submit_flow(data, rid, snapshot, send)
                        self.assertTrue(created)
                        self.assertFalse(repeated)
                        self.assertEqual(first['task_id'], again['task_id'])
                        self.assertEqual(first['status'], 'submitted')
                        self.assertEqual(len(calls), 1)
                        self.assertNotIn('max_credits', calls[0]['params'])
            finally:
                pool.close()

    def test_explicit_single_attempt_is_forwarded_and_bound_to_the_request_id(self):
        with fixture_directory() as directory:
            root = Path(directory)
            pool = RealPool(root / 'state', root, browser_inventory=lambda: None)
            project = str(uuid.uuid4())
            snapshot = {
                'projects': [{'key': 'project-key', 'source_profile': 'account-1', 'source_id': project}],
                'real_accounts': {'items': [{'profile': 'account-1', 'flow_login': 'verified'}]},
                'flow_models': {'image': [{'id': 'nano-banana-2.1'}]},
            }
            data = dict(project_key='project-key', kind='image', confirm=True, auto_retry=False,
                        params=dict(prompt='fixture', model='nano-banana-2.1',
                                    aspect='1:1', count=1, resolution='720p', duration=8))
            calls = []

            def send(body):
                calls.append(body)
                return dict(id='fixture-task', profile='account-1', project=project, kind='image')

            try:
                rid = str(uuid.uuid4())
                self.assertTrue(pool.submit_flow(data, rid, snapshot, send)[1])
                self.assertFalse(pool.submit_flow(data, rid, snapshot, send)[1])
                self.assertEqual(len(calls), 1)
                self.assertIs(calls[0]['auto_retry'], False)
                with self.assertRaisesRegex(Conflict, '该请求编号已用于其他任务'):
                    pool.submit_flow(dict(data, auto_retry=True), rid, snapshot, send)
                with self.assertRaisesRegex(ValueError, 'auto_retry'):
                    pool.submit_flow(dict(data, auto_retry='false'), str(uuid.uuid4()), snapshot, send)
                self.assertEqual(len(calls), 1)
            finally:
                pool.close()


class VideoAutoDispatchCompatibility(unittest.TestCase):
    def test_original_video_format_and_idempotency_without_provider(self):
        with fixture_directory() as directory:
            (directory / 'state').mkdir()
            pool = RealPool(directory / 'state', directory, browser_inventory=lambda: None)
            project = str(uuid.uuid4())
            chosen = dict(key='project-key', source_profile='account-1', source_id=project)
            pool._auto_flow_project = lambda *args: chosen
            snapshot = {'flow_models': {'video': [{'id':'omni-flash'}]}}
            payload = dict(kind='video', confirm=True, params=dict(prompt='Local fixture only',
                model='omni-flash', aspect='16:9', count=1, resolution='720p', duration=10))
            calls=[]
            def send(body):
                calls.append(body)
                return dict(id='fixture-video', profile='account-1', project=project, kind='video')
            try:
                rid=str(uuid.uuid4())
                first,created=pool.submit_flow(payload,rid,snapshot,send)
                repeated,created_again=pool.submit_flow(payload,rid,snapshot,send)
                self.assertTrue(created)
                self.assertFalse(created_again)
                self.assertEqual(first['task_id'],repeated['task_id'])
                self.assertEqual(first['status'],'submitted')
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0]['params']['duration'],10)
                self.assertEqual(calls[0]['params']['model'],'omni-flash')
                self.assertEqual(calls[0]['request_key'],rid)
            finally:
                pool.close()
