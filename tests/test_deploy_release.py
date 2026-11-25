from contextlib import nullcontext
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu'))
import deploy_release as release
from test_flow_regressions import fixture_directory


@unittest.skipUnless(sys.platform.startswith('linux'), 'Ubuntu deployment')
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(fixture_directory())
        roots, self.state = release.paths(self.home)
        self.state.mkdir(parents=True)
        self.target = roots['workbench'] / 'server.py'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b'old version')
        self.snapshot = self.home / 'snapshot'
        self.candidate = self.snapshot / 'workbench/server.py'
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b'new version')
        self.changes = {'workbench/server.py': {
            'before': hashlib.sha256(self.target.read_bytes()).hexdigest(),
            'after': hashlib.sha256(self.candidate.read_bytes()).hexdigest()}}

    def test_failed_start_restores_program_and_existing_configuration(self):
        unit = 'creative-unified-workbench'
        config = release.unit_file(self.home, unit)
        config.parent.mkdir(parents=True)
        config.write_bytes(b'previous release')
        with patch.object(release, 'pause_dispatch', return_value=nullcontext()), \
                patch.object(release, 'systemctl') as systemctl, \
                patch.object(release, 'healthy', side_effect=[None, RuntimeError('startup failed'), None]), \
                patch.object(release, 'backup_databases'):
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                release.apply(self.home, self.snapshot, 'workbench', self.changes, 'a'*40, '0.3.7')
        self.assertEqual(self.target.read_bytes(), b'old version')
        self.assertEqual(config.read_bytes(), b'previous release')
        self.assertFalse(release.unit_file(self.home, 'creative-unified-api').exists())
        self.assertEqual([c.args[0] for c in systemctl.call_args_list],
                         ['stop', 'daemon-reload', 'start', 'stop', 'daemon-reload', 'start'])

    def test_does_not_take_over_maintenance_requested_elsewhere(self):
        marker = self.state / 'maintenance'
        marker.write_bytes(b'existing maintenance')
        with self.assertRaises(FileExistsError):
            with release.pause_dispatch(self.home):
                self.fail('existing maintenance must take priority')
        self.assertEqual(marker.read_bytes(), b'existing maintenance')

    def test_failed_rollback_keeps_admission_paused(self):
        with patch.object(release, 'active_tasks', return_value=0), patch.object(release.time, 'sleep'):
            with self.assertRaises(release.RollbackFailed):
                with release.pause_dispatch(self.home):
                    raise release.RollbackFailed('fixture')
        self.assertTrue((self.state / 'maintenance').is_file())


if __name__ == '__main__':
    unittest.main()
