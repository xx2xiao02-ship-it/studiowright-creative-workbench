import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/workbench"))
import unittest
from unittest.mock import AsyncMock, patch

from wayland_desktop_driver import MAX_COMPOSER_TABS, WaylandDesktopDriver


class TabFocusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.driver = WaylandDesktopDriver({})
        self.driver._identity_confirmed = AsyncMock(return_value=True)
        self.driver._os_focus_confirmed = AsyncMock(return_value=True)
        self.page, self.composer = AsyncMock(), AsyncMock()
        self.target = object()

    async def test_tab_focus_checks_the_editor_without_clicking(self):
        self.composer.evaluate.side_effect = ['other', 'Shift+Tab', 'composer']
        with patch('task_coordinator.record') as record:
            self.assertTrue(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
            record.assert_called_once_with('composer_tab_focused', steps=1)
        self.page.keyboard.press.assert_awaited_once_with('Shift+Tab')
        self.composer.click.assert_not_awaited()

    async def test_focus_loss_stops_further_tab_keys(self):
        self.composer.evaluate.side_effect = ['other', 'Tab']
        self.driver._os_focus_confirmed.side_effect = [True, True, False]
        self.assertFalse(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
        self.page.keyboard.press.assert_awaited_once_with('Tab')

    async def test_tab_search_does_not_continue_indefinitely(self):
        self.composer.evaluate.side_effect = ['other', 'Tab'] + ['other'] * MAX_COMPOSER_TABS
        self.assertFalse(await self.driver.composer_ready_cdp(self.page, self.composer, self.target))
        self.assertEqual(self.page.keyboard.press.await_count, MAX_COMPOSER_TABS)
        self.composer.click.assert_not_awaited()
