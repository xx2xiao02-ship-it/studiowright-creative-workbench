import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from live_pool import RealPool
from official_pool import QUOTA_LIMIT_BLOCK, QUOTA_RECHECK_COOLDOWN, READINESS_PROMPT, READINESS_REPLY


class GeminiQuotaRecovery(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1800000000
        self.enterContext(patch('official_pool.time.time', side_effect=lambda: self.now))
        self.pool = RealPool(root, root, browser_inventory=lambda: None)
        self.addCleanup(self.pool.close)
        self.official = self.pool.official
        self.account = 'a' * 64
        self.other = 'b' * 64
        self.model = 'gemini-4-neon-high'
        self.inventory = {self.account: True, self.other: True}
        self.runtime = SimpleNamespace(
            inventory=lambda: self.inventory,
            text_models=lambda: {self.account: {self.model}, self.other: {self.model}},
            run=Mock(),
        )
        self.official.runtime = self.runtime
        self.quota = {}
        self.reader = Mock()
        self.reader.cached_accounts.side_effect = lambda keys: {
            key: self.quota if key == self.account else {} for key in keys
        }
        self.enterContext(patch('official_pool.reader_for', return_value=self.reader))
        self.original = self.submit(self.account)
        self.official.finish(self.official.claim(), {
            'status': 'failed', 'reason': QUOTA_LIMIT_BLOCK, 'submitted': True,
        })
        self.block_time = self.now

    def submit(self, account):
        request = str(uuid.uuid4())
        self.official.generate(account, 'fixture business prompt', request,
                               [{'key': 'flow-account-' + account}], model=self.model)
        return request

    def readable_quota(self, five_hour=100, weekly=5, **changes):
        self.quota = {'status': 'ok', 'identity_matched': True, 'checked_at': self.now,
                      'five_hour': {'remaining_percent': five_hour},
                      'weekly': {'remaining_percent': weekly}, **changes}

    def dispatch_restriction(self):
        with self.pool.connection() as db:
            return self.official.reason(db, self.account, self.inventory)

    def completed_check(self, job):
        self.runtime.run.return_value = {'status': 'completed', 'response': READINESS_REPLY,
                                         'submitted': True, 'native': str(uuid.uuid4())}
        self.official.finish(job, self.official.run_text(job))

    def test_automatic_check_requires_fresh_positive_quota_and_recovers_only_after_success(self):
        self.now += QUOTA_RECHECK_COOLDOWN - 1
        self.readable_quota()
        self.assertEqual(self.official.recheck_quota_blocks(), [])
        self.reader.watch_quota_recovery.assert_called_once()
        self.assertIn(self.account, self.reader.watch_quota_recovery.call_args.args[0])
        self.now += 2
        for name, values in (
            ('empty cycle', {'five_hour': 0}),
            ('unknown window', {'weekly': None}),
            ('wrong identity', {'identity_matched': False}),
            ('old observation', {'checked_at': self.now - 601}),
            ('verification', {'status': 'verification_required'}),
        ):
            with self.subTest(name=name):
                self.readable_quota(**values)
                self.assertEqual(self.official.recheck_quota_blocks(), [])

        self.readable_quota()
        [probe] = self.official.recheck_quota_blocks()
        self.assertEqual(self.official.recheck_quota_blocks(), [])
        self.assertEqual(self.dispatch_restriction(), 'official_call_blocked')
        job = self.official.claim()
        self.assertEqual((job['request_id'], job['account_key'], job['model'], job['prompt']),
                         (probe, self.account, self.model, READINESS_PROMPT))
        self.assertEqual(job['no_retry'], 1)
        self.completed_check(job)
        self.assertIsNone(self.dispatch_restriction())
        self.runtime.run.assert_called_once()
        self.assertEqual(self.runtime.run.call_args.kwargs, {'probe': True, 'model': self.model})
        self.assertEqual(self.pool.get(self.original)['status'], 'failed')
        new = self.submit(self.account)
        self.assertEqual(self.official.claim()['request_id'], new)

    def test_failed_check_waits_for_cooldown_and_new_quota_reading(self):
        self.now += QUOTA_RECHECK_COOLDOWN + 1
        self.readable_quota()
        self.assertEqual(len(self.official.recheck_quota_blocks()), 1)
        self.official.finish(self.official.claim(), {
            'status': 'failed', 'reason': QUOTA_LIMIT_BLOCK, 'submitted': True,
        })
        self.assertEqual(self.dispatch_restriction(), 'official_call_blocked')
        self.now += QUOTA_RECHECK_COOLDOWN - 1
        self.readable_quota()
        self.assertEqual(self.official.recheck_quota_blocks(), [])
        self.now += 2
        self.assertEqual(self.official.recheck_quota_blocks(), [])
        self.readable_quota()
        self.assertEqual(len(self.official.recheck_quota_blocks()), 1)

    def test_unconfirmed_submission_cannot_release_the_account(self):
        self.now += QUOTA_RECHECK_COOLDOWN + 1
        self.readable_quota()
        self.assertEqual(len(self.official.recheck_quota_blocks()), 1)
        self.official.finish(self.official.claim(), {'status': 'completed', 'response': READINESS_REPLY,
            'submitted': False, 'native': str(uuid.uuid4())})
        self.assertEqual(self.dispatch_restriction(), 'official_call_blocked')

    def test_queued_recheck_revalidates_quota_and_never_clears_a_newer_block(self):
        self.now += QUOTA_RECHECK_COOLDOWN + 1
        self.readable_quota()
        [probe] = self.official.recheck_quota_blocks()
        self.readable_quota(five_hour=0)
        self.assertIsNone(self.official.claim())
        self.assertEqual(self.pool.get(probe)['status'], 'failed')

        self.now += 60
        self.readable_quota()
        self.assertEqual(len(self.official.recheck_quota_blocks()), 1)
        job = self.official.claim()
        with self.pool.connection() as db:
            db.execute("UPDATE official_account_blocks SET reason='verification_required',created=? WHERE account_key=?",
                       (self.now + 1, self.account))
        self.completed_check(job)
        self.assertEqual(self.dispatch_restriction(), 'official_call_blocked')

    def test_quota_check_waits_for_queued_business_but_can_use_a_spare_slot(self):
        self.now += QUOTA_RECHECK_COOLDOWN + 1
        self.readable_quota()
        [probe] = self.official.recheck_quota_blocks()
        business = self.submit(self.other)
        self.assertIsNone(self.official.claim(probe_only=True))
        self.assertEqual(self.official.claim()['request_id'], business)
        self.assertEqual(self.official.claim(probe_only=True)['request_id'], probe)

    def test_automated_check_does_not_resume_an_operator_paused_account(self):
        self.now += QUOTA_RECHECK_COOLDOWN + 1
        self.readable_quota()
        with self.pool.connection() as db:
            db.execute('INSERT INTO account_preferences(account_key,gemini_enabled) VALUES(?,0)', (self.account,))
        self.assertEqual(self.official.recheck_quota_blocks(), [])
        self.assertEqual(self.dispatch_restriction(), 'dispatch_paused')
