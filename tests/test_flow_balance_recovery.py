import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as S
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(ROOT/'flow/console'), str(ROOT/'workbench')]
from account_insights import FRESH_SECONDS, Insights
from balance_reader import fixed_flow_page, page_url, open_account_panel, BalancePanelUnavailable, BalanceReadDeferred, REVEAL_HEADER
from console_store import Store


class BalanceRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_missing_playwright_url_uses_only_the_verified_target(self):
        project = 'https://flow.google.com/project/11111111-1111-4111-8111-111111111111'
        binding = dict(profile='a', binding_state='verified', browser_pid=120, port=9222,
                       process_start='stamp', page_target='bound', project_url=project)
        for target, observed, accepted in (('bound', project, True),
                                           ('other', project, False),
                                           ('bound', 'https://accounts.google.com/', False)):
            with self.subTest(target=target, accepted=accepted, observed=observed):
                session = S(send=AsyncMock(side_effect=[
                    {'targetInfo': {'type': 'page', 'targetId': target, 'url': project}},
                    {'result': {'value': {'url': observed}}},
                    {'result': {'value': {'url': observed}}},
                ]), detach=AsyncMock())
                page = S(url='', context=S(new_cdp_session=AsyncMock(return_value=session)))
                browser = S(contexts=[S(pages=[page])])
                registry = S(get=lambda key: binding)
                with patch('task_window_registry.TaskWindowRegistry', return_value=registry), \
                        patch('task_window_registry.process_identity', return_value=('a', 'stamp')):
                    async with AsyncExitStack() as sessions:
                        if accepted:
                            chosen = await fixed_flow_page(browser, 'a', 'a@example.test', sessions)
                            self.assertEqual(await page_url(chosen), project)
                        else:
                            with self.assertRaisesRegex(ValueError, '现有浏览器没有 Flow 页面'):
                                await fixed_flow_page(browser, 'a', 'a@example.test', sessions)
                session.detach.assert_awaited_once()

    def make_fixture(self, *, available=True, stays_missing=False, identity='a@example.test', duplicate=False):
        page = S(url='https://flow.google.com/project/fixture', focused=False)
        state = dict(lease_entries=0, lease_exits=0)
        async def front(): page.focused=True
        async def evaluate(expression):
            if expression == 'identity': return {'signed_in':True,'emails':[identity]}
            return 720 if not page.focused else None
        page.bring_to_front=AsyncMock(side_effect=front)
        page.wait_for_load_state=AsyncMock()
        page.evaluate=AsyncMock(side_effect=evaluate)
        count = lambda: 2 if duplicate else (1 if page.focused and not stays_missing else 0)
        panel=S(count=AsyncMock(side_effect=count), click=AsyncMock())
        close=S(count=AsyncMock(return_value=0))
        @asynccontextmanager
        async def acquire():
            state['lease_entries']+=1
            try: yield available
            finally: state['lease_exits']+=1
        queue=S(try_acquire_async=acquire)
        return page,panel,close,state,queue

    async def run_fixture(self, page,panel,close,queue,restoration):
        with patch.dict(sys.modules, {'adapter':S(READ_STATE='identity'),
                'foreground_queue':S(ForegroundQueue=lambda _:queue)}), \
             patch('balance_reader.asyncio.sleep',new_callable=AsyncMock):
            async with AsyncExitStack() as stack:
                return await open_account_panel(page,panel,close,'a@example.test',
                    'https://flow.google.com/project/fixture',stack,restoration)

    async def test_missing_button_switches_exact_tab_then_rechecks(self):
        page,panel,close,state,queue=self.make_fixture()
        restoration={}
        opened,top=await self.run_fixture(page,panel,close,queue,restoration)
        self.assertTrue(opened)
        self.assertEqual(top,720)
        page.bring_to_front.assert_awaited_once()
        panel.click.assert_awaited_once()
        self.assertEqual(state,dict(lease_entries=1,lease_exits=1))
        self.assertEqual(restoration['scroll_top'],720)

    async def test_foreground_busy_defers_without_switching(self):
        page,panel,close,state,queue=self.make_fixture(available=False)
        with self.assertRaises(BalanceReadDeferred):
            await self.run_fixture(page,panel,close,queue,{})
        page.bring_to_front.assert_not_awaited()
        panel.click.assert_not_awaited()
        self.assertEqual(state,dict(lease_entries=1,lease_exits=1))

    async def test_still_missing_reports_only_after_one_recovery(self):
        page,panel,close,state,queue=self.make_fixture(stays_missing=True)
        restoration={}
        with self.assertRaisesRegex(BalancePanelUnavailable, 'Flow 账号详情暂未打开'):
            await self.run_fixture(page,panel,close,queue,restoration)
        page.bring_to_front.assert_awaited_once()
        panel.click.assert_not_awaited()
        self.assertEqual(restoration['scroll_top'],720)
        self.assertEqual(state['lease_exits'],1)

    async def test_other_account_never_clicked_or_focused(self):
        page,panel,close,state,queue=self.make_fixture(identity='other@example.test')
        with self.assertRaisesRegex(ValueError,'邮箱与登记账号不符'):
            await self.run_fixture(page,panel,close,queue,{})
        page.bring_to_front.assert_not_awaited()
        panel.click.assert_not_awaited()

    async def test_duplicate_entries_never_guessed(self):
        page,panel,close,state,queue=self.make_fixture(duplicate=True)
        with self.assertRaisesRegex(ValueError,'多个账号详情入口'):
            await self.run_fixture(page,panel,close,queue,{})
        page.bring_to_front.assert_not_awaited()
        panel.click.assert_not_awaited()

    async def test_already_open_panel_is_left_open(self):
        page,panel,close,state,queue=self.make_fixture()
        close.count.return_value=1
        opened,top=await self.run_fixture(page,panel,close,queue,{})
        self.assertFalse(opened)
        self.assertIsNone(top)
        page.bring_to_front.assert_not_awaited()
        panel.click.assert_not_awaited()

    async def test_page_change_is_not_navigated_back_or_clicked(self):
        page,panel,close,state,queue=self.make_fixture()
        async def front(): page.url='https://flow.google.com/project/other'
        page.bring_to_front.side_effect=front
        with self.assertRaisesRegex(ValueError,'页面在读取时发生切换'):
            await self.run_fixture(page,panel,close,queue,{})
        panel.click.assert_not_awaited()
        self.assertEqual(state['lease_exits'],1)


