from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


class WindowsManagerBridgeTests(unittest.TestCase):
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
