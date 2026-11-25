import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging' / 'ubuntu'))
from build_server import SNAPSHOT_MANIFEST, refresh_snapshot, verify_snapshot
from test_flow_regressions import fixture_directory


class SnapshotPackageTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(fixture_directory())
        self.snapshot = self.root / 'snapshot'
        self.snapshot.mkdir()
        self.manifest = self.snapshot / SNAPSHOT_MANIFEST
        self.program = self.snapshot / 'program.py'
        self.program.write_bytes(b'print("fixture")\n')
        self.checksums = {'program.py': hashlib.sha256(self.program.read_bytes()).hexdigest()}
        self.manifest.write_text(json.dumps(self.checksums), encoding='utf-8')

    def test_stages_only_files_listed_in_manifest(self):
        (self.snapshot / 'local-settings.json').write_text('fixture', encoding='utf-8')
        stage = self.root / 'stage'
        self.assertEqual(verify_snapshot(self.snapshot, stage), 1)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ['program.py', SNAPSHOT_MANIFEST])
        self.assertEqual((stage / 'program.py').read_bytes(), self.program.read_bytes())

    def test_rejects_stale_checksum_and_relative_escape(self):
        self.program.write_bytes(b'changed\n')
        with self.assertRaisesRegex(ValueError, 'program.py'):
            verify_snapshot(self.snapshot)
        self.manifest.write_text(json.dumps({'../program.py': self.checksums['program.py']}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '无效的快照路径'):
            verify_snapshot(self.snapshot)

    def test_refresh_includes_new_tracked_files_only(self):
        (self.root / '.git').mkdir()
        (self.snapshot / 'added.py').write_bytes(b'pass\n')
        (self.snapshot / 'local-settings.json').write_text('fixture', encoding='utf-8')
        tracked = {'program.py', 'added.py', SNAPSHOT_MANIFEST}
        with patch('build_server.tracked_snapshot_names', return_value=tracked):
            with self.assertRaisesRegex(ValueError, 'added.py'):
                verify_snapshot(self.snapshot)
            self.assertEqual(refresh_snapshot(self.snapshot), 2)
            self.assertEqual(verify_snapshot(self.snapshot), 2)


if __name__ == '__main__':
    unittest.main()
