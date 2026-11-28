import importlib.util
import json
import os
import sys
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

WORKBENCH = Path(__file__).resolve().parents[1] / 'packaging' / 'ubuntu' / 'snapshot' / 'workbench'


def load_quota_module():
    # Keep the module import hermetic: the real official_runtime only matters
    # at runtime, so a stub satisfies the import without spawning anything.
    module_type = sys.modules.setdefault('official_runtime', types.ModuleType('official_runtime'))
    if not hasattr(module_type, 'official_network_env'):
        module_type.official_network_env = lambda **kwargs: {}
    spec = importlib.util.spec_from_file_location('official_quota_under_test', WORKBENCH / 'official_quota.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bare_reader(rows, module=None):
    module = module or load_quota_module()
    reader = module.QuotaReader.__new__(module.QuotaReader)  # skip __init__: no daemon thread
    reader.lock = threading.Lock()
    reader.running = False
    reader.attempted = 0
    reader.pending_recovery = ()
    reader.recovery_accounts = set()
    reader.rows = rows
    return reader


class AutoRefreshTickTest(unittest.TestCase):
    def test_advanced_quota_totals_use_one_shared_model_pool(self):
        module = load_quota_module()
        now = 1000
        row = lambda models, five, weekly: {
            'status': 'ok', 'checked_at': now - 30, 'models': models,
            'five_hour': {'remaining_percent': five},
            'weekly': {'remaining_percent': weekly},
        }
        rows = {
            'hydrogen': row(['gemini-4-hydrogen-high'], 80, 60),
            'neon': row(['gemini-4-neon-high'], 40, 20),
            'both': row(['gemini-4-hydrogen-high', 'gemini-4-neon-high'], 70, 50),
        }

        shared = module.aggregate(rows, now)

        self.assertEqual(shared['total_accounts'], 3)
        self.assertEqual(shared['known_accounts'], 3)
        self.assertEqual(shared['windows']['five_hour']['total_remaining_percent'], 190)
        self.assertEqual(shared['windows']['five_hour']['average_remaining_percent'], 63.33)
        self.assertEqual(shared['windows']['weekly']['total_remaining_percent'], 130)

    def test_advanced_tick_only_refreshes_the_allowlisted_readable_accounts(self):
        reader = bare_reader({'a': {'status': 'ok'}, 'b': {'status': 'ok'}, 'c': {'status': 'verification_required'}})
        reader.runtime = types.SimpleNamespace(text_models=lambda:{'b':{'model'},'c':set()})
        read = []
        reader.check = lambda key, **_: read.append(key) or {'status':'ok'}
        self.assertEqual(reader._auto_refresh_tick(advanced_only=True),1)
        self.assertEqual(read,['b'])

    def test_tick_refreshes_a_recovering_basic_account_and_leaves_verification_to_the_provider(self):
        reader = bare_reader({'a'*64: {'status':'unavailable'}, 'b'*64: {'status':'verification_required'}})
        reader.runtime = types.SimpleNamespace(text_models=lambda:{})
        reader.watch_quota_recovery(['a'*64, 'b'*64, 'c'*64])
        read = []
        reader.check = lambda key, **_: read.append(key) or {'status':'ok'}
        self.assertEqual(reader._auto_refresh_tick(advanced_only=True),2)
        self.assertEqual(set(read), {'a'*64,'c'*64})

    def test_headless_usage_counts_only_google_shared_gemini_windows(self):
        module=load_quota_module()
        output=('Gemini Models\tWeekly Limit Remaining\t96%\t2026-11-26T07:09:35Z\n'
                'Gemini Models\tFive Hour Limit Remaining\t0%\t2026-11-19T12:09:35Z\n'
                'Claude and GPT models\tWeekly Limit Remaining\t43%\t2026-11-26T07:09:35Z\n')
        windows=module.parse_headless_usage(output)
        self.assertEqual(windows['weekly']['remaining_percent'],96)
        self.assertEqual(windows['five_hour']['remaining_percent'],0)
        reset=windows['five_hour']['reset_at']
        row={'status':'ok','checked_at':reset-80,**windows}
        self.assertEqual(module.quota_restriction(row,reset-10),'quota_exhausted')
        self.assertIsNone(module.quota_restriction(row,reset+10))
        self.assertEqual(module.quota_restriction({'status':'verification_required'},reset),'verification_required')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'The official quota reader runs on Ubuntu')
    def test_headless_quota_uses_each_accounts_login_mode(self):
        module = load_quota_module()
        reply = types.SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'status':'SUCCESS',
            'response':('Gemini Models\tWeekly Limit Remaining\t96%\t\n'
                        'Gemini Models\tFive Hour Limit Remaining\t80%\t\n'),
        }))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'home/.gemini/antigravity-cli'
            for name in ('antigravity-oauth-token', 'settings.json', 'jetski_state.pbtxt', 'cache/onboarding.json'):
                path = config / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            runtime = types.SimpleNamespace(account=lambda _: {
                'root':root, 'binary':root/'cli', 'email':'test@example.invalid',
            }, text_models=lambda: {'advanced': {'gemini-4-hydrogen-medium'}})
            for key in ('ordinary', 'advanced'):
                with self.subTest(account=key), patch.object(module, 'official_network_env', return_value={}), \
                     patch.object(module.subprocess, 'run', return_value=reply) as runner:
                    row = module.read_account(runtime, key)
                    runner.assert_called_once()
                    self.assertEqual(runner.call_args.args[0][1:3], ['--print', '/usage'])
                    env = runner.call_args.kwargs['env']
                    self.assertEqual('SSH_TTY' in env, key == 'advanced')
                    self.assertEqual('SSH_CONNECTION' in env, key == 'advanced')
                    self.assertNotEqual(env['HOME'], str(root/'home'))
                    self.assertEqual(row['five_hour']['remaining_percent'], 80)
                    self.assertEqual(row['weekly']['remaining_percent'], 96)

    def test_quota_parsers_keep_the_reported_window_when_one_is_missing(self):
        module = load_quota_module()
        weekly = 'Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-29T12:17:00Z'
        five_hour = 'Gemini Models\tFive Hour Limit Remaining\t0%\t'
        self.assertEqual(set(module.parse_headless_usage(weekly)), {'weekly'})
        self.assertEqual(module.parse_headless_usage(five_hour),
                         {'five_hour': {'remaining_percent': 0.0, 'reset_at': None}})
        self.assertEqual(module.parse_usage('GEMINI MODELS\nWeekly Limit Remaining [===] 100% Quota available', 0),
                         {'weekly': {'remaining_percent': 100.0, 'reset_at': None}})

    def test_tick_reads_only_readable_accounts_and_resets_running(self):
        reader = bare_reader({'a': {'status': 'ok'}, 'b': {'status': 'verification_required'},
                              'c': {'status': 'not_read'}, 'd': {'status': 'network_error'}})
        read = []
        reader.check = lambda key, **_: read.append(key) or {'status': 'ok', 'checked_at': 0}

        self.assertEqual(reader._auto_refresh_tick(), 2)
        self.assertEqual(read, ['a', 'c'])
        self.assertFalse(reader.running)

    def test_tick_skips_while_a_refresh_is_already_running(self):
        reader = bare_reader({'a': {'status': 'ok'}})
        reader.running = True
        read = []
        reader.check = lambda key: read.append(key) or {'status': 'ok'}

        self.assertEqual(reader._auto_refresh_tick(), 0)
        self.assertEqual(read, [])
        self.assertTrue(reader.running)

    def test_tick_does_nothing_without_known_accounts(self):
        reader = bare_reader({})
        read = []
        reader.check = lambda key: read.append(key) or {'status': 'ok'}

        self.assertEqual(reader._auto_refresh_tick(), 0)
        self.assertEqual(read, [])

    @unittest.skipUnless(hasattr(os, 'fchmod'), 'The quota cache is written on Ubuntu')
    def test_startup_failure_is_retried_before_updating_the_account_card_data(self):
        module = load_quota_module()
        reader = bare_reader({}, module)
        key = 'a' * 64
        previous = {'status':'ok','checked_at':100,'weekly':{'remaining_percent':90}}
        reader.rows[key] = previous
        reader.runtime = object()
        reader.account_locks = {}
        reader.verification_links = {}
        failure = {'status':'unavailable','reason':'startup_not_ready','checked_at':110}
        success = {'status':'ok','checked_at':112,'five_hour':{'remaining_percent':100},'weekly':{'remaining_percent':99}}
        with tempfile.TemporaryDirectory() as directory:
            reader.cache = Path(directory) / 'quota.json'
            for recover, outcomes, expected in (
                (False, [failure, success], 'ok'),
                (False, [failure, failure], 'unavailable'),
                (True, [failure, {'status':'network_error'}], 'network_error'),
                (True, [{'status':'authorization_required'}], 'authorization_required'),
                (False, [{'status':'verification_required'}], 'verification_required'),
            ):
                with self.subTest(recover=recover, expected=expected):
                    reader.rows[key] = previous
                    output = iter(outcomes)
                    def read(runtime, profile):
                        self.assertEqual(reader.rows[key], previous)
                        self.assertEqual(profile, key)
                        return next(output).copy()
                    with patch.object(module, 'read_account', side_effect=read) as attempts:
                        result = reader.check(key, recover=recover)
                    self.assertEqual(result['status'], expected)
                    self.assertEqual(result['query_attempts'], len(outcomes))
                    self.assertEqual(attempts.call_count, len(outcomes))
                    self.assertEqual(json.loads(reader.cache.read_text())['accounts'][key], result)
