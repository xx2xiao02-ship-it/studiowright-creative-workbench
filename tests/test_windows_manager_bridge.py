from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch


class WindowsManagerBridgeTests(unittest.TestCase):
    def test_sync_queue_launches_one_at_a_time_and_reuses_active_profiles(self):
        source = (Path(__file__).resolve().parents[1]
                  / 'packaging/windows/client-payload/flow-manager/chrome_manager.py')
        spec = spec_from_file_location('windows_profile_manager', source)
        manager = module_from_spec(spec)
        spec.loader.exec_module(manager)
        first = Event()
        second = Event()
        opened = []

        def open_window(profile, url):
            opened.append((profile, url.split('=')[-1]))
            (first if len(opened) == 1 else second).set()

        with patch.object(manager, 'open_profile', side_effect=open_window), \
                patch.object(manager, 'flow_backend_ready', return_value=True):
            self.assertEqual(manager.enqueue_sync(['Profile 1', 'Profile 2', 'Profile 1'])['queued'],
                             ['Profile 1', 'Profile 2'])
            self.assertTrue(first.wait(3))
            self.assertFalse(second.wait(.1))
            self.assertEqual(manager.enqueue_sync(['Profile 1', 'Profile 2'])['reused'],
                             ['Profile 1', 'Profile 2'])
            self.assertEqual(opened[0][0], 'Profile 1')
            with manager.SYNC_QUEUE:
                manager.JOBS[opened[0][1]]['status'] = 'pending'
                manager.SYNC_QUEUE.notify_all()
            self.assertTrue(second.wait(3))
            self.assertEqual(opened[1][0], 'Profile 2')
            with manager.SYNC_QUEUE:
                manager.JOBS[opened[1][1]]['status'] = 'succeeded'
                manager.SYNC_QUEUE.notify_all()

    def test_supported_console_returns_the_bridge_and_connection_check_flag(self):
        source = Path(__file__).resolve().parents[1] / 'packaging/windows/client-payload/manager_bridge.py'
        spec = spec_from_file_location('windows_manager_bridge', source)
        manager = module_from_spec(spec)
        spec.loader.exec_module(manager)

        bridge_id = 'aaipdbnbbgeadlodajhcobjmomnemclj'
        health = {
            'application': 'studiowright-console',
            'bridge_id': bridge_id,
            'bridge_check': True,
        }
        with patch.object(manager, 'build_opener') as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps(health))
            self.assertEqual(manager.selected_bridge(), (bridge_id, True))
