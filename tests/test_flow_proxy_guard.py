from pathlib import Path
import shutil
import stat
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging/ubuntu/snapshot/flow/console'))
import flow_proxy_guard as guard


class FlowProxyGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ('.fixture-' + uuid.uuid4().hex)
        (self.root / 'bin').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.mapping = self.root / 'bin/exit-map.txt'
        self.mapping.write_text('account-a 3101\naccount-b 3102\n', encoding='utf-8')
        self.mapping.chmod(0o600)
        self.profile = '--user-data-dir=' + str(self.root / 'data/profiles/account-a')
        if sys.platform == 'win32':
            lstat = Path.lstat

            def posix_lstat(path, *args, **kwargs):
                info = lstat(path, *args, **kwargs)
                if path == self.mapping:
                    return SimpleNamespace(st_uid=info.st_uid, st_mode=stat.S_IFREG | 0o600, st_size=info.st_size)
                return info

            self.enterContext(patch.object(Path, 'lstat', posix_lstat))

    def test_uses_only_the_assigned_proxy_after_a_successful_probe(self):
        with patch.object(guard.os, 'getuid', return_value=self.mapping.stat().st_uid, create=True), \
                patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='204')) as run:
            self.assertEqual(guard.proxy_argument(self.root, [self.profile]), '--proxy-server=socks5://127.0.0.1:3101')
            self.assertIn('127.0.0.1:3101', run.call_args.args[0])
            self.assertIn('--noproxy', run.call_args.args[0])

    def test_unbound_and_explicit_overrides_fail_without_probing(self):
        with patch.object(guard.os, 'getuid', return_value=self.mapping.stat().st_uid, create=True), \
                patch.object(guard, 'check_proxy') as probe:
            with self.assertRaisesRegex(ValueError, 'flow_proxy_unbound'):
                guard.proxy_argument(self.root, [self.profile.replace('account-a', 'other')])
            for flag in ('--proxy-server=direct://', '--no-proxy-server', '--proxy-bypass-list=*'):
                with self.assertRaisesRegex(ValueError, 'flow_proxy_override_forbidden'):
                    guard.proxy_argument(self.root, [self.profile, flag])
            probe.assert_not_called()

    def test_duplicate_mappings_and_unreachable_proxy_stop_launch(self):
        with patch.object(guard.os, 'getuid', return_value=self.mapping.stat().st_uid, create=True):
            with patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(returncode=7, stdout='000')):
                with self.assertRaisesRegex(ValueError, 'flow_proxy_unreachable'):
                    guard.proxy_argument(self.root, [self.profile])
            self.mapping.write_text('account-a 3101\naccount-b 3101\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate_or_invalid_flow_proxy_map'):
                guard.proxy_argument(self.root, [self.profile])