class BalanceObservation(unittest.TestCase):
    def test_daily_credits_require_a_current_verified_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'console-data')
            store.execute('CREATE TABLE account_balances(profile TEXT PRIMARY KEY,balance REAL,observed REAL,'
                          'checked REAL,status TEXT,error TEXT)')
            store.execute("INSERT INTO account_balances VALUES('a',1050,1000,1000,'ready',NULL)")
            insights = Insights(store, root/'source.db')
            account = dict(profile='a', state='verified')

            def remaining(now, state='verified'):
                return insights.report([dict(account, state=state)], now=now)['accounts'][0]['daily_credits_remaining']

            self.assertIsNone(remaining(1000))
            insights.record('a', 1029, now=1001, daily_credits_remaining=29)
            self.assertEqual(remaining(1001), 29)
            self.assertIsNone(remaining(1001, state='attention'))
            self.assertIsNone(remaining(1002 + FRESH_SECONDS))
            insights.record('a', error='panel not ready', now=1002, transient=True)
            self.assertIsNone(remaining(1002))
            insights.record('a', 800, now=1003, daily_credits_remaining=0)
            self.assertEqual(remaining(1003), 0)
            insights.record('a', 800, now=1004)
            self.assertIsNone(remaining(1004))

    def test_temporary_panel_failure_retains_only_a_recent_verified_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            insights = Insights(Store(root/'console-data'), root/'source.db')
            account = dict(profile='a', state='verified', generation_state='not_cooling_down', risk_paused=False)

            def observe(at, **account_state):
                return insights.report([dict(account, **account_state)], now=at)['accounts'][0]

            insights.record('a', 775, now=1000)
            insights.record('a', error='panel not ready', now=1200, transient=True)
            self.assertTrue(observe(1200)['balance_fresh'])
            self.assertEqual(observe(1200)['balance_observed'], 1000)
            self.assertEqual(observe(1200, flow_page_incident=True)['health'], 'paused')
            self.assertFalse(observe(1001+FRESH_SECONDS)['balance_fresh'])
            insights.record('a', error='wrong identity', now=1250)
            insights.record('a', error='panel not ready', now=1260, transient=True)
            self.assertFalse(observe(1260)['balance_fresh'])
            insights.record('a', 0, now=1300)
            insights.record('a', error='panel not ready', now=1310, transient=True)
            self.assertEqual(observe(1310)['health'], 'empty')


class BalanceScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_reads_two_accounts_in_parallel_and_defers_busy_accounts(self):
        spec = spec_from_file_location('flow_balance_console', ROOT/'flow/console/server.py')
        server = module_from_spec(spec)
        spec.loader.exec_module(server)
        rows = [dict(profile=p) for p in ('a', 'b', 'c', 'busy', 'remote', 'paused')]
        rows[-1]['risk_paused'] = True
        slot = Mock()
        console = S(balance_refreshing=False, balance_busy=set(), busy={'busy'},
                    root=Path('.'), store=S(accounts=lambda: rows, page_incidents=dict),
                    remote=S(for_profile=lambda profile: profile == 'remote'),
                    dispatch_limit=S(paused=lambda: False, try_acquire=lambda: slot),
                    insights=S(record=Mock()))
        started = []
        two_active = asyncio.Event()
        release = asyncio.Event()

        async def panel(account, *args, **kwargs):
            started.append(account['profile'])
            if len(started) == 2:
                two_active.set()
            await release.wait()
            return {'balance':12, 'next_credit_date':None, 'daily_credits_remaining':7}

        with patch.object(server, 'account_status', return_value=rows), \
             patch('balance_reader.read_balance', side_effect=panel):
            run = asyncio.create_task(server.Console.read_balance_batch(console, None))
            try:
                await asyncio.wait_for(two_active.wait(), timeout=3)
                self.assertCountEqual(started, ['a', 'b'])
                self.assertEqual(console.balance_busy, {'a', 'b'})
            finally:
                release.set()
                deferred = await asyncio.wait_for(run, timeout=3)
        self.assertEqual(deferred, {'busy', 'remote'})
        self.assertCountEqual(started, ['a', 'b', 'c'])
        self.assertEqual(slot.close.call_count, 3)
        self.assertEqual(console.insights.record.call_count, 3)
        for profile in ('a', 'b', 'c'):
            console.insights.record.assert_any_call(profile, 12, next_credit_date=None, daily_credits_remaining=7)
        self.assertFalse(console.balance_busy)
        self.assertFalse(console.balance_refreshing)

    async def test_only_temporarily_unavailable_account_is_retried_twice(self):
        spec = spec_from_file_location('flow_balance_console', ROOT/'flow/console/server.py')
        server = module_from_spec(spec)
        spec.loader.exec_module(server)
        calls = []
        retries_finished = asyncio.Event()

        async def batch(profiles):
            calls.append(profiles)
            if len(calls) == 3:
                retries_finished.set()
            return {'account-a'}

        console = S(test=False, balance_wake=asyncio.Event(), balance_requested=set(),
                    balance_full_requested=False, dispatch_limit=S(paused=lambda: False),
                    read_balance_batch=batch)
        with patch.object(server, 'BALANCE_RETRY_DELAYS', (0, 0)):
            worker = asyncio.create_task(server.Console.balance_worker(console))
            try:
                await asyncio.wait_for(retries_finished.wait(), timeout=1)
                await asyncio.sleep(0)
                self.assertEqual(calls, [None, {'account-a'}, {'account-a'}])
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker


if __name__=='__main__':
    unittest.main()
