import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/flow/source/src'))

from studiowright.pacing import CoolingDown, Governor, PaceSettings, RiskPaused, save_pace
from studiowright.store import Store


class FlowPacing(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'pacing.db'
        self.store = Store(self.path)
        self.now = time.time()

    def test_generation_refusals_always_have_a_thirty_minute_cooldown(self):
        save_pace(self.store, 'generation', {'cooldown_s': 7200})
        with patch.dict('os.environ', {'STUDIOWRIGHT_COOLDOWN_S': '10800'}):
            self.assertEqual(PaceSettings.load(self.store, 'generation').cooldown_s, 1800)
        governor = Governor(self.store, 'generation', PaceSettings(cooldown_s=14400))
        with patch('subprocess.run'):
            for offset in (0, 2000, 4000):
                cooldown = governor.report_unusual_activity(self.now + offset)
                self.assertEqual(cooldown, self.now + offset + 1800)
                self.assertEqual(governor.status()['cooldown_until'], cooldown)
                if offset == 0:
                    with self.assertRaises(CoolingDown):
                        governor.reserve(cooldown - 1)
            with self.assertRaises(RiskPaused):
                governor.reserve(cooldown + 1)

    def test_existing_cooldowns_keep_the_original_start_and_review_status(self):
        with self.store.transaction() as db:
            db.executemany('INSERT INTO pacing(profile,cooldown_until,refusals) VALUES(?,?,3)', [
                ('generation', self.now + 10800),
                ('generation:upscale', self.now + 10800),
                ('missing-start', self.now + 10800),
                ('already-shorter', self.now + 300),
            ])
            db.executemany('INSERT INTO flow_risk(profile,last_unusual,paused) VALUES(?,?,1)', [
                ('generation', self.now - 600),
                ('already-shorter', self.now - 600),
            ])
        with Store(self.path).connect() as db:
            timers = {row['profile']: row['cooldown_until'] for row in db.execute('SELECT * FROM pacing')}
            risk = db.execute("SELECT paused FROM flow_risk WHERE profile='generation'").fetchone()
        self.assertEqual(timers['generation'], self.now + 1200)
        self.assertEqual(timers['generation:upscale'], self.now + 10800)
        self.assertEqual(timers['missing-start'], self.now + 10800)
        self.assertEqual(timers['already-shorter'], self.now + 300)
        self.assertEqual(risk['paused'], 1)

    def test_upscale_cooling_uses_its_separate_timer(self):
        governor = Governor(self.store, 'generation:upscale', PaceSettings(cooldown_s=3600))
        self.assertEqual(governor.report_unusual_activity(self.now), self.now + 3600)
        self.assertEqual(governor.report_unusual_activity(self.now + 3600), self.now + 10800)
