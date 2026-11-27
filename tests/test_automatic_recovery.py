import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
import flow_readiness_recovery as recovery
from test_flow_regressions import fixture_directory


class AutomaticRecoverySafety(unittest.IsolatedAsyncioTestCase):
    async def test_independent_accounts_repair_in_parallel_with_a_limit(self):
        with fixture_directory() as directory:
            controller = types.ModuleType('shared_session_controller')
            controller.flow_runtime = lambda: (None, types.SimpleNamespace(locks_dir=lambda: directory))
            profiles = ['one', 'two', 'three', 'four', 'busy']
            accounts = [dict(profile=p, key='flow-account-'+p, email=p+'@example.invalid',
                             projects=[], review_task=None, repairable=True) for p in profiles]
            entered = set()
            peak = set()
            full = asyncio.Event()
            release = asyncio.Event()

            async def repair_account(account, *args, **kwargs):
                profile = account['profile']
                entered.add(profile)
                if len(entered) >= len(peak):
                    peak.clear()
                    peak.update(entered)
                if len(entered) == 3:
                    full.set()
                try:
                    await release.wait()
                    if profile == 'two':
                        raise ValueError('window changed')
                    return 'ok', None
                finally:
                    entered.remove(profile)

            with patch.dict(sys.modules, {'shared_session_controller': controller}), \
                 patch('flow_window_policy.Lifecycle'), \
                 patch.object(recovery, 'inspect_page', AsyncMock(return_value='binding')), \
                 patch.object(recovery, 'repair', side_effect=repair_account), \
                 patch.object(recovery.subprocess, 'check_output', return_value='active'):
                run = asyncio.create_task(recovery.recover(dict(accounts=accounts, bindings=[], busy=['busy'])))
                try:
                    await asyncio.wait_for(full.wait(), timeout=3)
                    self.assertEqual(len(entered), 3)
                finally:
                    release.set()
                    result = await asyncio.wait_for(run, timeout=3)
            self.assertEqual(len(peak), 3)
            self.assertEqual({p: item['page'] for p, item in result.items()},
                             dict(one='ok', two='unconfirmed', three='ok', four='ok'))

    async def run_case(self, page='binding', busy=False, review=None, repairable=True, reset_draft=None,
                       maintenance=False):
        with fixture_directory() as directory:
            controller=types.ModuleType('shared_session_controller')
            controller.flow_runtime=lambda:(None,types.SimpleNamespace(locks_dir=lambda:directory))
            account=dict(profile='fixture',key='flow-account-'+'a'*64,email='fixture@example.invalid',
                         projects=[],review_task=review,repairable=repairable)
            action=AsyncMock(return_value=('ok',None))
            with patch.dict(sys.modules,{'shared_session_controller':controller}), \
                 patch('flow_window_policy.Lifecycle') as lifecycle, \
                 patch.object(recovery,'inspect_page',AsyncMock(return_value=page)), \
                 patch.object(recovery,'repair',action), \
                 patch.object(recovery,'review_hold',return_value=False), \
                 patch.object(recovery,'operator_dispatch_paused',return_value=False), \
                 patch.object(recovery.subprocess,'check_output',return_value='active'):
                lifecycle.return_value.status.return_value=[]
                payload = dict(accounts=[account],bindings=[],busy=['fixture'] if busy else [],
                               safe_only=True,operator_dispatch_paused=maintenance)
                if reset_draft is not None:
                    payload['reset_draft'] = reset_draft
                result=await recovery.recover(payload)
            return result,action

    async def test_recoverable_page_uses_guarded_repair(self):
        result,action=await self.run_case()
        self.assertEqual(result['fixture']['page'],'ok')
        self.assertTrue(action.await_args.kwargs['safe_only'])

    async def test_running_tasks_and_unconfirmed_results_are_not_touched(self):
        for args in [dict(busy=True),dict(repairable=False)]:
            result,action=await self.run_case(**args)
            self.assertEqual(result,{})
            action.assert_not_awaited()

        result,action=await self.run_case(review='original-task')
        self.assertEqual(result['fixture']['page'],'unconfirmed')
        action.assert_not_awaited()

    async def test_plain_inspection_and_maintenance_do_not_reset_drafts(self):
        result,action=await self.run_case(page='draft')
        self.assertEqual(result['fixture']['page'],'draft')
        action.assert_not_awaited()
        result,action=await self.run_case(page='draft',reset_draft=False,maintenance=True)
        self.assertEqual(result['fixture']['page'],'draft')
        action.assert_not_awaited()
        result,action=await self.run_case(page='login',reset_draft=False)
        self.assertEqual(result['fixture']['page'],'login')
        action.assert_not_awaited()

    async def test_manual_reset_repairs_idle_drafts_only(self):
        result,action=await self.run_case(page='draft',reset_draft=True)
        self.assertEqual(result['fixture']['page'],'ok')
        self.assertTrue(action.await_args.kwargs['reset_draft'])
        for args in [dict(busy=True),dict(repairable=False),dict(review='original-task')]:
            _,action=await self.run_case(page='draft',reset_draft=True,**args)
            action.assert_not_awaited()

    async def test_automatic_reset_rechecks_idle_drafts_only(self):
        result,action=await self.run_case(page='draft',reset_draft=False)
        self.assertEqual(result['fixture']['page'],'ok')
        self.assertEqual(action.await_args.args[1],'draft')
        self.assertTrue(action.await_args.kwargs['auto_reset_draft'])
        for args in [dict(busy=True),dict(repairable=False),dict(review='original-task')]:
            _,action=await self.run_case(page='draft',reset_draft=False,**args)
            action.assert_not_awaited()

    async def test_duplicate_cleanup_retains_binding_and_requires_empty_project_or_home_pages(self):
        project='https://flow.google.com/project/11111111-1111-4111-8111-111111111111'
        account=dict(profile='fixture',key='flow-account-'+'a'*64,email='fixture@example.invalid')
        binding=dict(account_key='a'*64,browser_pid=123,port=9222,page_target='fixed',project_url=project)
        endpoint='ws://127.0.0.1:9222/devtools/page/copy'
        fixed=dict(id='fixed',url=project)
        empty=dict(origin=True,account=True,editor=True,path=project.removeprefix('https://flow.google.com'))
        home='https://flow.google.com/'
        empty_home=dict(origin=True,account=True,editor=False,path='/',emptyHome=True)
        cases = [(project, empty, 'ok'), (project, dict(empty,draft=True), 'unique'),
                 (project, dict(empty,busy=True), 'unique'), (home, empty_home, 'ok'),
                 (home+'?pli=1', empty_home, 'ok'),
                 (home+'?unrecognized=1', empty_home, 'unique'),
                 (home, dict(empty_home,emptyHome=False), 'unique'),
                 (home, dict(empty_home,overlay=True), 'unique'),
                 (home, dict(empty_home,account=False), 'unique'),
                 (home, dict(empty_home,draft=True), 'unique'),
                 (home+'about?returnUrl=%2Fproject%2Fother', empty_home, 'unique')]
        for url, state, expected in cases:
            with self.subTest(url=url, expected=expected, state=state):
                copy=dict(id='copy',url=url,webSocketDebuggerUrl=endpoint)
                fake_websockets=types.SimpleNamespace(connect=Mock(return_value=AsyncMock()))
                with patch.dict(sys.modules,websockets=fake_websockets), \
                     patch('flow_window_policy.Lifecycle') as lifecycle, \
                     patch('task_coordinator.Coordinator'), \
                     patch.object(recovery,'guard_live'), \
                     patch.object(recovery,'TaskWindowRegistry') as registry, \
                     patch.object(recovery,'inspect_page',AsyncMock(side_effect=['unique','unique','ok'])), \
                     patch.object(recovery,'_pages',side_effect=[[fixed,copy],[fixed,copy],[fixed]]), \
                     patch.object(recovery,'evaluate',AsyncMock(return_value=state)), \
                     patch.object(recovery,'command',AsyncMock()) as command:
                    registry.return_value.get.return_value=binding
                    actions=[]
                    self.assertEqual(await recovery.reconcile_fixed_window(account,binding,actions),expected)
                if expected=='ok':
                    self.assertEqual(actions,['close_duplicate'])
                    self.assertEqual(command.await_args.args[1:],('Page.close',))
                    lifecycle.return_value.observe.assert_called_once_with('fixture',1,1)
                else:
                    self.assertEqual(actions,[])
                    command.assert_not_awaited()
                    lifecycle.return_value.observe.assert_not_called()

    async def test_multiple_windows_reuse_exact_registered_task_target(self):
        account=dict(profile='fixture',key='flow-account-'+'a'*64)
        known=dict(browser_pid=123,port=9222,process_start='stamp',page_target='A',window_id=101,workspace=0)
        pages=[dict(id=x,webSocketDebuggerUrl='ws://127.0.0.1:9222/devtools/page/'+x) for x in ['A','B']]
        windows=[dict(id=x,workspace=0) for x in [101,102]]
        with patch.object(recovery,'registration',return_value=dict(pid=123,port=9222)), \
             patch.object(recovery,'process_identity',return_value=('fixture','stamp')), \
             patch.object(recovery,'_pages',return_value=pages), \
             patch.object(recovery,'focus_call',return_value=windows), \
             patch.object(recovery,'TaskWindowRegistry') as registry:
            registry.return_value.get.return_value=known
            current,page,_=await recovery.candidate(account)
        self.assertEqual(current['window_id'],101)
        self.assertEqual(page['id'],'A')

    async def test_bounded_no_output_evidence_is_distinct_from_a_generic_timeout(self):
        task=dict(status='failed',error='no image reply within 240 s, and no new image is in the project',
                  result={'source_jobs':[dict(status='failed',media_ids=[],files=[])]})
        self.assertTrue(recovery.failed_without_result(task))
        task['error']='connection timeout'
        self.assertFalse(recovery.failed_without_result(task))
        task['error']='no image reply within 240 s, and no new image is in the project'
        task['result']['source_jobs'][0]['media_ids']=['existing-media']
        self.assertFalse(recovery.failed_without_result(task))

    async def test_responsive_restart_requires_the_single_owned_task_page(self):
        binding=dict(profile='fixture',browser_pid=123,account_key='a'*64,port=9222,
                     process_start='stamp',page_target='A',window_id=101)
        for tabs,allowed in [([dict(type='page',id='A')],True),
                             ([dict(type='page',id='A'),dict(type='page',id='manual')],False)]:
            with patch.object(recovery.subprocess,'check_output',return_value='123'), \
                 patch.object(recovery.subprocess,'run') as restart, \
                 patch.object(recovery,'process_identity',return_value=('fixture','stamp')), \
                 patch.object(recovery,'focus_call',return_value=[dict(id=101)]), \
                 patch.object(recovery,'build_opener') as opener:
                opener.return_value.open.side_effect=[BytesIO(b'{}'),BytesIO(json.dumps(tabs).encode())]
                if allowed:
                    recovery.restart_managed(binding,allow_responsive=True)
                    restart.assert_called_once()
                else:
                    with self.assertRaisesRegex(ValueError,'restart_not_safe'):
                        recovery.restart_managed(binding,allow_responsive=True)
                    restart.assert_not_called()
