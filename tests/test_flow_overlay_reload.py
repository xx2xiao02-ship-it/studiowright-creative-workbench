import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
import flow_readiness_recovery as recovery
import flow_check_readiness as readiness


PROJECT = 'https://flow.google.com/project/11111111-1111-4111-8111-111111111111'
STATE = dict(origin=True, account=True, path=PROJECT.removeprefix('https://flow.google.com'), overlay=True)


class FlowOverlayReload(unittest.IsolatedAsyncioTestCase):
    async def test_reopened_browser_reports_login_only_for_the_accounts_original_project(self):
        account = dict(profile='profile-a', key='flow-account-key', projects=[PROJECT.rsplit('/', 1)[1]])
        binding = dict(binding_state='unconfirmed', account_key='key', browser_pid=100, port=9222,
                       project_url=PROJECT)
        cases = [
            ([dict(url='https://accounts.google.com/v3/signin/confirmidentifier?continue='+PROJECT)], [{}], 'login'),
            ([dict(url='https://accounts.google.com/v3/signin/challenge/totp?continue='+PROJECT)], [{}], 'verification_required'),
            ([dict(url='https://accounts.google.com/v3/signin/identifier')], [{}], 'binding'),
            ([dict(url='https://accounts.google.com/v3/signin/identifier?continue=https://flow.google.com/')], [{}], 'binding'),
            ([dict(url='https://accounts.google.com/v3/signin/identifier?continue='+PROJECT)], [{}, {}], 'binding'),
            ([dict(url='https://accounts.google.com/v3/signin/identifier?continue='+PROJECT), dict(url=PROJECT)], [{}], 'binding'),
        ]
        for pages, windows, expected in cases:
            with self.subTest(expected=expected, pages=pages, windows=windows):
                with patch.object(readiness, '_live_browser', return_value=101) as browser, \
                        patch.object(readiness, '_pages', return_value=pages) as inventory, \
                        patch.object(readiness, 'focus_call', return_value=windows):
                    self.assertEqual(await readiness.inspect_page(account, binding), expected)
                browser.assert_called_once_with('key', 100, 9222)
                inventory.assert_called_once_with('key', 101, 9222, include_non_flow=True)

    async def test_closes_account_panel_on_verified_task_page_before_refresh(self):
        account = {'profile': 'profile-a', 'key': 'flow-account-key', 'email': 'test@example.com'}
        project_id = PROJECT.rsplit('/', 1)[1]
        current = dict(account_key='key', profile='profile-a', browser_pid=100, process_start='stamp',
                       port=9222, page_target='fixed', window_id=12, workspace=0)
        binding = dict(current, chrome_window=34, project_url=PROJECT, binding_state='verified')
        editor = dict(STATE, overlay=False, editor=True)
        registry, lifecycle, coordinator = MagicMock(), MagicMock(), MagicMock()
        registry.get.return_value = binding
        session = MagicMock()
        fake_websockets = SimpleNamespace(connect=MagicMock(return_value=session))
        endpoint = 'ws://127.0.0.1:9222/devtools/page/fixed'
        with patch.dict(sys.modules, websockets=fake_websockets), \
                patch.object(recovery, 'guard_live', return_value=({project_id}, {project_id})), \
                patch.object(recovery, 'candidate', AsyncMock(return_value=(current, {'url': PROJECT}, endpoint))), \
                patch.object(recovery, 'TaskWindowRegistry', return_value=registry), \
                patch('flow_window_policy.Lifecycle', return_value=lifecycle), \
                patch('task_coordinator.Coordinator', return_value=coordinator), \
                patch.object(recovery, 'command', AsyncMock(return_value={'windowId': 34})) as command, \
                patch.object(recovery, 'evaluate', AsyncMock(side_effect=[STATE, True, editor])) as evaluate, \
                patch.object(recovery, 'inspect_page', AsyncMock(return_value='ok')) as inspect, \
                patch.object(recovery.asyncio, 'sleep', AsyncMock()):
            actions = []
            verdict, confirmed = await recovery.repair(account, 'overlay', actions, safe_only=True)
        self.assertEqual((verdict, confirmed), ('ok', binding))
        self.assertEqual(actions, ['dismiss_account_panel'])
        self.assertIn(recovery.DISMISS_ACCOUNT_PANEL, evaluate.await_args_list[1].args[1])
        self.assertEqual([call.args[1] for call in command.await_args_list], ['Browser.getWindowForTarget'])
        inspect.assert_awaited_once()
        registry.bind.assert_called_once()

    async def test_unrecognized_modal_is_left_for_existing_refresh(self):
        actions = []
        with patch.object(recovery, 'guard_live'), \
                patch.object(recovery, 'evaluate', AsyncMock(return_value=False)) as evaluate:
            state = await recovery.close_account_panel(None, {}, 'readPage()', PROJECT, STATE, actions)
        self.assertIs(state, STATE)
        self.assertEqual(actions, [])
        evaluate.assert_awaited_once()

    async def test_bound_google_challenge_is_reported_without_reading_login_page(self):
        account = dict(profile='profile-a', key='flow-account-key', projects=[PROJECT.rsplit('/', 1)[1]])
        binding = dict(binding_state='verified', account_key='key', browser_pid=100, port=9222,
                       process_start='stamp', page_target='fixed', project_url=PROJECT,
                       window_id=12, workspace=0, chrome_window=34)
        target = dict(id='fixed', url='https://accounts.google.com/v3/signin/challenge/totp',
                      webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/page/fixed')
        unrelated = dict(target, id='other')
        for pages, window_id, expected in (([target, unrelated], 34, 'verification_required'),
                                            ([unrelated], 34, 'binding'), ([target], 56, 'binding')):
            with self.subTest(expected=expected, window_id=window_id):
                socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(return_value=json.dumps({
                    'result': {'windowId': window_id}})))
                session = MagicMock()
                session.__aenter__.return_value = socket
                fake_websockets = SimpleNamespace(connect=MagicMock(return_value=session))
                with patch.dict(sys.modules, websockets=fake_websockets), \
                        patch.object(readiness, 'process_identity', return_value=('profile-a', 'stamp')), \
                        patch.object(readiness, '_pages', return_value=pages) as inventory, \
                        patch.object(readiness, 'focus_call', return_value=[{'id': 12, 'workspace': 0}]):
                    self.assertEqual(await readiness.inspect_page(account, binding), expected)
                inventory.assert_called_once_with('key', 100, 9222, include_non_flow=True)
                self.assertEqual([json.loads(call.args[0])['method'] for call in socket.send.await_args_list],
                                 ['Browser.getWindowForTarget'] if target in pages else [])

    def test_login_url_classifier_requires_the_google_origin(self):
        self.assertEqual(readiness.google_login_page('https://accounts.google.com/signin/v2/challenge/selection'),
                         'verification_required')
        self.assertEqual(readiness.google_login_page('https://accounts.google.com/v3/signin/identifier'), 'login')
        for url in ('https://accounts.google.com.example.org/signin/challenge',
                    'https://accounts.google.com/ManageAccount?next=/signin/challenge', PROJECT):
            self.assertIsNone(readiness.google_login_page(url))

    def test_intro_return_is_limited_to_the_original_project(self):
        path = '/project/' + PROJECT.rsplit('/', 1)[1]
        url = 'https://flow.google.com/about?returnUrl=' + path
        self.assertTrue(readiness.flow_project_redirect(url, PROJECT))
        self.assertFalse(readiness.flow_project_redirect(url + '&returnUrl=' + path, PROJECT))
        self.assertFalse(readiness.flow_project_redirect(url.replace('flow.google.com', 'other.invalid'), PROJECT))
        self.assertFalse(readiness.flow_project_redirect(url.replace(path, PROJECT), PROJECT))
        self.assertEqual(readiness.google_login_page('https://accounts.google.com/v3/signin/challenge/pwd'), 'login')

    async def test_intro_reopens_original_project_once_and_leaves_google_verification(self):
        original = dict(id='frame', loaderId='old', url='https://flow.google.com/about?returnUrl=' + STATE['path'])
        for path, expected in [
            (PROJECT, 'ok'),
            (original['url'], 'flow_landing'),
            ('https://accounts.google.com/v3/signin/identifier', 'login'),
            ('https://accounts.google.com/v3/signin/challenge/totp', 'verification_required'),
        ]:
            with self.subTest(expected=expected):
                before = {'frameTree': {'frame': original}}
                after = {'frameTree': {'frame': dict(original, loaderId='new', url=path)}}
                actions = []
                with patch.object(recovery, 'guard_live'), \
                        patch.object(recovery, 'command', AsyncMock(side_effect=[before, before, {}, after])) as command, \
                        patch.object(recovery, 'evaluate', AsyncMock(side_effect=[
                            dict(origin=True), dict(STATE, overlay=False, editor=True)])), \
                        patch.object(recovery.asyncio, 'sleep', AsyncMock()):
                    state = await recovery.reopen_redirected_project(None, {}, '', PROJECT, actions)
                self.assertEqual(recovery.page_verdict(state), expected)
                self.assertEqual(actions, ['return_to_project'])
                self.assertEqual([call.args[2] for call in command.await_args_list if call.args[1]=='Page.navigate'],
                                 [{'url': PROJECT, 'frameId': 'frame'}])

    async def test_intro_with_a_draft_does_not_navigate(self):
        frame = {'frameTree': {'frame': {'url':'https://flow.google.com/about?returnUrl=' + STATE['path']}}}
        actions = []
        with patch.object(recovery, 'guard_live'), \
                patch.object(recovery, 'command', AsyncMock(return_value=frame)) as command, \
                patch.object(recovery, 'evaluate', AsyncMock(return_value=dict(origin=True, draft=True))):
            result = await recovery.reopen_redirected_project(None, {}, '', PROJECT, actions)
        self.assertEqual(recovery.page_verdict(result), 'draft')
        command.assert_awaited_once()
        self.assertEqual(actions, [])

    async def test_refreshes_same_project_and_checks_new_document(self):
        loaded = dict(STATE, overlay=False, editor=True)
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        actions = []
        with patch.object(recovery, 'guard_live'), patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, old, new])) as c, \
                patch.object(recovery, 'evaluate', AsyncMock(side_effect=[STATE, loaded])), patch.object(recovery.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(await recovery.reload_overlay(None, {}, '', PROJECT, actions), loaded)
        self.assertEqual(actions, ['reload'])
        self.assertEqual([a.args[1] for a in c.await_args_list],
                         ['Page.getFrameTree', 'Page.reload', 'Page.getFrameTree', 'Page.getFrameTree'])
        self.assertEqual(c.await_args_list[1].args[2], {'loaderId': 'old'})

    async def test_busy_or_draft_pages_are_never_refreshed(self):
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        for flag in ('busy', 'draft'):
            state = dict(STATE, **{flag: True})
            actions = []
            with patch.object(recovery, 'guard_live'), patch.object(recovery, 'command', AsyncMock(return_value=old)) as c, \
                    patch.object(recovery, 'evaluate', AsyncMock(return_value=state)):
                self.assertEqual(await recovery.reload_overlay(None, {}, '', PROJECT, actions), state)
            c.assert_awaited_once()
            self.assertEqual(actions, [])

    async def test_blank_page_reloads_once_and_waits_for_the_original_editor(self):
        blank = dict(origin=True, account=False, loadFailed=True, path=STATE['path'])
        loading = dict(blank, loadFailed=False)
        ready = dict(origin=True, account=True, editor=True, path=STATE['path'])
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        actions = []
        self.assertEqual(recovery.page_verdict(blank), 'load_failed')
        self.assertEqual(recovery.page_verdict(loading), 'unconfirmed')
        with patch.object(recovery, 'guard_live'), \
                patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, old, new, new])) as c, \
                patch.object(recovery, 'evaluate', AsyncMock(side_effect=[blank, loading, ready])), \
                patch.object(recovery.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(await recovery.reload_fixed_project(None, {}, '', PROJECT, actions, 'load_failed'), ready)
        self.assertEqual(actions, ['reload'])
        self.assertEqual([call.args[2] for call in c.await_args_list if call.args[1] == 'Page.reload'],
                         [{'loaderId': 'old'}])

    async def test_blank_page_recovery_leaves_sign_in_and_drafts_alone(self):
        frame = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        for flag, verdict in [('login', 'login'), ('busy', 'busy'), ('draft', 'draft')]:
            with self.subTest(flag=flag):
                state = dict(origin=True, account=False, loadFailed=True, path=STATE['path'], **{flag: True})
                actions = []
                self.assertEqual(recovery.page_verdict(state), verdict)
                with patch.object(recovery, 'guard_live'), \
                        patch.object(recovery, 'command', AsyncMock(return_value=frame)) as c, \
                        patch.object(recovery, 'evaluate', AsyncMock(return_value=state)):
                    self.assertEqual(await recovery.reload_fixed_project(None, {}, '', PROJECT, actions, 'load_failed'), state)
                c.assert_awaited_once()
                self.assertEqual(actions, [])

    async def test_manual_draft_reset_reloads_once_and_rechecks_the_editor(self):
        draft = dict(origin=True, account=True, draft=True, editor=True, path=STATE['path'])
        ready = dict(draft, draft=False)
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        for after in (ready, draft):
            actions = []
            with patch.object(recovery, 'guard_live'), \
                    patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, new])) as command, \
                    patch.object(recovery, 'evaluate', AsyncMock(side_effect=[draft, after])), \
                    patch.object(recovery.asyncio, 'sleep', AsyncMock()):
                self.assertEqual(await recovery.reload_fixed_project(None, {}, '', PROJECT, actions, 'draft'), after)
            self.assertEqual(actions, ['reset_draft'])
            self.assertEqual([c.args[2] for c in command.await_args_list if c.args[1] == 'Page.reload'],
                             [{'loaderId': 'old'}])

    async def test_busy_project_can_reload_only_after_the_editor_is_idle(self):
        ready = dict(origin=True, account=True, editor=True, path=STATE['path'])
        busy = dict(ready, busy=True)
        old = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'old'}}}
        new = {'frameTree': {'frame': {'url': PROJECT, 'loaderId': 'new'}}}
        for cleared in (True, False):
            actions = []
            states = [busy, ready, ready, ready] if cleared else [busy] * 5
            with patch.object(recovery, 'guard_live'), \
                    patch.object(recovery, 'command', AsyncMock(side_effect=[old, {}, new])) as command, \
                    patch.object(recovery, 'evaluate', AsyncMock(side_effect=states)), \
                    patch.object(recovery.asyncio, 'sleep', AsyncMock()):
                result = await recovery.reload_fixed_project(None, {}, '', PROJECT, actions, 'busy')
            self.assertEqual(result, ready if cleared else busy)
            self.assertEqual(actions, ['reload'] if cleared else [])
            self.assertEqual([c.args[2] for c in command.await_args_list if c.args[1] == 'Page.reload'],
                             [{'loaderId': 'old'}] if cleared else [])
